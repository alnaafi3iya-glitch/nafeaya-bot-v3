"""قائمة طالبات الحصة: انضمام الطالبة بزر مرة واحدة، وإغلاق المعلمة للانضمام.

لا تقرأ هذه الوحدة رسائل المجموعات أبدًا؛ لا يُسجَّل انتماء الطالبة إلا بإجراء صريح منها:
الضغط على زر «أنضم إلى طالبات هذه الحصة»، أو أخذ دور في جلسة أدوار (يستدعي roles_module add_member).

الحصة هنا = اسم الدورة (بعد تطبيع المسافات)، وتشترك في قاعدة بيانات الأدوار ROLES_DB_PATH.

البيئة
    CLASS_JOIN_BUTTON=true   يُضاف الزر تحت رسالة تذكير الثلاثين دقيقة (الافتراضي false).
    CLASS_CONTACT_LINK       رابط التواصل مع الإدارة، يظهر في الرسالة المثبّتة بعد الإغلاق إن ضُبط.
    CLASS_MEMBER_DAYS=60     مدة بقاء الانتماء بلا نشاط قبل أن يُحذف.
"""

import asyncio
import hashlib
import html
import logging
import os
import time
from contextlib import closing
from datetime import datetime, timedelta

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import CallbackQueryHandler, CommandHandler, filters

import roles_module as rm

log = logging.getLogger(__name__)

JOIN_LABEL = "📌 أنضم إلى طالبات هذه الحصة"
CLOSE_LABEL = "🔒 إغلاق انضمام الطالبات إلى هذه الحصة"
REOPEN_LABEL = "🔓 إعادة فتح الانضمام إلى هذه الحصة"
CONFIRM_TEXT = ("لن تتمكن طالبات جدد من الانضمام إلى قائمة طالبات هذه الحصة، "
                "وتبقى أسماء المنضمات كما هي. هل تغلقين الانضمام؟")

PIN_OPEN = ("📌 <b>طالبات هذه الحصة</b>\n{course}\n\n"
            "اضغطي الزر مرة واحدة لتُضافي إلى قائمة طالبات الحصة، فتصلك الأدوات الخاصة بها "
            "كالاعتذار عن الحصة ومراسلة الإدارة. هذا لا يعني أخذ دور.")
PIN_CLOSED = "🔒 أُغلق الانضمام إلى قائمة طالبات هذه الحصة.\n{course}"

PANEL_URL = ""
PANEL_API_KEY = ""
CONTACT_LINK = ""
MEMBER_DAYS = 60
BUTTON_ON_REMINDER = False
_teacher_cache = {"at": 0.0, "data": None}
TEACHER_TTL = 60.0


def course_key(course):
    return rm._norm_course(course)


def short_id(course):
    """رمز قصير للدورة داخل callback_data (الحد 64 بايت، والعربية بايتان للحرف)."""
    return hashlib.sha1(course_key(course).encode("utf-8")).hexdigest()[:10]


def init_db():
    with closing(rm.db_connect()) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS class_courses(
          cid TEXT PRIMARY KEY,
          course TEXT NOT NULL,
          teacher TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS class_state(
          course TEXT PRIMARY KEY,
          join_open INTEGER NOT NULL DEFAULT 1,
          changed_by INTEGER,
          changed_at TEXT);
        CREATE TABLE IF NOT EXISTS class_members(
          course TEXT NOT NULL,
          user_id INTEGER NOT NULL,
          display_name TEXT NOT NULL,
          username TEXT,
          source TEXT NOT NULL,
          joined_at TEXT NOT NULL,
          last_active TEXT NOT NULL,
          PRIMARY KEY(course,user_id));
        CREATE TABLE IF NOT EXISTS class_pins(
          chat_id INTEGER NOT NULL,
          thread_id INTEGER NOT NULL,
          course TEXT NOT NULL,
          message_id INTEGER NOT NULL,
          PRIMARY KEY(chat_id,thread_id,course));
        """)
        db.commit()


def remember_course(db, course, teacher=""):
    """يسجّل الدورة ورمزها القصير؛ يعيد الرمز. المعلمة الأحدث تحلّ محل القديمة."""
    key, cid = course_key(course), short_id(course)
    db.execute("""INSERT INTO class_courses(cid,course,teacher) VALUES(?,?,?)
                  ON CONFLICT(cid) DO UPDATE SET teacher=CASE WHEN excluded.teacher<>'' THEN excluded.teacher
                                                              ELSE class_courses.teacher END""",
               (cid, key, (teacher or "").strip()))
    return cid


def join_open(db, course):
    row = db.execute("SELECT join_open FROM class_state WHERE course=?", (course_key(course),)).fetchone()
    return True if row is None else bool(row["join_open"])


def add_member(course, user_id, display_name, username=None, source="button"):
    """يضيف الطالبة إلى قائمة الحصة (أو يجدّد نشاطها). لا يتحقق من الإغلاق: الإغلاق للضغط على الزر فقط،
    أما من أخذت دورًا في جلسة فهي مشاركة فعلًا. أي عطل لا يؤثر في من يستدعيه."""
    try:
        now = rm.now_iso()
        with closing(rm.db_connect()) as db:
            db.execute("""INSERT INTO class_members(course,user_id,display_name,username,source,joined_at,last_active)
                          VALUES(?,?,?,?,?,?,?)
                          ON CONFLICT(course,user_id) DO UPDATE SET display_name=excluded.display_name,
                                 username=excluded.username, last_active=excluded.last_active""",
                       (course_key(course), user_id, display_name or "طالبة", username, source, now, now))
            db.commit()
        return True
    except Exception:
        log.exception("تعذر تسجيل انتماء طالبة")
        return False


def members(course):
    with closing(rm.db_connect()) as db:
        return db.execute("SELECT * FROM class_members WHERE course=? ORDER BY joined_at,user_id",
                          (course_key(course),)).fetchall()


def prune_members(days=None):
    """يحذف الانتماءات التي لم يُجدَّد نشاطها منذ المدة المحددة. يعيد عدد المحذوف."""
    days = MEMBER_DAYS if days is None else days
    cutoff = (datetime.now(rm.TZ) - timedelta(days=days)).isoformat()
    with closing(rm.db_connect()) as db:
        n = db.execute("DELETE FROM class_members WHERE last_active < ?", (cutoff,)).rowcount
        db.commit()
    return n


# ───────────── الأزرار والنصوص ─────────────
def join_markup(course, teacher="", for_pin=False):
    """زر الطالبة (وزر المعلمة في الرسالة المثبّتة). None إن كان الانضمام مغلقًا في رسالة التذكير."""
    with closing(rm.db_connect()) as db:
        cid = remember_course(db, course, teacher)
        is_open = join_open(db, course)
        db.commit()
    rows = []
    if is_open:
        rows.append([InlineKeyboardButton(JOIN_LABEL, callback_data=f"cls:j:{cid}")])
    if for_pin:
        rows.append([InlineKeyboardButton(CLOSE_LABEL if is_open else REOPEN_LABEL,
                                          callback_data=f"cls:{'c' if is_open else 'o'}:{cid}")])
    return InlineKeyboardMarkup(rows) if rows else None


def pin_text(course, is_open):
    body = (PIN_OPEN if is_open else PIN_CLOSED).format(course=f"<b>{html.escape(course_key(course))}</b>")
    if not is_open and CONTACT_LINK:
        body += f"\nللتواصل مع الإدارة: {html.escape(CONTACT_LINK)}"
    return body


# ───────────── صلاحية المعلمة ─────────────
def _fetch_teacher_ids():
    r = requests.get(f"{PANEL_URL}/api/teachers/telegram.json",
                     headers={"Authorization": f"Bearer {PANEL_API_KEY}"}, timeout=15)
    r.raise_for_status()
    data = r.json().get("teachers")
    if not isinstance(data, dict):
        raise ValueError("ردّ اللوحة بلا قائمة معلمات")
    return {rm._norm_course(k): int(v) for k, v in data.items() if v}


async def teacher_telegram_id(name):
    """معرّف تيليغرام المربوط بالمعلمة، أو None. تعذّر اللوحة أو لوحة قديمة = None (فيبقى للإدارة والمشرفات)."""
    if not (PANEL_URL and PANEL_API_KEY and name):
        return None
    now = time.monotonic()
    if _teacher_cache["data"] is None or now - _teacher_cache["at"] > TEACHER_TTL:
        try:
            _teacher_cache["data"] = await asyncio.to_thread(_fetch_teacher_ids)
            _teacher_cache["at"] = now
        except Exception as exc:
            log.warning("تعذر جلب معرّفات المعلمات من اللوحة: %s", exc)
            _teacher_cache["at"] = now - TEACHER_TTL + 5
    return (_teacher_cache["data"] or {}).get(rm._norm_course(name))


async def may_manage(bot, chat_id, user_id, teacher):
    """الإدارة (ROLES_ADMIN_IDS)، أو المعلمة المربوطة بهذه الحصة، أو مشرفة المجموعة في تيليغرام."""
    if user_id in rm.ADMIN_IDS:
        return True
    tid = await teacher_telegram_id(teacher)
    if tid is not None and tid == user_id:
        return True
    return await rm.is_chat_admin(bot, chat_id, user_id)


# ───────────── المعالج ─────────────
async def callback(update, context):
    q = update.callback_query
    try:
        _, action, cid = q.data.split(":", 2)
    except ValueError:
        await q.answer("زر غير صالح.", show_alert=True); return
    chat_id = q.message.chat.id
    if rm.ALLOWED_CHAT_IDS and chat_id not in rm.ALLOWED_CHAT_IDS:
        await q.answer("هذه الميزة غير مفعّلة في هذه المجموعة.", show_alert=True); return
    with closing(rm.db_connect()) as db:
        row = db.execute("SELECT course,teacher FROM class_courses WHERE cid=?", (cid,)).fetchone()
    if not row:
        await q.answer("هذا الزر قديم؛ اطلبي من الإدارة إعادة إرسال الرسالة.", show_alert=True); return
    course, teacher = row["course"], row["teacher"]
    user = q.from_user

    if action == "j":
        with closing(rm.db_connect()) as db:
            if not join_open(db, course):
                await q.answer("أُغلق الانضمام إلى قائمة طالبات هذه الحصة.", show_alert=True); return
            existing = db.execute("SELECT 1 FROM class_members WHERE course=? AND user_id=?",
                                  (course, user.id)).fetchone()
        add_member(course, user.id, user.full_name, user.username, "button")
        await q.answer("أنتِ ضمن طالبات هذه الحصة بالفعل ✅" if existing
                       else f"أُضفتِ إلى طالبات حصة ({course}) ✅", show_alert=True)
        return

    if action not in ("c", "cy", "cn", "o"):
        await q.answer("زر غير صالح.", show_alert=True); return
    if not await may_manage(context.bot, chat_id, user.id, teacher):
        await q.answer("هذا الزر للمعلمة أو الإدارة فقط.", show_alert=True); return

    thread_id = int(getattr(q.message, "message_thread_id", 0) or 0)
    if action == "c":                                      # طلب الإغلاق: تأكيد قبل التنفيذ
        confirm = InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، أغلقي الانضمام", callback_data=f"cls:cy:{cid}"),
                                         InlineKeyboardButton("↩️ تراجع", callback_data=f"cls:cn:{cid}")]])
        await q.answer(CONFIRM_TEXT, show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=confirm)
        except BadRequest:
            pass
        return
    if action == "cn":
        await q.answer("لم يتغير شيء.")
        await _refresh_pin(context.bot, chat_id, thread_id, course, teacher, q.message.message_id)
        return
    opening = action == "o"
    with closing(rm.db_connect()) as db:
        db.execute("""INSERT INTO class_state(course,join_open,changed_by,changed_at) VALUES(?,?,?,?)
                      ON CONFLICT(course) DO UPDATE SET join_open=excluded.join_open,
                             changed_by=excluded.changed_by, changed_at=excluded.changed_at""",
                   (course, 1 if opening else 0, user.id, rm.now_iso()))
        pins = db.execute("SELECT chat_id,thread_id,message_id FROM class_pins WHERE course=?", (course,)).fetchall()
        db.commit()
    await q.answer("أُعيد فتح الانضمام إلى هذه الحصة." if opening else "أُغلق الانضمام إلى هذه الحصة.", show_alert=True)
    targets = {(p["chat_id"], p["thread_id"], p["message_id"]) for p in pins}
    targets.add((chat_id, thread_id, q.message.message_id))
    for c, t, m in targets:
        await _refresh_pin(context.bot, c, t, course, teacher, m)


async def _refresh_pin(bot, chat_id, thread_id, course, teacher, message_id):
    with closing(rm.db_connect()) as db:
        is_open = join_open(db, course)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=pin_text(course, is_open),
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=join_markup(course, teacher, for_pin=True))
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("تعذر تحديث الرسالة المثبّتة: %s", exc)
    except Forbidden:
        log.warning("تعذر تحديث الرسالة المثبّتة (لا صلاحية)")


async def pin_command(update, context):
    """/class_pin [اسم الدورة] في مجموعة أو موضوع: يرسل رسالة «طالبات هذه الحصة» ويثبّتها لكل دورة مرتبطة بالمكان."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user:
        return
    if not (user.id in rm.ADMIN_IDS or await rm.is_chat_admin(context.bot, chat.id, user.id)):
        await msg.reply_text("هذه الأداة خاصة بالإدارة ومشرفات المجموعة.")
        return
    if rm.ALLOWED_CHAT_IDS and chat.id not in rm.ALLOWED_CHAT_IDS:
        await msg.reply_text("هذه الميزة غير مفعّلة في هذه المجموعة.")
        return
    thread_id = int(getattr(msg, "message_thread_id", 0) or 0) if getattr(msg, "is_topic_message", False) else 0
    resolver = context.application.bot_data.get("class_courses_in_dest")
    found = list(await asyncio.to_thread(resolver, chat.id, thread_id)) if resolver else []
    wanted = " ".join(context.args or []).strip()
    if wanted:
        found = [(c, t) for c, t in found if wanted in course_key(c)]
    seen, courses = set(), []
    for c, t in found:
        if course_key(c) not in seen:
            seen.add(course_key(c)); courses.append((course_key(c), t))
    if not courses:
        await msg.reply_text("لا توجد حصة مرتبطة بهذا المكان في الجدول." if not wanted
                             else "لا حصة بهذا الاسم مرتبطة بهذا المكان.")
        return
    kw = {"message_thread_id": thread_id} if thread_id else {}
    for course, teacher in courses:
        with closing(rm.db_connect()) as db:
            is_open = join_open(db, course)
        sent = await context.bot.send_message(chat.id, pin_text(course, is_open), parse_mode=ParseMode.HTML,
                                              reply_markup=join_markup(course, teacher, for_pin=True), **kw)
        with closing(rm.db_connect()) as db:
            db.execute("""INSERT INTO class_pins(chat_id,thread_id,course,message_id) VALUES(?,?,?,?)
                          ON CONFLICT(chat_id,thread_id,course) DO UPDATE SET message_id=excluded.message_id""",
                       (chat.id, thread_id, course, sent.message_id))
            db.commit()
        try:
            await context.bot.pin_chat_message(chat.id, sent.message_id, disable_notification=True)
        except (BadRequest, Forbidden):
            await msg.reply_text("أُرسلت الرسالة لكن تعذّر تثبيتها؛ أعطي البوت صلاحية تثبيت الرسائل ثم ثبّتيها يدويًا.")


def register_students(application, *, courses_in_dest=None, group=0):
    """يُستدعى بعد register_roles. courses_in_dest(chat_id, thread_id) -> [(الدورة، المعلمة)]."""
    global PANEL_URL, PANEL_API_KEY, CONTACT_LINK, MEMBER_DAYS, BUTTON_ON_REMINDER
    if application.bot_data.get("students_registered"):
        return
    PANEL_URL = os.getenv("PANEL_URL", "").strip().rstrip("/")
    PANEL_API_KEY = os.getenv("PANEL_API_KEY", "").strip()
    CONTACT_LINK = os.getenv("CLASS_CONTACT_LINK", "").strip()
    try:
        MEMBER_DAYS = max(1, int(os.getenv("CLASS_MEMBER_DAYS", "60") or 60))
    except ValueError:
        MEMBER_DAYS = 60
    BUTTON_ON_REMINDER = os.getenv("CLASS_JOIN_BUTTON", "").strip().lower() in {"1", "true", "yes", "on"}
    init_db()
    try:
        n = prune_members()
        if n:
            log.info("حُذف %s انتماء بلا نشاط", n)
    except Exception:
        log.exception("تعذر تنظيف الانتماءات القديمة")
    application.bot_data["class_courses_in_dest"] = courses_in_dest
    groups_only = filters.ChatType.GROUPS
    if rm.ALLOWED_CHAT_IDS:
        groups_only = groups_only & filters.Chat(chat_id=rm.ALLOWED_CHAT_IDS)
    application.add_handler(CommandHandler("class_pin", pin_command, filters=groups_only), group=group)
    application.add_handler(CallbackQueryHandler(callback, pattern=r"^cls:[a-z]+:[0-9a-f]{10}$"), group=group)
    application.bot_data["students_registered"] = True
