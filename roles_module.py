"""إضافة إدارة أدوار الحصص إلى تطبيق تيليغرام قائم.

بعد بناء application وقبل تشغيله أو تسجيل أي معالج عام للأزرار،
استدعي register_roles(application) من ملف بوت التذكير.
إذا استُخدم معالج عام للأزرار، سجّليه بعدها ضمن المجموعة نفسها (0 افتراضيًا)
أو استثني منه بيانات الأزرار التي تبدأ بـ roles:.

متغيرات البيئة الخاصة بهذه الإضافة
    ROLES_ADMIN_IDS: معرّفات الإدارة مفصولة بفواصل، مطلوب.
    ROLES_ALLOWED_CHAT_IDS: معرّفات المجموعات المسموح لها؛ يمكن تخصيصها لمجموعة الاختبار.
    ROLES_DB_PATH: مسار SQLite مستقل، الافتراضي /data/roles.db.
    ROLES_TIMEZONE: المنطقة الزمنية للتقارير، الافتراضي Asia/Riyadh.
    ROLES_DEFAULT_CAPACITY: الحد الأساسي الافتراضي، 15.
    PANEL_URL / PANEL_API_KEY: عنوان اللوحة ومفتاحها؛ إن ضُبطا أُرسلت لقطة كل جلسة إلى اللوحة بعد كل تغيير.
    ROLES_TELEGRAM_ADMINS: true (الافتراضي) تُعدّ مشرفات المجموعة في تيليغرام مديرات للجلسات؛ false لتعطيله.

الأوامر داخل المجموعة أو الموضوع
    /roles_open اسم الحصة
    /roles_name اسم الحصة
    /roles_limit 15
    /roles_assign و /roles_unassign بالرد على رسالة المعلمة
    /roles_setsticker open أو next أو finish بالرد على الملصق

تحتاج الإضافة إلى python-telegram-bot وpytz. استخدمي قاعدة اختبار مستقلة
عند التجربة، وتأكدي من صلاحية البوت لإرسال الرسائل وتثبيتها في المجموعة.
"""

import asyncio
import html
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytz
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import CallbackQueryHandler, CommandHandler, filters

log = logging.getLogger(__name__)

FLORAL_DIVIDER = "\u200f⋆⊱༻𖥸༺⊰⋆"
STAR_DIVIDER = "\u200f✦•┈๑⋅⋯ ⋯⋅๑┈•✦"
# Retained as a wider option for layouts with more space.
HEART_DIVIDER = "\u200f₊˚ ‿︵‿︵‿︵୨୧ · · ♡ · · ୨୧‿︵‿︵‿︵ ˚₊"

# علامة اليمين-إلى-اليسار تثبّت حواف الأسطر التي تبدأ برمز تعبيري.
# لا نستعمل blockquote: يلصقه تيليغرام باليسار بينما بقية الأسطر محاذية لليمين.
SCHOOL_HEADER = (
    "\u200f✨﷽✨\n\u200f<b>المدرسة النافعية</b>",
    "",
)


def read_ids(name):
    raw = os.getenv(name, "").replace("،", ",")
    return {int(value.strip()) for value in raw.split(",") if value.strip()}



def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def column_names(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def add_column(db, table, definition):
    name = definition.split()[0]
    if name not in column_names(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db():
    with closing(db_connect()) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_key TEXT NOT NULL UNIQUE,
          chat_id INTEGER NOT NULL,
          course TEXT NOT NULL,
          teacher TEXT,
          riwaya TEXT,
          starts_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          message_id INTEGER,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entries(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL,
          display_name TEXT NOT NULL,
          username TEXT,
          queue_type TEXT NOT NULL DEFAULT 'main',
          position INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'waiting',
          registered_at TEXT NOT NULL,
          UNIQUE(session_id,user_id),
          UNIQUE(session_id,position));
        CREATE TABLE IF NOT EXISTS topic_settings(
          chat_id INTEGER NOT NULL,
          thread_id INTEGER NOT NULL,
          title TEXT NOT NULL,
          capacity INTEGER NOT NULL DEFAULT 15,
          waitlist_open INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(chat_id,thread_id));
        CREATE TABLE IF NOT EXISTS topic_staff(
          chat_id INTEGER NOT NULL,
          thread_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          display_name TEXT NOT NULL,
          added_by INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(chat_id,thread_id,user_id));
        CREATE TABLE IF NOT EXISTS panel_pending(
          session_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS stickers(
          scope_chat_id INTEGER NOT NULL DEFAULT 0,
          event TEXT NOT NULL,
          file_id TEXT NOT NULL,
          PRIMARY KEY(scope_chat_id,event));
        """)
        legacy_sessions = "thread_id" not in column_names(db, "sessions")
        add_column(db, "sessions", "thread_id INTEGER NOT NULL DEFAULT 0")
        add_column(db, "sessions", "management_message_id INTEGER")
        add_column(db, "sessions", f"capacity INTEGER NOT NULL DEFAULT {DEFAULT_CAPACITY}")
        add_column(db, "sessions", "waitlist_open INTEGER NOT NULL DEFAULT 1")
        add_column(db, "sessions", "opened_by INTEGER")
        add_column(db, "sessions", "ended_at TEXT")
        add_column(db, "entries", "queue_type TEXT NOT NULL DEFAULT 'main'")
        if legacy_sessions:
            db.execute("UPDATE sessions SET status='finished',ended_at=? WHERE status IN ('open','running','review','paused')",
                       (now_iso(),))
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_scope ON sessions(chat_id,thread_id,status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_entries_queue ON entries(session_id,status,position)")
        db.commit()


def now_iso():
    return datetime.now(TZ).isoformat()

# ───────────── إرسال لقطات الجلسات إلى اللوحة ─────────────
# كل تغيير يُرسل لقطة كاملة للجلسة؛ تُسجَّل الجلسة في panel_pending قبل الإرسال وتُحذف عند نجاحه،
# فإن تعذّر الاتصال أعادت panel_retry_loop المحاولة. أي فشل هنا لا يؤثر في عمل الجلسة داخل تيليغرام.
PANEL_URL = ""
PANEL_API_KEY = ""
_push_state = {}          # session_id -> "running" | "dirty"
_push_tasks = set()


def panel_enabled():
    return bool(PANEL_URL and PANEL_API_KEY)


def build_snapshot(db, sid):
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        return None
    rows = db.execute("""SELECT user_id,display_name,username,queue_type,status,position,registered_at
                         FROM entries WHERE session_id=? ORDER BY queue_type,position""", (sid,)).fetchall()
    return {
        "session_key": s["session_key"], "chat_id": s["chat_id"], "thread_id": s["thread_id"],
        "course": s["course"], "opened_by": s["teacher"] or "", "opened_at": s["created_at"],
        "ended_at": s["ended_at"] or "", "status": s["status"], "capacity": s["capacity"],
        "snapshot_at": now_iso(),
        "entries": [{"user_id": r["user_id"], "display_name": r["display_name"], "username": r["username"] or "",
                     "queue_type": r["queue_type"], "status": r["status"], "position": r["position"],
                     "registered_at": r["registered_at"]} for r in rows],
    }


def _post_snapshot(payload):
    r = requests.post(f"{PANEL_URL}/api/roles/session", json=payload, timeout=10,
                      headers={"Authorization": f"Bearer {PANEL_API_KEY}"})
    if r.status_code == 400:      # لقطة مرفوضة لا تُعاد محاولتها (لن تنجح أبدًا)
        log.error("رفضت اللوحة لقطة الجلسة: %s", r.text[:200])
        return True
    r.raise_for_status()
    return True


async def push_session(sid):
    """يرسل لقطة الجلسة؛ الاستدعاءات المتزامنة لنفس الجلسة تُدمج في إرسال لاحق واحد."""
    if not panel_enabled():
        return
    if _push_state.get(sid):
        _push_state[sid] = "dirty"; return
    _push_state[sid] = "running"
    try:
        while True:
            _push_state[sid] = "running"
            with closing(db_connect()) as db:
                db.execute("INSERT OR IGNORE INTO panel_pending(session_id) VALUES(?)", (sid,)); db.commit()
                payload = build_snapshot(db, sid)
            if payload is None:
                with closing(db_connect()) as db:
                    db.execute("DELETE FROM panel_pending WHERE session_id=?", (sid,)); db.commit()
            else:
                try:
                    await asyncio.to_thread(_post_snapshot, payload)
                    if _push_state.get(sid) != "dirty":
                        with closing(db_connect()) as db:
                            db.execute("DELETE FROM panel_pending WHERE session_id=?", (sid,)); db.commit()
                except Exception as exc:
                    log.warning("تعذّر إرسال الجلسة %s إلى اللوحة (ستُعاد المحاولة): %s", sid, exc)
                    return
            if _push_state.get(sid) != "dirty":
                return
    finally:
        _push_state.pop(sid, None)


def schedule_push(sid):
    if not panel_enabled():
        return
    task = asyncio.create_task(push_session(sid))
    _push_tasks.add(task)
    task.add_done_callback(_push_tasks.discard)


async def panel_retry_loop(interval=60):
    """يعيد إرسال الجلسات التي لم تصل إلى اللوحة (انقطاع أو إعادة تشغيل)."""
    while True:
        await asyncio.sleep(interval)
        if not panel_enabled():
            continue
        try:
            with closing(db_connect()) as db:
                ids = [r[0] for r in db.execute("SELECT session_id FROM panel_pending")]
            for sid in ids:
                await push_session(sid)
        except Exception as exc:
            log.warning("فشل دورة إعادة الإرسال إلى اللوحة: %s", exc)



def scope(update):
    message = update.effective_message
    return update.effective_chat.id, int(getattr(message, "message_thread_id", 0) or 0)


def thread_kwargs(thread_id):
    return {"message_thread_id": thread_id} if thread_id else {}


def session_scope(s):
    return thread_kwargs(int(s["thread_id"] or 0))


def active_session(db, chat_id, thread_id):
    return db.execute(
        "SELECT * FROM sessions WHERE chat_id=? AND thread_id=? AND status IN ('open','running','review','paused') ORDER BY id DESC LIMIT 1",
        (chat_id, thread_id)).fetchone()


def topic_setting(db, chat_id, thread_id):
    return db.execute("SELECT * FROM topic_settings WHERE chat_id=? AND thread_id=?",
                      (chat_id, thread_id)).fetchone()


def is_topic_staff(db, chat_id, thread_id, user_id):
    if user_id in ADMIN_IDS:
        return True
    return db.execute(
        "SELECT 1 FROM topic_staff WHERE chat_id=? AND thread_id=? AND user_id=?",
        (chat_id, thread_id, user_id)).fetchone() is not None


_ADMIN_CACHE = {}


async def is_chat_admin(bot, chat_id, user_id):
    """هل هي مشرفة (أو مالكة) في المجموعة في تيليغرام؟ نتيجة تُحفظ 60 ثانية؛ أي عطل = ليست مشرفة."""
    if not TELEGRAM_ADMINS:
        return False
    key = (chat_id, user_id)
    now = datetime.now().timestamp()
    hit = _ADMIN_CACHE.get(key)
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        value = getattr(member, "status", "") in ("administrator", "creator")
    except Exception:
        log.warning("تعذر فحص صلاحية المشرفة %s في %s", user_id, chat_id)
        return False
    _ADMIN_CACHE[key] = (now, value)
    return value


async def is_allowed_manager(update, context):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        return True
    chat_id, thread_id = scope(update)
    with closing(db_connect()) as db:
        if is_topic_staff(db, chat_id, thread_id, user_id):
            return True
    return await is_chat_admin(context.bot, chat_id, user_id)


async def deny(message):
    await message.reply_text("هذه الأداة خاصة بإدارة هذا الموضوع.")


def unique_key(chat_id, thread_id):
    return f"{chat_id}:{thread_id}:{datetime.now(TZ).strftime('%Y%m%d%H%M%S%f')}"


def student_keyboard(sid, status, waitlist_open):
    if status != "open":
        return InlineKeyboardMarkup([[InlineKeyboardButton("📋 عرض اللائحة", callback_data=f"roles:list:{sid}")]])
    rows = [[InlineKeyboardButton("📝 أخذ دور", callback_data=f"roles:join:{sid}"),
             InlineKeyboardButton("❌ إلغاء دوري", callback_data=f"roles:cancel:{sid}")],
            [InlineKeyboardButton("📋 عرض اللائحة", callback_data=f"roles:list:{sid}")]]
    if not waitlist_open:
        rows.append([InlineKeyboardButton("قائمة الانتظار مغلقة", callback_data=f"roles:list:{sid}")])
    return InlineKeyboardMarkup(rows)


def manager_keyboard(sid, status, waitlist_open):
    if status == "review":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ اعتماد التقرير", callback_data=f"roles:approve:{sid}"),
             InlineKeyboardButton("↩️ العودة للجلسة", callback_data=f"roles:reopen:{sid}")]])
    if status == "finished":
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اكتمل الدور", callback_data=f"roles:done:{sid}"),
         InlineKeyboardButton("⏭ غائبة", callback_data=f"roles:absent:{sid}")],
        [InlineKeyboardButton("🔄 إلى آخر اللائحة", callback_data=f"roles:defer:{sid}"),
         InlineKeyboardButton("▶️ استدعاء الحالية", callback_data=f"roles:next:{sid}")],
        [InlineKeyboardButton("🔒 إغلاق التسجيل", callback_data=f"roles:close:{sid}"),
         InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data=f"roles:pause:{sid}")],
        [InlineKeyboardButton("➕ زيادة الحد", callback_data=f"roles:limitup:{sid}"),
         InlineKeyboardButton("➖ خفض الحد", callback_data=f"roles:limitdown:{sid}")],
        [InlineKeyboardButton("🔐 إغلاق الانتظار" if waitlist_open else "🔓 فتح الانتظار",
                              callback_data=f"roles:waittoggle:{sid}")],
        [InlineKeyboardButton("⏹ معاينة التقرير", callback_data=f"roles:finish:{sid}")]])


def queue_rows(db, sid, queue_type):
    return db.execute(
        "SELECT * FROM entries WHERE session_id=? AND queue_type=? AND status IN ('waiting','current') ORDER BY position",
        (sid, queue_type)).fetchall()


def student_text(db, sid):
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    main = queue_rows(db, sid, "main")
    waiting = queue_rows(db, sid, "waitlist")
    lines = [
        *SCHOOL_HEADER,
        "\u200f🎓 <b>خاص بالطالبة</b>",
        FLORAL_DIVIDER,
        f"🎙️ <b>تسجيل أدوار {html.escape(s['course'])}</b>",
    ]
    if main:
        lines += ["", "📋 <b>الأدوار الأساسية:</b>", ""]
        lines += [f"{i}. {html.escape(e['display_name'])}" + (" ← الدور الحالي" if e["status"] == "current" else "")
                  for i, e in enumerate(main, 1)]
    else:
        lines += ["", "لم تسجل أي مشاركة بعد."]
    if waiting:
        lines += ["", "⏳ <b>قائمة الانتظار الاحتياطية:</b>"]
        lines += [f"{i}. {html.escape(e['display_name'])}" for i, e in enumerate(waiting, 1)]
    lines += ["", STAR_DIVIDER,
              f"📌 الأدوار الأساسية: <b>{len(main)} من {s['capacity']}</b>",
              f"🕰 قائمة الانتظار: <b>{len(waiting)}</b>"]
    state = {"open": "التسجيل مفتوح.", "running": "التسجيل مغلق وبدأت متابعة الأدوار.",
             "review": "الجلسة في مراجعة التقرير.", "finished": "انتهت الجلسة."}
    lines.append(f"🔔 {state.get(s['status'], 'الجلسة متوقفة مؤقتًا.')}")
    return "\n".join(lines)


def manager_text(db, sid):
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    current = db.execute(
        "SELECT * FROM entries WHERE session_id=? AND status='current' ORDER BY position LIMIT 1", (sid,)
    ).fetchone()
    nxt = db.execute(
        "SELECT * FROM entries WHERE session_id=? AND status='waiting' AND queue_type='main' ORDER BY position LIMIT 1",
        (sid,)).fetchone()
    status_ar = {
        "open": "التسجيل مفتوح",
        "running": "متابعة الأدوار جارية",
        "paused": "متوقفة مؤقتًا",
        "review": "مراجعة التقرير",
        "finished": "منتهية",
    }.get(s["status"], s["status"])
    return "\n".join([
        *SCHOOL_HEADER,
        "\u200f👑 <b>خاص بالمعلمة</b>",
        FLORAL_DIVIDER,
        f"🛠 <b>إدارة حصة {html.escape(s['course'])}</b>",
        "",
        f"🔔 الحالة: <b>{html.escape(status_ar)}</b>",
        f"🔢 عدد الأدوار المتاحة: <b>{s['capacity']}</b>",
        f"🎙️ الدور الحالي: <b>{html.escape(current['display_name']) if current else '—'}</b>",
        f"⏭ التالية: <b>{html.escape(nxt['display_name']) if nxt else '—'}</b>"])


async def edit_message_safe(bot, **kwargs):
    try:
        await bot.edit_message_text(**kwargs)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def refresh(context, sid):
    with closing(db_connect()) as db:
        s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            return
        student = student_text(db, sid)
        manager = manager_text(db, sid)
        sk = student_keyboard(sid, s["status"], bool(s["waitlist_open"]))
        mk = manager_keyboard(sid, s["status"], bool(s["waitlist_open"]))
    common = dict(chat_id=s["chat_id"], parse_mode=ParseMode.HTML)
    if s["message_id"]:
        await edit_message_safe(context.bot, message_id=s["message_id"], text=student, reply_markup=sk, **common)
    if s["management_message_id"]:
        await edit_message_safe(context.bot, message_id=s["management_message_id"], text=manager, reply_markup=mk, **common)


def sticker_id(db, chat_id, event):
    row = db.execute("SELECT file_id FROM stickers WHERE scope_chat_id=? AND event=?", (chat_id, event)).fetchone()
    if not row:
        row = db.execute("SELECT file_id FROM stickers WHERE scope_chat_id=0 AND event=?", (event,)).fetchone()
    return row["file_id"] if row else None


async def send_sticker_event(context, s, event):
    with closing(db_connect()) as db:
        file_id = sticker_id(db, s["chat_id"], event)
    if file_id:
        try:
            await context.bot.send_sticker(s["chat_id"], file_id, **session_scope(s))
            return True
        except (BadRequest, Forbidden):
            log.exception("تعذر إرسال ملصق %s", event)
            return False
    return None


async def name_command(update, context):
    if not await is_allowed_manager(update, context):
        await deny(update.effective_message); return
    title = " ".join(context.args).strip()
    if not title:
        await update.effective_message.reply_text("اكتبي الاسم بعد الأمر: /name اسم الحصة"); return
    chat_id, thread_id = scope(update)
    with closing(db_connect()) as db:
        old = topic_setting(db, chat_id, thread_id)
        capacity = old["capacity"] if old else DEFAULT_CAPACITY
        db.execute("""INSERT INTO topic_settings(chat_id,thread_id,title,capacity) VALUES(?,?,?,?)
                      ON CONFLICT(chat_id,thread_id) DO UPDATE SET title=excluded.title""",
                   (chat_id, thread_id, title, capacity))
        db.commit()
    await update.effective_message.reply_text(f"حُفظ اسم الحصة: {title}")


async def assign_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await deny(update.effective_message); return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.from_user or replied.from_user.is_bot:
        await update.effective_message.reply_text("ردّي على رسالة المعلمة بالأمر /roles_assign"); return
    chat_id, thread_id = scope(update)
    user = replied.from_user
    with closing(db_connect()) as db:
        db.execute("""INSERT INTO topic_staff(chat_id,thread_id,user_id,display_name,added_by,created_at)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id,thread_id,user_id)
                      DO UPDATE SET display_name=excluded.display_name""",
                   (chat_id, thread_id, user.id, user.full_name, update.effective_user.id, now_iso()))
        db.commit()
    await update.effective_message.reply_text(f"أُضيفت {user.full_name} لإدارة هذا الموضوع.")


async def unassign_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await deny(update.effective_message); return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.from_user:
        await update.effective_message.reply_text("ردّي على رسالة المعلمة بالأمر /unassign"); return
    chat_id, thread_id = scope(update)
    with closing(db_connect()) as db:
        db.execute("DELETE FROM topic_staff WHERE chat_id=? AND thread_id=? AND user_id=?",
                   (chat_id, thread_id, replied.from_user.id)); db.commit()
    await update.effective_message.reply_text("أُزيلت صلاحية إدارة هذا الموضوع.")


async def limit_command(update, context):
    if not await is_allowed_manager(update, context):
        await deny(update.effective_message); return
    try:
        value = int(context.args[0])
        if not 1 <= value <= 500:
            raise ValueError
    except (IndexError, ValueError):
        await update.effective_message.reply_text("استعملي: /roles_limit 15"); return
    chat_id, thread_id = scope(update)
    with closing(db_connect()) as db:
        setting = topic_setting(db, chat_id, thread_id)
        title = setting["title"] if setting else (update.effective_chat.title or "الحصة")
        db.execute("""INSERT INTO topic_settings(chat_id,thread_id,title,capacity) VALUES(?,?,?,?)
                      ON CONFLICT(chat_id,thread_id) DO UPDATE SET capacity=excluded.capacity""",
                   (chat_id, thread_id, title, value))
        s = active_session(db, chat_id, thread_id)
        if s:
            db.execute("UPDATE sessions SET capacity=? WHERE id=?", (value, s["id"]))
        db.commit()
    if s:
        with closing(db_connect()) as promotion_db:
            promote_waitlist(promotion_db, s["id"])
        schedule_push(s["id"])
        await refresh(context, s["id"])
    await update.effective_message.reply_text(f"الحد الأساسي الآن: {value}")


async def open_command(update, context):
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text("افتحي الجلسة من داخل المجموعة أو الموضوع."); return
    if not await is_allowed_manager(update, context):
        await deny(update.effective_message); return
    chat_id, thread_id = scope(update)
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        await update.effective_message.reply_text("إدارة الأدوار غير مفعّلة في هذه المجموعة.")
        return
    typed_title = " ".join(context.args or []).strip()
    with closing(db_connect()) as db:
        if active_session(db, chat_id, thread_id):
            await update.effective_message.reply_text("توجد جلسة مفتوحة في هذا الموضوع بالفعل."); return
        setting = topic_setting(db, chat_id, thread_id)
        title = typed_title or (setting["title"] if setting else "") or update.effective_chat.title or "الحصة"
        capacity = setting["capacity"] if setting else DEFAULT_CAPACITY
        db.execute("""INSERT INTO topic_settings(chat_id,thread_id,title,capacity) VALUES(?,?,?,?)
                      ON CONFLICT(chat_id,thread_id) DO UPDATE SET
                      title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE topic_settings.title END""",
                   (chat_id, thread_id, title, capacity))
        cur = db.execute("""INSERT INTO sessions(session_key,chat_id,thread_id,course,teacher,starts_at,status,
                          capacity,waitlist_open,opened_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (unique_key(chat_id, thread_id), chat_id, thread_id, title,
                          update.effective_user.full_name, now_iso(), "open", capacity, 1,
                          update.effective_user.id, now_iso()))
        sid = cur.lastrowid
        db.commit()
        s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    sticker_sent = await send_sticker_event(context, s, "open")
    if sticker_sent is False:
        await update.effective_message.reply_text(
            "⚠️ فُتح التسجيل، لكن تعذر إرسال ملصق البداية. تحققي من صلاحية إرسال الملصقات ثم أعيدي ضبطه.")
    student = await context.bot.send_message(chat_id, "جاري فتح الأدوار…", **thread_kwargs(thread_id))
    manager = await context.bot.send_message(chat_id, "جاري إعداد لوحة الإدارة…", **thread_kwargs(thread_id))
    with closing(db_connect()) as db:
        db.execute("UPDATE sessions SET message_id=?,management_message_id=? WHERE id=?",
                   (student.message_id, manager.message_id, sid)); db.commit()
    schedule_push(sid)
    await refresh(context, sid)
    try:
        await context.bot.pin_chat_message(chat_id, student.message_id, disable_notification=True)
    except (BadRequest, Forbidden):
        log.warning("تعذر تثبيت رسالة الجلسة")


def promote_waitlist(db_or_factory, sid):
    db = db_or_factory
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    main_count = db.execute(
        "SELECT COUNT(*) FROM entries WHERE session_id=? AND queue_type='main' AND status IN ('waiting','current')",
        (sid,)).fetchone()[0]
    while main_count < s["capacity"]:
        row = db.execute(
            "SELECT id FROM entries WHERE session_id=? AND queue_type='waitlist' AND status='waiting' ORDER BY position LIMIT 1",
            (sid,)).fetchone()
        if not row:
            break
        db.execute("UPDATE entries SET queue_type='main' WHERE id=?", (row["id"],))
        main_count += 1
    db.commit()


def current_entry(db, sid):
    return db.execute("SELECT * FROM entries WHERE session_id=? AND status='current' LIMIT 1", (sid,)).fetchone()


def advance(db, sid):
    current = current_entry(db, sid)
    if current:
        return current
    row = db.execute(
        "SELECT * FROM entries WHERE session_id=? AND queue_type='main' AND status='waiting' ORDER BY position LIMIT 1",
        (sid,)).fetchone()
    if row:
        db.execute("UPDATE entries SET status='current' WHERE id=?", (row["id"],))
    return row


async def announce_current(context, sid):
    with closing(db_connect()) as db:
        s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        current = current_entry(db, sid)
        following = db.execute(
            "SELECT * FROM entries WHERE session_id=? AND queue_type='main' AND status='waiting' ORDER BY position LIMIT 1",
            (sid,)).fetchone()
    if not current:
        return
    await send_sticker_event(context, s, "next")
    display_name = html.escape(current["display_name"])
    text = f"🎙️ الدور الحالي: <b>{display_name}</b>"
    if following:
        text += f"\nتستعد بعدها: {html.escape(following['display_name'])}"
    await context.bot.send_message(s["chat_id"], text, parse_mode=ParseMode.HTML, **session_scope(s))


async def callback(update, context):
    q = update.callback_query
    announce = False
    try:
        _, action, raw_sid = q.data.split(":", 2)
        sid = int(raw_sid)
    except Exception:
        await q.answer("زر غير صالح.", show_alert=True); return
    tg_admin = False
    if action not in {"join", "cancel", "list"} and q.from_user.id not in ADMIN_IDS:
        tg_admin = await is_chat_admin(context.bot, q.message.chat.id, q.from_user.id)
    with closing(db_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            await q.answer("الجلسة غير موجودة.", show_alert=True); return
        callback_thread = int(getattr(q.message, "message_thread_id", 0) or 0)
        if ALLOWED_CHAT_IDS and q.message.chat.id not in ALLOWED_CHAT_IDS:
            await q.answer("إدارة الأدوار غير مفعّلة في هذه المجموعة.", show_alert=True)
            return
        if q.message.chat.id != s["chat_id"] or callback_thread != s["thread_id"]:
            await q.answer("هذا الزر لا يخص هذا الموضوع.", show_alert=True); return
        manager = tg_admin or is_topic_staff(db, s["chat_id"], s["thread_id"], q.from_user.id)
        if action == "join":
            if s["status"] != "open":
                await q.answer("التسجيل مغلق.", show_alert=True); return
            existing = db.execute("SELECT * FROM entries WHERE session_id=? AND user_id=?",
                                  (sid, q.from_user.id)).fetchone()
            if existing and existing["status"] != "cancelled":
                await q.answer("لديك دور مسجل بالفعل.", show_alert=True); return
            main_count = db.execute(
                "SELECT COUNT(*) FROM entries WHERE session_id=? AND queue_type='main' AND status IN ('waiting','current')",
                (sid,)).fetchone()[0]
            queue_type = "main" if main_count < s["capacity"] else "waitlist"
            if queue_type == "waitlist" and not s["waitlist_open"]:
                await q.answer("اكتملت الأدوار وقائمة الانتظار مغلقة.", show_alert=True); return
            pos = db.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM entries WHERE session_id=?",
                (sid,)).fetchone()[0]
            if existing:
                db.execute("""UPDATE entries SET display_name=?,username=?,queue_type=?,position=?,
                              status='waiting',registered_at=? WHERE id=?""",
                           (q.from_user.full_name, q.from_user.username, queue_type, pos, now_iso(), existing["id"]))
            else:
                db.execute("""INSERT INTO entries(session_id,user_id,display_name,username,queue_type,position,
                              status,registered_at) VALUES(?,?,?,?,?,?,'waiting',?)""",
                           (sid, q.from_user.id, q.from_user.full_name or "مشاركة", q.from_user.username,
                            queue_type, pos, now_iso()))
            db.commit()
            answer = "سُجل دورك في القائمة الأساسية." if queue_type == "main" else "سُجلتِ في قائمة الانتظار."
        elif action == "cancel":
            row = db.execute(
                "SELECT * FROM entries WHERE session_id=? AND user_id=? AND status IN ('waiting','current')",
                (sid, q.from_user.id)).fetchone()
            if not row:
                await q.answer("لا يوجد لك دور قابل للإلغاء.", show_alert=True); return
            db.execute("UPDATE entries SET status='cancelled' WHERE id=?", (row["id"],))
            if row["queue_type"] == "main":
                promote_waitlist(db, sid)
            if row["status"] == "current":
                advance(db, sid)
                announce = True
            db.commit()
            answer = "أُلغي دورك."
        elif action == "list":
            answer = "تم تحديث اللائحة."
        else:
            if not manager:
                await q.answer("هذه الأداة خاصة بإدارة هذا الموضوع.", show_alert=True); return
            answer = "تم."
            if action == "close":
                db.execute("UPDATE sessions SET status='running' WHERE id=?", (sid,))
                advance(db, sid); announce = True
            elif action in {"done", "absent"}:
                current = current_entry(db, sid)
                if not current:
                    await q.answer("لا يوجد دور حالي.", show_alert=True); return
                db.execute("UPDATE entries SET status=? WHERE id=?", (action, current["id"]))
                promote_waitlist(db, sid); advance(db, sid); announce = True
            elif action == "defer":
                current = current_entry(db, sid)
                if not current:
                    await q.answer("لا يوجد دور حالي.", show_alert=True); return
                pos = db.execute(
                    "SELECT COALESCE(MAX(position),0)+1 FROM entries WHERE session_id=?",
                    (sid,)).fetchone()[0]
                db.execute("UPDATE entries SET status='waiting',position=? WHERE id=?", (pos, current["id"]))
                advance(db, sid); announce = True
            elif action == "next":
                advance(db, sid); announce = True
            elif action == "pause":
                new_status = "running" if s["status"] == "paused" else "paused"
                db.execute("UPDATE sessions SET status=? WHERE id=?", (new_status, sid))
                answer = "استؤنفت الجلسة." if new_status == "running" else "أوقفت الجلسة مؤقتًا."
            elif action == "limitup":
                db.execute("UPDATE sessions SET capacity=capacity+1 WHERE id=?", (sid,))
                promote_waitlist(db, sid)
            elif action == "limitdown":
                current_capacity = s["capacity"]
                if current_capacity > 1:
                    db.execute("UPDATE sessions SET capacity=capacity-1 WHERE id=?", (sid,))
            elif action == "waittoggle":
                db.execute("UPDATE sessions SET waitlist_open=1-waitlist_open WHERE id=?", (sid,))
            elif action == "finish":
                db.execute("UPDATE sessions SET status='review' WHERE id=?", (sid,))
                answer = "راجعي التقرير ثم اعتمديه."
            elif action == "reopen":
                db.execute("UPDATE sessions SET status='running' WHERE id=?", (sid,))
            elif action == "approve":
                db.execute("UPDATE sessions SET status='finished',ended_at=? WHERE id=?", (now_iso(), sid))
            db.commit()
    await q.answer(answer)
    if action != "list":
        schedule_push(sid)
    if action == "approve":
        await send_final_report(context, sid)
        with closing(db_connect()) as db:
            fresh = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        await send_sticker_event(context, fresh, "finish")
    elif action == "finish":
        await send_report_preview(context, sid)
    elif announce:
        await announce_current(context, sid)
    await refresh(context, sid)


def report_text(db, sid, preview=False):
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    rows = db.execute("SELECT * FROM entries WHERE session_id=? ORDER BY registered_at,id", (sid,)).fetchall()
    groups = [({"done"}, "✅ أتممن أدوارهن"),
              ({"absent"}, "⏭ غائبات عند النداء"),
              ({"cancelled"}, "❌ ألغين أدوارهن"),
              ({"waiting", "current"}, "⌛ لم تُحسم أدوارهن")]
    started = datetime.fromisoformat(s["created_at"]).astimezone(TZ)
    heading = "🔎 <b>معاينة تقرير الحصة</b>" if preview else "📋 <b>التقرير النهائي للحصة</b>"
    parts = [
        *SCHOOL_HEADER,
        heading,
        FLORAL_DIVIDER,
        f"🎙️ الحصة: <b>{html.escape(s['course'])}</b>",
        f"👩‍🏫 إدارة الجلسة: <b>{html.escape(s['teacher'] or '—')}</b>",
        f"📅 التاريخ: <b>{started.strftime('%d/%m/%Y')}</b>",
        f"🕰 وقت الفتح: <b>{started.strftime('%H:%M')}</b>",
    ]
    for statuses, label in groups:
        names = [r["display_name"] for r in rows if r["status"] in statuses]
        if names:
            section = [f"<b>{label}</b>"]
            if statuses == {"done"}:
                section.extend(f"{i}. <b>{html.escape(name)}</b>" for i, name in enumerate(names, 1))
                parts.extend(["", *section])
            else:
                section.extend(f"{i}. {html.escape(name)}" for i, name in enumerate(names, 1))
                parts.extend(["", *section])
    completed = sum(r["status"] == "done" for r in rows)
    absent = sum(r["status"] == "absent" for r in rows)
    cancelled = sum(r["status"] == "cancelled" for r in rows)
    unresolved = sum(r["status"] in {"waiting", "current"} for r in rows)
    parts += [
        "",
        STAR_DIVIDER,
        "📊 <b>ملخص الأعداد</b>",
        f"👥 إجمالي المسجلات: <b>{len(rows)}</b>",
        f"✅ أتممن أدوارهن: <b>{completed}</b>",
        f"⏭ غائبات عند النداء: <b>{absent}</b>",
        f"❌ ألغين أدوارهن: <b>{cancelled}</b>",
        f"⌛ أدوار غير محسومة: <b>{unresolved}</b>",
    ]
    if preview:
        parts += ["", "راجعي البيانات، ثم اختاري <b>اعتماد التقرير</b> أو <b>العودة للجلسة</b>."]
    return "\n".join(parts)


async def send_report_preview(context, sid):
    with closing(db_connect()) as db:
        s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        text = report_text(db, sid, True)
    await context.bot.send_message(s["chat_id"], text, parse_mode=ParseMode.HTML,
                                   reply_markup=manager_keyboard(sid, "review", bool(s["waitlist_open"])),
                                   **session_scope(s))


async def send_final_report(context, sid):
    with closing(db_connect()) as db:
        s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        text = report_text(db, sid)
    await context.bot.send_message(s["chat_id"], text, parse_mode=ParseMode.HTML, **session_scope(s))


async def set_sticker_command(update, context):
    if not await is_allowed_manager(update, context):
        await deny(update.effective_message); return
    event = (context.args[0].lower() if context.args else "")
    if event not in {"open", "next", "finish"}:
        await update.effective_message.reply_text("الأنواع المتاحة: open أو next أو finish"); return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.sticker:
        await update.effective_message.reply_text("ردّي بهذا الأمر على الملصق المطلوب."); return
    try:
        await context.bot.send_sticker(
            update.effective_chat.id,
            replied.sticker.file_id,
            **thread_kwargs(scope(update)[1]),
        )
    except Forbidden:
        await update.effective_message.reply_text(
            "تعذر إرسال الملصق. تأكدي أن البوت يملك صلاحية إرسال الملصقات في المجموعة.")
        return
    except BadRequest as exc:
        log.exception("رفض تيليغرام الملصق أثناء اختباره")
        await update.effective_message.reply_text(
            f"لم يقبل تيليغرام هذا الملصق، لذلك لم يُحفظ. السبب: {html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return
    with closing(db_connect()) as db:
        db.execute("""INSERT INTO stickers(scope_chat_id,event,file_id) VALUES(0,?,?)
                      ON CONFLICT(scope_chat_id,event) DO UPDATE SET file_id=excluded.file_id""",
                   (event, replied.sticker.file_id)); db.commit()
    await update.effective_message.reply_text("✅ ظهر الملصق التجريبي وحُفظ بنجاح.")



def register_roles(application, *, group=0):
    """تهيئة قاعدة الأدوار وإضافة المعالجات مرة واحدة قبل بدء التطبيق."""
    global ADMIN_IDS, ALLOWED_CHAT_IDS, DB_PATH, TZ, DEFAULT_CAPACITY, TELEGRAM_ADMINS, PANEL_URL, PANEL_API_KEY

    if application.bot_data.get("roles_registered"):
        return

    ADMIN_IDS = read_ids("ROLES_ADMIN_IDS")
    if not ADMIN_IDS:
        raise ValueError("يجب ضبط ROLES_ADMIN_IDS لإدارة الأدوار.")
    ALLOWED_CHAT_IDS = read_ids("ROLES_ALLOWED_CHAT_IDS")
    DB_PATH = Path(os.getenv("ROLES_DB_PATH", "/data/roles.db"))
    TZ = pytz.timezone(os.getenv("ROLES_TIMEZONE", "Asia/Riyadh"))
    DEFAULT_CAPACITY = max(1, int(os.getenv("ROLES_DEFAULT_CAPACITY", "15")))
    TELEGRAM_ADMINS = os.getenv("ROLES_TELEGRAM_ADMINS", "true").strip().lower() not in {"0", "false", "no", "off"}
    PANEL_URL = os.getenv("PANEL_URL", "").strip().rstrip("/")
    PANEL_API_KEY = os.getenv("PANEL_API_KEY", "").strip()
    init_db()

    commands = {
        "roles_open": open_command,
        "roles_name": name_command,
        "roles_limit": limit_command,
        "roles_assign": assign_command,
        "roles_unassign": unassign_command,
        "roles_setsticker": set_sticker_command,
    }
    groups_only = filters.ChatType.GROUPS
    if ALLOWED_CHAT_IDS:
        groups_only = groups_only & filters.Chat(chat_id=ALLOWED_CHAT_IDS)
    for command, handler in commands.items():
        application.add_handler(CommandHandler(command, handler, filters=groups_only), group=group)
    application.add_handler(
        CallbackQueryHandler(callback, pattern=r"^roles:[a-z]+:[0-9]+$"),
        group=group,
    )
    application.bot_data["roles_registered"] = True
