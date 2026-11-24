"""
بوت المدرسة النافعية لتحفيظ القرآن الكريم
النسخة: 5.0-v3 — قراءة الحصص والملصقات من اللوحة (مع رجوع تلقائي إلى الجدول) + إضافة الأدوار اختيارية

المنطق حسب عمود "نوع العرض" في الشيت:
┌─────────────────┬──────────────────────────────────────────────────────┐
│  نوع العرض      │  السلوك                                              │
├─────────────────┼──────────────────────────────────────────────────────┤
│  متعدد          │  مساءً: شعار+سلام+صورة_غد (11 مساء مكة)             │
│                 │  اليوم: شعار+سلام+صورة_اليوم+رابط (قبل 30 دقيقة)    │
├─────────────────┼──────────────────────────────────────────────────────┤
│  فارغ / صورة   │  السلوك القديم — نص الأوقات الثلاثة + صورة الدورة   │
│                 │  اليوم: صورة الدورة + رابط الغرفة (قبل 30 دقيقة)    │
└─────────────────┴──────────────────────────────────────────────────────┘

أعمدة الشيت:
  الدورة | اليوم | الساعة | الدقيقة | المعلمة | الرواية | المجموعات
  الحالة | رابط الغرفة | صورة الدورة | نوع العرض | صورة_غد | صورة_اليوم | ملاحظة

GitHub Actions cron:
  المسائي (11 مساء مكة):  0 20 * * *
  تذكير اليوم:           */5 * * * *
"""

import os
import sys
import csv
import json
import sqlite3
import pytz
import tzdata
import asyncio
import time
import signal
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, reset_tzpath
from telegram import Bot
try:
    from telegram.error import NetworkError, RetryAfter, TimedOut
except ImportError:  # توافق احتياطي مع نسخ المكتبة القديمة
    class _TelegramTransientError(Exception):
        pass
    NetworkError = RetryAfter = TimedOut = _TelegramTransientError

# ══════════════════════════════════════════════
#  الإعدادات الأساسية
# ══════════════════════════════════════════════

TOKEN    = os.getenv("BOT_TOKEN", "").strip()
SHEET_ID = "12k9GozACQeJH8U3OaXm5qbXbuYLt5-rLAc3c3hSK3HQ"
TZ       = pytz.timezone("Asia/Riyadh")   # UTC+3 — توقيت مكة المكرمة

# اجعل تحويل الأوقات يعتمد على حزمة tzdata المثبتة مع المشروع، لا على
# قاعدة التوقيت الخاصة بنظام Railway التي قد تكون متأخرة عن تغييرات المغرب.
reset_tzpath([])
ZI_RIYADH = ZoneInfo("Asia/Riyadh")
ZI_CAIRO = ZoneInfo("Africa/Cairo")
ZI_CASABLANCA = ZoneInfo("Africa/Casablanca")

# ══════════════════════════════════════════════
#  المجموعات
# ══════════════════════════════════════════════

GROUPS = {
    "معلمات":           -1003413846021,
    "طالبات":           -1003414489597,
    "نافعية":           -1001376738965,
    "تفاعلية":          -1001305707867,
    "مجموعة_قطر":      -1003273007381,
    "مجموعة_شاطبية":   -1002751628160,
    "مجموعة_عاصم":     -1003848182765,
    "مراقبة":           -1003980237292,
    "أصول_رجال":       -1003605199944,
    "أصول_القراءات":   -1003590743246,
    "الإقراء_أشرف أبو المكارم": -1002545783147,
    "تفسير_الزهراوين": -1003853539348,
    "سمير_الطالبين":   -1004306200686,
    "تأهيل_المعلمات":  -1001534094437,
    "أصول_ورش":        -1003457484514,
    "تجويد_مستوى2":    -1002425893383,
    "تسميع_حفص":       -1001867163381,
    "تسميع_ورش2":      -1001546966406,
    "تسميع_ورش1":      -1001766296118,
    "ضبط_الأداء":      -1002792974437,
    "رحلة الإتقان_تجويد": -1003960315040,
}

_STATIC_GROUPS = dict(GROUPS)          # المعرّفة في الكود؛ اللوحة تضيف إليها ولا تغيّرها

def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"قيمة غير صحيحة للمتغير {name}: {value!r}")


def env_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"قيمة غير صحيحة للمتغير {name}: {value!r}") from exc
    if number < 1:
        raise ValueError(f"يجب أن تكون قيمة {name} عدداً موجباً")
    return number


TEST_MODE     = env_bool("TEST_MODE", False)
DRY_RUN       = env_bool("DRY_RUN", False)
FORCE_SEND    = env_bool("FORCE_SEND", False)
SKIP_STICKER_ERRORS = env_bool("SKIP_STICKER_ERRORS", False)   # للبوت التجريبي: معرّفات الملصقات خاصة بكل بوت


def is_bad_media_error(exc) -> bool:
    """BadRequest في مكتبة تيليغرام يرث NetworkError، فنميّزه بالاسم لا بالوراثة."""
    return exc.__class__.__name__ == "BadRequest"
PAUSE_REMINDERS = env_bool("PAUSE_REMINDERS", False)
COURSE_FILTER = os.getenv("COURSE_FILTER", "").strip()
TEST_LIMIT    = env_positive_int("TEST_LIMIT", 1)
INSTANCE_ID   = (
    os.getenv("RAILWAY_DEPLOYMENT_ID")
    or os.getenv("RAILWAY_REPLICA_ID")
    or f"pid-{os.getpid()}"
)[:12]

if FORCE_SEND and not (TEST_MODE or DRY_RUN):
    raise RuntimeError("FORCE_SEND مسموح فقط عندما يكون TEST_MODE=true أو DRY_RUN=true")
if DRY_RUN and not TOKEN:
    TOKEN = "0:DRY_RUN_DUMMY_TOKEN"
print(f"🔧 TEST_MODE = {TEST_MODE}")
print(f"🧩 INSTANCE_ID = {INSTANCE_ID} | pid={os.getpid()}")
print(f"🕰️ tzdata = {tzdata.__version__}")
if TEST_MODE:
    print(f"🧪 تجربة آمنة: الوجهة الوحيدة هي مجموعة المراقبة | FORCE_SEND={FORCE_SEND}")
if DRY_RUN:
    print(f"🧾 DRY_RUN = True | محاكاة فقط دون إرسال تيليغرام | FORCE_SEND={FORCE_SEND}")

FIXED_COURSE_GROUPS = []

# ══════════════════════════════════════════════
#  الملصقات
# ══════════════════════════════════════════════

S_NAFEAYA  = "CAACAgQAAyEFAATLeygFAAO7aeiSOI1sxY-RnijW1QoKTPFe6UUAAswKAAK0ebFQisLk1wqlAjw7BA"
S_SALAM    = "CAACAgQAAyEFAATlXpvtAAICX2no5ixMFj55ezKF3PXVPWIAAchkmAACPwsAAip2WFI9ltWLF3pjtzsE"
S_REMINDER = "CAACAgQAAyEFAATLeygFAAO8aeiTa9PyeAYK5kWKTI0OyGt7G88AAi8JAAJWnllSmxc6G-UneJE7BA"
S_TOMORROW = "CAACAgQAAyEFAATLeygFAAPNaejVBhO7jeglYcU9EAJzqCM3ssIAAlALAAKTVNFQtomA7t7qZPg7BA"
S_TODAY    = "CAACAgQAAyEFAATlXpvtAAICYGno5sKnai01XeYdmKTc3iEGsSVMAAKWCQACbCsAAVP-sZqv_toRtDsE"
S_OPEN     = "CAACAgQAAyEFAATtPZnsAANvaes3DYRH38ri51PKwwdkNdEnIZsAAvQLAAL6YolRsy7lgWDnTzk7BA"
S_HALF_HR  = "CAACAgQAAyEGAATtPZnsAAIGgWqhN264q0tpahoLiIgLGMrdu_JCAAIXEgAC7AMJUf5w7hckGy_RPQQ"
S_EXCUSE   = "CAACAgQAAyEFAATtPZnsAAMNaej_2916xNz5YrgcZuuHJAG1VhUAAnwLAAJWgfBS3ZMj2356YYM7BA"
S_EXCUSE_SHEIKH  = S_EXCUSE     # اعتذار الشيخ؛ يبقى مثل الافتراضي حتى تخصّصه اللوحة
S_EXCUSE_TEACHER = S_EXCUSE     # اعتذار المعلمة

# ملصقات التذكير الصباحي (جديدة، لتفادي تكرار الملصقات مع تذكير الـ30 دقيقة)
S_SALAM2     = "CAACAgQAAxkBAAMZap6k4oa1r2nKgAIqW4F0v03VGR0AAsMmAAK77tFQRIS-yS2_usU9BA"
S_REMINDER2  = "CAACAgQAAxkBAAMfap6uMoLI67Gm_pG2ei7b7VTveXQAAlIkAALC4chQsZbP2jOE3UA9BA"
S_TODAY2     = "CAACAgQAAxkBAAMaap6lJg6TREvsDHsLI1dL0pNlUssAAtYdAAKaZslQu999X3GiCQk9BA"

STICKER_LABELS = {
    S_NAFEAYA: "شعار المدرسة",
    S_SALAM: "السلام",
    S_REMINDER: "تذكير",
    S_TOMORROW: "غداً",
    S_TODAY: "اليوم",
    S_OPEN: "فتح الغرفة",
    S_HALF_HR: "الحلقة بعد نصف ساعة",
    S_EXCUSE: "اعتذار",
    S_SALAM2: "السلام الصباحي",
    S_REMINDER2: "تذكير صباحي",
    S_TODAY2: "اليوم صباحي",
}

_DRY_RUN_CLAIMS: set[str] = set()

AR_MONTHS = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل", 5: "مايو", 6: "يونيو",
    7: "يوليو", 8: "أغسطس", 9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر",
}

RLM       = "\u200f"   # علامة اتجاه خفية تجبر المحاذاة يمين-يسار (لا تظهر كحرف مرئي)
RLE       = "\u202b"   # بداية تضمين اتجاه كامل من اليمين لليسار (للأسطر التي تبدأ برقم)
PDF       = "\u202c"   # نهاية التضمين
LRE       = "\u202a"   # بداية تضمين اتجاه من اليسار لليمين (لحماية تنسيق الأرقام مثل التاريخ من الانعكاس)

# فاصل خاص بالتذكير الصباحي فقط (مختلف عن SEP العام)
SEP_MORNING = RLM + "· · ─ ·ʚɞ· ─ · ·"

BISMILLAH = RLM + "﷽"
SEP       = RLM + "✦•┈๑⋅⋯ ⋯⋅๑┈•✦"

DAY_MAP = {
    "الأحد":    6, "الاثنين":  0, "الثلاثاء": 1,
    "الأربعاء": 2, "الخميس":   3, "الجمعة":   4, "السبت": 5,
}

DAY_NAMES_AR = {
    0: "الاثنين", 1: "الثلاثاء", 2: "الأربعاء",
    3: "الخميس",  4: "الجمعة",   5: "السبت",   6: "الأحد",
}


def normalize_text(value: object) -> str:
    """توحيد الإدخال العربي والمسافات لتفادي إسقاط الصف بسبب اختلاف الكتابة."""
    text = str(value or "").strip()
    for old, new in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ـ", "")):
        text = text.replace(old, new)
    return " ".join(text.split())


NORMALIZED_DAYS = {normalize_text(name): number for name, number in DAY_MAP.items()}
NORMALIZED_GROUPS = {normalize_text(name): (name, cid) for name, cid in GROUPS.items()}


def day_number(value: object):
    return NORMALIZED_DAYS.get(normalize_text(value))


def test_banner(text: str) -> str:
    return text


def sticker_label(sticker: str) -> str:
    return STICKER_LABELS.get(sticker, sticker[:18] + "...")


def preview_text(text: str | None, limit: int = 220) -> str:
    if not text:
        return ""
    compact = " ".join(str(text).split())
    return compact if len(compact) <= limit else compact[:limit] + "..."


# وجهة الإرسال: معرّف مجموعة عادي، أو «مسار» يجمع المجموعة والموضوع (Topic) في عدد صحيح واحد
# (chat_id * 10^9 - thread_id) حتى تبقى بقية الشيفرة (الفرز، مفاتيح منع التكرار) تعمل دون تغيير.
_ROUTE_MUL = 10 ** 9
_ROUTE_MIN = 10 ** 16      # معرّفات تيليغرام الحقيقية أصغر من 10^14


def make_route(chat_id: int, thread_id: int) -> int:
    return chat_id * _ROUTE_MUL - thread_id


def split_route(dest: int) -> tuple[int, int | None]:
    """يفكّ الوجهة إلى (chat_id، thread_id أو None)."""
    if abs(dest) < _ROUTE_MIN:
        return dest, None
    thread = (-dest) % _ROUTE_MUL
    return (dest + thread) // _ROUTE_MUL, thread


def dest_kwargs(dest: int) -> dict:
    chat_id, thread_id = split_route(dest)
    return {"chat_id": chat_id, **({"message_thread_id": thread_id} if thread_id else {})}


def fmt_dest(dest: int) -> str:
    chat_id, thread_id = split_route(dest)
    return f"{chat_id}#{thread_id}" if thread_id else str(chat_id)


async def send_text(bot: Bot, chat_id: int, text: str, markup=None):
    if DRY_RUN:
        print(f"🧾 [DRY_RUN] نص → chat_id={fmt_dest(chat_id)} | {preview_text(text)}")
        return None
    return await telegram_retry(
        lambda: bot.send_message(text=test_banner(text), **({"reply_markup": markup} if markup else {}), **dest_kwargs(chat_id)),
        "إرسال النص",
    )


async def send_sticker(bot: Bot, chat_id: int, sticker: str):
    if DRY_RUN:
        print(f"🧾 [DRY_RUN] ملصق → chat_id={fmt_dest(chat_id)} | {sticker_label(sticker)}")
        return None
    return await telegram_retry(
        lambda: bot.send_sticker(sticker=sticker, **dest_kwargs(chat_id)),
        f"إرسال ملصق {sticker_label(sticker)}",
    )


async def send_photo(bot: Bot, chat_id: int, photo: str, caption=None, markup=None):
    if TEST_MODE and caption:
        caption = test_banner(caption)
    if DRY_RUN:
        print(
            f"🧾 [DRY_RUN] صورة → chat_id={fmt_dest(chat_id)} | photo={photo!r}"
            + (f" | caption={preview_text(caption)}" if caption else "")
        )
        return None
    return await telegram_retry(
        lambda: bot.send_photo(photo=photo, caption=caption, **({"reply_markup": markup} if markup else {}), **dest_kwargs(chat_id)),
        "إرسال الصورة",
    )


async def telegram_retry(operation, label: str, attempts: int = 3):
    """إعادة المحاولة فقط لأخطاء Telegram المؤقتة؛ لا نكرر الأخطاء الدائمة."""
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            # Telegram may have delivered a message before reporting a timeout.
            transient = isinstance(exc, RetryAfter)
            if not transient or attempt == attempts:
                raise
            retry_after = getattr(exc, "retry_after", 0) or 0
            delay = max(float(retry_after), float(2 ** (attempt - 1)))
            print(f"⚠️ {label} تعذر مؤقتاً؛ المحاولة {attempt + 1}/{attempts} بعد {delay:g} ث")
            await asyncio.sleep(delay)


async def send_photo_or_fallback(
    bot: Bot, chat_id: int, photo: str, *, caption: str | None, fallback_text: str, markup=None
):
    """الصورة هي الأصل، والنص لا يُرسل إلا إن غابت الصورة أو فشل إرسالها."""
    if DRY_RUN:
        if photo:
            print(
                f"🧾 [DRY_RUN] صورة أو بديل → سيُرسل الصورة | chat_id={fmt_dest(chat_id)} | "
                f"photo={photo!r}" + (f" | caption={preview_text(caption)}" if caption else "")
            )
            return None
        print(f"🧾 [DRY_RUN] صورة أو بديل → لا توجد صورة، سيُرسل النص البديل | chat_id={fmt_dest(chat_id)}")
        return await send_text(bot, chat_id, fallback_text)
    if photo:
        try:
            return await send_photo(bot, chat_id, photo, caption=caption, markup=markup)
        except Exception as exc:
            if isinstance(exc, NetworkError) and not (SKIP_STICKER_ERRORS and is_bad_media_error(exc)):
                raise  # Delivery is uncertain; a fallback would duplicate it.
            print(f"⚠️ تعذر إرسال الصورة؛ أُرسل البديل النصي: {exc}")
    return await send_text(bot, chat_id, fallback_text, markup=markup)


async def report_failure(bot: Bot, message: str):
    """إبلاغ المراقبة دون إخفاء فشل التشغيل عن GitHub Actions."""
    if DRY_RUN:
        print(f"🧾 [DRY_RUN] تنبيه فشل إلى المراقبة | {preview_text(message)}")
        return
    try:
        await bot.send_message(
            chat_id=GROUPS["مراقبة"],
            text=f"🚨 تعطل بوت التذكير\n\n{message[:3500]}",
        )
    except Exception as notify_error:
        print(f"❌ تعذر إرسال تنبيه المراقبة: {notify_error}")


def course_selected(lesson: dict) -> bool:
    if not COURSE_FILTER:
        return True
    return normalize_text(COURSE_FILTER) in normalize_text(lesson.get("الدورة", ""))


# حصص دائمة (ضبط الأداء / تسميع حفص / تسميع ورش1 / تسميع ورش2) — ليست دورات
# بوقت محدد، فلا تدخل في التذكير الصباحي، وتُعرض في بقية التذكيرات باسم مبسّط.
STANDING_SESSION_PREFIXES = ("ضبط", "تسميع")
# دورات دائمة أيضاً لكن اسمها لا يبدأ بأحد البادئات أعلاه — تُستثنى من التذكير
# الصباحي فقط بالاسم الكامل (لا بالاحتواء الجزئي)، حتى لا يُستثنى معها بالخطأ
# اسم مشابه لكنه مختلف مثل "تفسير الزهراوين". وتبقى ضمن تذكير المساء وتذكير
# اليوم كالمعتاد.
STANDING_SESSION_EXACT_NAMES = ("حفظ الزهراوين",)


def is_standing_session(lesson: dict) -> bool:
    name = normalize_text(lesson.get("الدورة", ""))
    if any(name.startswith(p) for p in STANDING_SESSION_PREFIXES):
        return True
    return any(name == normalize_text(term) for term in STANDING_SESSION_EXACT_NAMES)


def display_course_label(course_name: str) -> str:
    name = normalize_text(course_name)
    if name.startswith("ضبط"):
        return "حصص الضبط"
    if name.startswith("تسميع"):
        return "حصص التسميع"
    if name == normalize_text("حفظ الزهراوين"):
        return "حصص حفظ الزهراوين"
    return f"دورة {course_name}"

# ══════════════════════════════════════════════
#  قراءة البيانات من Google Sheets
# ══════════════════════════════════════════════

def get_lessons_from_sheet() -> list[dict]:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        r.encoding = "utf-8"
        reader  = csv.DictReader(r.text.splitlines())
        if not reader.fieldnames:
            raise ValueError("الشيت فارغ أو لا يحتوي على صف العناوين")
        cleaned = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]
        required = {"الدورة", "اليوم", "الساعة", "الدقيقة", "الحالة", "المجموعات"}
        missing = required - {name.strip() for name in reader.fieldnames if name}
        if missing:
            raise ValueError(f"أعمدة إلزامية مفقودة: {', '.join(sorted(missing))}")
        active  = [row for row in cleaned if row.get("الحالة", "") in ("نشط", "اعتذار")]
        print(f"📋 الحصص النشطة/اعتذار: {len(active)}")
        return active
    except Exception as e:
        raise RuntimeError(f"تعذر قراءة جدول الحصص: {e}") from e


# ══════════════════════════════════════════════
#  مصدر الحصص والملصقات: لوحة الإدارة أولاً، والجدول عند الغياب أو العطل
#  الرجوع إلى الوضع القديم بحذف PANEL_URL وPANEL_API_KEY.
# ══════════════════════════════════════════════

PANEL_URL     = os.getenv("PANEL_URL", "").strip().rstrip("/")
PANEL_API_KEY = os.getenv("PANEL_API_KEY", "").strip()


PANEL_CACHE_FILE = os.getenv("PANEL_CACHE_FILE", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "panel_lessons_cache.json")
PANEL_CACHE_MAX_AGE_HOURS = float(os.getenv("PANEL_CACHE_MAX_AGE_HOURS", "6") or 6)
# الرجوع إلى الجدول القديم (Google Sheet) عند تعذّر اللوحة مغلق افتراضيًا: ذلك الجدول لا يعرف اعتذارات اللوحة
# ولا تغيير المواعيد ولا الدورات المؤرشفة، فقد يرسل تذكيرًا خاطئًا. فعّليه بـ SHEET_FALLBACK=true إن أردتِه.
SHEET_FALLBACK = env_bool("SHEET_FALLBACK", False)


def _is_active(row: dict) -> bool:
    return row.get("الحالة", "") in ("نشط", "اعتذار")


def _save_panel_cache(cleaned: list[dict]) -> None:
    try:
        tmp = PANEL_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"saved_at": time.time(), "lessons": cleaned}, fh, ensure_ascii=False)
        os.replace(tmp, PANEL_CACHE_FILE)
    except Exception as exc:                       # الذاكرة المؤقتة تحسين فقط؛ فشلها لا يمنع التذكير
        print(f"⚠️ تعذّر حفظ نسخة الحصص المؤقتة: {exc}")


def _load_panel_cache():
    """يعيد (الحصص النشطة، عمر النسخة بالدقائق) أو None إن لم توجد نسخة صالحة وحديثة."""
    try:
        with open(PANEL_CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        age_min = (time.time() - float(data["saved_at"])) / 60
        rows = data["lessons"]
        if not isinstance(rows, list) or age_min > PANEL_CACHE_MAX_AGE_HOURS * 60:
            return None
        return [r for r in rows if _is_active(r)], int(age_min)
    except Exception:
        return None


def get_lessons_from_panel() -> list[dict]:
    last_exc: Exception | None = None
    for attempt, wait in enumerate((0, 2, 5), start=1):       # ثلاث محاولات قبل اعتبار اللوحة متعذّرة
        if wait:
            time.sleep(wait)
        try:
            r = requests.get(
                f"{PANEL_URL}/api/lessons.json",
                headers={"Authorization": f"Bearer {PANEL_API_KEY}"},
                timeout=15,
            )
            r.raise_for_status()
            rows = r.json().get("lessons")
            if not isinstance(rows, list):
                raise ValueError("ردّ اللوحة لا يحتوي قائمة حصص")
            break
        except Exception as exc:
            last_exc = exc
            print(f"⚠️ محاولة {attempt}/3 لقراءة اللوحة فشلت: {exc}")
    else:
        raise last_exc  # type: ignore[misc]
    cleaned = [{str(k).strip(): ("" if v is None else str(v)).strip() for k, v in row.items()} for row in rows]
    _save_panel_cache(cleaned)
    active = [row for row in cleaned if _is_active(row)]
    print(f"📋 الحصص النشطة/اعتذار: {len(active)}")
    return active


# أدوار الملصقات التي يمكن تغييرها من اللوحة (أسماء الثوابت S_* أعلاه)
STICKER_ROLES = {
    "NAFEAYA": "شعار المدرسة", "SALAM": "السلام", "HALF_HR": "الحلقة بعد نصف ساعة", "OPEN": "فتح الغرفة",
    "EXCUSE": "اعتذار", "REMINDER": "تذكير", "TOMORROW": "غداً", "TODAY": "اليوم",
    "EXCUSE_SHEIKH": "اعتذار الشيخ", "EXCUSE_TEACHER": "اعتذار المعلمة",
}
_DEFAULT_STICKERS = {k: globals()["S_" + k] for k in STICKER_ROLES}

# ملصقات خاصة بدورة بعينها من اللوحة: {اسم الدورة المطبّع: {الدور: معرّف}}. الشعار ثابت دائمًا؛ و«اليوم» عام (تذكير صباحي مشترك).
COURSE_STICKER_ROLES = ("SALAM", "HALF_HR", "OPEN", "EXCUSE_SHEIKH", "EXCUSE_TEACHER", "EXCUSE", "REMINDER", "TOMORROW")
COURSE_STICKERS: dict[str, dict[str, str]] = {}


def sticker_for(role: str, course: str | None = None) -> str:
    """ملصق الدور؛ وإن كان للدورة ملصق خاص في هذا الدور فهو الأولى، وإلا فالعام (المعدَّل من اللوحة أو المضمّن)."""
    if course and role in COURSE_STICKER_ROLES:
        fid = COURSE_STICKERS.get(normalize_text(course), {}).get(role)
        if fid:
            return fid
    return globals()["S_" + role]


def apply_panel_stickers() -> None:
    """يقرأ ملصقات اللوحة المفعّلة ويطبّقها؛ ما لم تخصّصه اللوحة يبقى على ملصقه المضمّن. أي عطل يترك الحالية."""
    try:
        r = requests.get(
            f"{PANEL_URL}/api/stickers.json",
            headers={"Authorization": f"Bearer {PANEL_API_KEY}"},
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        custom = body.get("stickers") or {}
        raw_course = body.get("course_stickers") or {}
    except Exception as exc:
        print(f"⚠️ تعذرت قراءة ملصقات اللوحة ({exc})؛ تبقى الملصقات الحالية")
        return
    per_course = {}
    if isinstance(raw_course, dict):
        for cname, roles in raw_course.items():
            if not isinstance(roles, dict):
                continue
            ok = {k: v.strip() for k, v in roles.items()
                  if k in COURSE_STICKER_ROLES and isinstance(v, str) and v.strip()}
            if ok:
                per_course[normalize_text(cname)] = ok
                for k, v in ok.items():
                    STICKER_LABELS[v] = f"{STICKER_ROLES[k]} ({cname})"
    if per_course != COURSE_STICKERS:
        print(f"🎭 ملصقات خاصة بالدورات: {', '.join(sorted(per_course)) or 'لا شيء'}")
    COURSE_STICKERS.clear()
    COURSE_STICKERS.update(per_course)
    changed = []
    for role, default in _DEFAULT_STICKERS.items():
        fid = custom.get(role)
        if role in ("EXCUSE_SHEIKH", "EXCUSE_TEACHER"):
            general = custom.get("EXCUSE")      # الملصق العام للاعتذار احتياط إن لم يُخصَّص الشيخ/المعلمة
            default = general.strip() if isinstance(general, str) and general.strip() else _DEFAULT_STICKERS["EXCUSE"]
        value = fid.strip() if isinstance(fid, str) and fid.strip() else default
        if globals()["S_" + role] != value:
            changed.append(STICKER_ROLES[role])
        globals()["S_" + role] = value
    for role, label in STICKER_ROLES.items():
        STICKER_LABELS[globals()["S_" + role]] = label
    if changed:
        print(f"🎭 ملصقات مخصّصة من اللوحة: {', '.join(changed)}")


def _panel_headers() -> dict:
    return {"Authorization": f"Bearer {PANEL_API_KEY}"}


def apply_panel_groups() -> None:
    """يضيف إلى GROUPS المجموعات التي اعتمدتها الإدارة في اللوحة. المعرّف في الكود يغلب دائمًا على اللوحة
    (حتى لا تُحوَّل مجموعة المراقبة مثلًا بخطأ)، وأي عطل يترك القائمة الحالية كما هي."""
    try:
        r = requests.get(f"{PANEL_URL}/api/groups.json", headers=_panel_headers(), timeout=15)
        r.raise_for_status()
        body = r.json()
        groups = body.get("groups")
        topics = body.get("topics") or {}
    except Exception as exc:
        print(f"⚠️ تعذرت قراءة مجموعات اللوحة ({exc})؛ تبقى المجموعات الحالية")
        return
    if not isinstance(groups, dict):
        return
    if isinstance(topics, dict):          # موضوع (Topic) داخل مجموعة: يُعامَل كوجهة باسمه، ويُرسَل إليه بمعرّف الموضوع
        for name, t in topics.items():
            cid, tid = (t.get("chat_id"), t.get("thread_id")) if isinstance(t, dict) else (None, None)
            if (isinstance(name, str) and name.strip() and isinstance(cid, int) and isinstance(tid, int)
                    and not isinstance(cid, bool) and not isinstance(tid, bool) and tid > 0):
                groups[name] = make_route(cid, tid)
    added = []
    for name, cid in groups.items():
        if not (isinstance(name, str) and name.strip() and isinstance(cid, int) and not isinstance(cid, bool)):
            continue
        if name in _STATIC_GROUPS and _STATIC_GROUPS[name] != cid:
            print(f"⚠️ اللوحة تعرّف «{name}» بمعرّف مختلف عن الكود؛ يُعتمد ما في الكود")
            continue
        if name not in GROUPS:
            added.append(name)
        GROUPS[name] = cid
    NORMALIZED_GROUPS.clear()
    NORMALIZED_GROUPS.update({normalize_text(n): (n, c) for n, c in GROUPS.items()})
    if added:
        print(f"💬 مجموعات من اللوحة: {', '.join(added)}")


_groups_seeded = False


def seed_panel_groups_once() -> None:
    """ينقل المجموعات المعرّفة في الكود إلى اللوحة (معتمدة) مرة واحدة لكل تشغيل؛ لا يكتب فوق ما فيها."""
    global _groups_seeded
    if _groups_seeded or DRY_RUN:
        return
    try:
        r = requests.post(f"{PANEL_URL}/api/groups/seed", json={"groups": _STATIC_GROUPS},
                          headers=_panel_headers(), timeout=15)
        r.raise_for_status()
        _groups_seeded = True
        res = r.json()
        print(f"💬 نُقلت مجموعات الكود إلى اللوحة: جديد {res.get('added')}، موجود {res.get('skipped')}")
    except Exception as exc:
        print(f"⚠️ تعذر نقل المجموعات إلى اللوحة الآن ({exc}); ستعاد المحاولة")


def post_group_to_panel(chat_id: int, title: str, active: bool = True) -> None:
    r = requests.post(f"{PANEL_URL}/api/groups/discover",
                      json={"chat_id": chat_id, "title": title or "", "active": active},
                      headers=_panel_headers(), timeout=15)
    r.raise_for_status()


async def _send_group_with_retry(chat_id: int, title: str, active: bool) -> bool:
    for delay in (0, 3, 10, 30):
        if delay:
            await asyncio.sleep(delay)
        try:
            await asyncio.to_thread(post_group_to_panel, chat_id, title, active)
            return True
        except Exception as exc:
            print(f"⚠️ تعذر إبلاغ اللوحة بالمجموعة {chat_id} ({exc})")
    return False


async def on_bot_chat_member(update, context) -> None:
    """يُستدعى حين يُضاف البوت إلى مجموعة أو يُزال منها، فتظهر المجموعة في اللوحة بانتظار الاعتماد."""
    m = update.my_chat_member
    if m:
        print(f"📥 حدث عضوية البوت: {m.chat.type} | {m.chat.id} | {m.chat.title or ''} | "
              f"{m.old_chat_member.status} → {m.new_chat_member.status}")
    if not m or m.chat.type not in ("group", "supergroup", "channel"):
        return
    status = m.new_chat_member.status
    if status in ("member", "administrator", "creator"):
        active = True
    elif status in ("left", "kicked"):
        active = False
    else:
        return
    ok = await _send_group_with_retry(m.chat.id, m.chat.title or "", active)
    print(f"{'✅ أُبلغت اللوحة' if ok else '❌ تعذر إبلاغ اللوحة'} بالمجموعة {m.chat.id}")


_topic_last_post: dict[tuple[int, int], tuple[float, str]] = {}
TOPIC_REPOST_SECONDS = 600     # لا نكرر إبلاغ اللوحة بالموضوع نفسه أكثر من مرة كل 10 دقائق
TOPIC_RETRY_SECONDS = 60       # بعد فشل الاتصال


def post_topic_to_panel(chat_id: int, thread_id: int, title: str) -> None:
    r = requests.post(f"{PANEL_URL}/api/topics/discover",
                      json={"chat_id": chat_id, "thread_id": thread_id, "title": title or ""},
                      headers=_panel_headers(), timeout=15)
    r.raise_for_status()


def _topic_title(msg) -> str:
    """عنوان الموضوع إن حملته الرسالة: رسالة إنشاء الموضوع نفسها، أو الرد الافتراضي على رسالة إنشائه."""
    for m in (msg, getattr(msg, "reply_to_message", None)):
        created = getattr(m, "forum_topic_created", None) if m else None
        name = getattr(created, "name", "") if created else ""
        if name:
            return str(name)
    return ""


async def on_topic_message(update, context) -> None:
    """أي رسالة داخل موضوع من مجموعة: يُبلَغ بها اللوحة ليظهر الموضوع بانتظار الاعتماد (للمجموعات المعتمدة فقط)."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type != "supergroup":
        return
    thread_id = getattr(msg, "message_thread_id", None)
    if not thread_id or not getattr(msg, "is_topic_message", False):
        return
    key, title = (chat.id, int(thread_id)), _topic_title(msg)
    last = _topic_last_post.get(key)
    now = time.monotonic()
    if last and now - last[0] < TOPIC_REPOST_SECONDS and (last[1] or not title):
        return
    _topic_last_post[key] = (now - (TOPIC_REPOST_SECONDS - TOPIC_RETRY_SECONDS), title)   # يُعاد بعد دقيقة إن فشل
    try:
        await asyncio.to_thread(post_topic_to_panel, chat.id, int(thread_id), title)
        _topic_last_post[key] = (now, title)
    except Exception as exc:
        print(f"⚠️ تعذر إبلاغ اللوحة بالموضوع {chat.id}#{thread_id} ({exc})")


TEACHER_PLACES = env_bool("TEACHER_PLACES", False)      # اكتشاف أماكن الحصص: معطّل حتى يُفعَّل صراحة
SEEN_REPOST_SECONDS = 600
_seen_last: dict[tuple[int, int, int], float] = {}


def post_teacher_seen(telegram_id: int, chat_id: int, thread_id: int, ts: str) -> None:
    r = requests.post(f"{PANEL_URL}/api/teachers/seen",
                      json={"telegram_id": telegram_id, "chat_id": chat_id, "thread_id": thread_id, "ts": ts},
                      headers=_panel_headers(), timeout=15)
    r.raise_for_status()


async def on_teacher_seen(update, context) -> None:
    """رسالة في مجموعة/موضوع: يُبلَغ بها اللوحة (المعرّف والمكان والوقت فقط، دون النص) لتقرر هل هي معلمة مربوطة أثناء حصتها."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or user.is_bot or chat.type not in ("group", "supergroup"):
        return
    thread_id = int(getattr(msg, "message_thread_id", 0) or 0) if getattr(msg, "is_topic_message", False) else 0
    key, now = (user.id, chat.id, thread_id), time.monotonic()
    if now - _seen_last.get(key, -SEEN_REPOST_SECONDS) < SEEN_REPOST_SECONDS:
        return
    _seen_last[key] = now
    ts = (msg.date or datetime.now(timezone.utc)).isoformat()
    try:
        await asyncio.to_thread(post_teacher_seen, user.id, chat.id, thread_id, ts)
    except Exception as exc:
        _seen_last.pop(key, None)
        print(f"⚠️ تعذر إبلاغ اللوحة بنشاط معلمة ({exc})")


async def start_command(update, context) -> None:
    """/start في الخاص. برمز معلمة (من رابطها الخاص): تربط اللوحة حسابها وتردّ عليها برسالة الاعتذار الجاهزة."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not chat or chat.type != "private" or not user or not msg:
        return
    arg = (context.args[0] if getattr(context, "args", None) else "").strip()
    if not arg:
        await msg.reply_text("السلام عليكم ورحمة الله وبركاته. هذا بوت المدرسة النافعية. "
                             "إن كانت الإدارة أرسلت لكِ رابط ربط فاضغطيه من جديد.")
        return
    try:
        r = await asyncio.to_thread(
            lambda: requests.post(f"{PANEL_URL}/api/teachers/link", json={"start": arg, "telegram_id": user.id},
                                  headers=_panel_headers(), timeout=15))
    except Exception as exc:
        print(f"⚠️ تعذر ربط معلمة: {exc}")
        await msg.reply_text("تعذر الاتصال بالنظام الآن، أعيدي الضغط على الرابط بعد قليل.")
        return
    if r.status_code == 404:
        await msg.reply_text("هذا الرابط غير صالح أو انتهت صلاحيته. اطلبي من الإدارة رابطًا جديدًا.")
        return
    if r.status_code != 200:
        print(f"⚠️ ربط المعلمة رُفض: HTTP {r.status_code}")
        await msg.reply_text("تعذر إتمام الربط الآن، أعيدي المحاولة بعد قليل أو راسلي الإدارة.")
        return
    data = r.json()
    print(f"🔗 رُبط حساب تيليغرام بالمعلمة: {data.get('name')}")
    await msg.reply_text("تم ربط حسابك بالبوت ✅\n\n" + data.get("message", ""), disable_web_page_preview=True)


async def test_dest_command(update, context) -> None:
    """/test_dest اسم: يرسل رسالة تجربة إلى وجهة (موضوع أو مجموعة) باسمها المعتمد في اللوحة، لاختبار مسار الإرسال.
    للمعرّفات في ROLES_ADMIN_IDS فقط، وفي وضع التجربة TEST_MODE فقط، ولا يرسل إلا إلى وجهة داخل المجموعة التي كُتب فيها الأمر."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not chat or chat.type not in ("group", "supergroup") or not user or not msg or not TEST_MODE:
        return
    allowed = {int(v.strip()) for v in os.getenv("ROLES_ADMIN_IDS", "").replace("،", ",").split(",") if v.strip().lstrip("-").isdigit()}
    if user.id not in allowed:
        return
    name = " ".join(getattr(context, "args", None) or []).strip()
    if not name:
        await msg.reply_text("اكتبي اسم الموضوع بعد الأمر، مثل: /test_dest أم_سلمة")
        return
    await asyncio.to_thread(apply_panel_groups)                    # أحدث ما اعتُمد في اللوحة
    found = NORMALIZED_GROUPS.get(normalize_text(name))
    if not found:
        await msg.reply_text("لا توجد وجهة بهذا الاسم؛ اعتمديها من «المجموعات» في اللوحة أولًا.")
        return
    label, dest = found
    if split_route(dest)[0] != chat.id:
        await msg.reply_text("هذه الوجهة في مجموعة أخرى؛ اكتبي الأمر داخل مجموعتها.")
        return
    try:
        await send_text(context.bot, dest, f"رسالة تجربة إلى «{label}» ✅")
        await msg.reply_text(f"أُرسلت رسالة التجربة إلى «{label}»" + (" (داخل الموضوع)." if split_route(dest)[1] else "."))
    except Exception as exc:
        await msg.reply_text(f"تعذر الإرسال: {exc}")


async def register_group_command(update, context) -> None:
    """/register_group داخل مجموعة: تسجيلها في اللوحة يدويًا (للمجموعات التي أُضيف إليها البوت قبل هذه الميزة)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return
    allowed = {int(v.strip()) for v in os.getenv("ROLES_ADMIN_IDS", "").replace("،", ",").split(",") if v.strip().lstrip("-").isdigit()}
    if user.id not in allowed:
        return
    ok = await _send_group_with_retry(chat.id, chat.title or "", True)
    await msg.reply_text("أُرسلت المجموعة إلى اللوحة، اعتمديها من «المجموعات»." if ok
                         else "تعذر الاتصال باللوحة الآن، أعيدي المحاولة بعد قليل.")


def get_lessons() -> list[dict]:
    if PANEL_URL and PANEL_API_KEY:
        try:
            rows = get_lessons_from_panel()
            print("🧭 مصدر الحصص: لوحة الإدارة")
            apply_panel_stickers()
            seed_panel_groups_once()
            apply_panel_groups()
            return rows
        except Exception as exc:
            cached = _load_panel_cache()
            if cached:
                rows, age = cached
                print(f"⚠️ تعذرت قراءة اللوحة ({exc})؛ استعمال آخر نسخة سليمة منها قبل {age} دقيقة")
                return rows
            if not SHEET_FALLBACK:
                raise RuntimeError(f"تعذرت قراءة اللوحة ولا توجد نسخة سليمة حديثة منها: {exc}") from exc
            print(f"⚠️ تعذرت قراءة اللوحة ({exc})؛ الرجوع إلى الجدول القديم (SHEET_FALLBACK)")
    return get_lessons_from_sheet()


def parse_groups(groups_str: str) -> list[tuple[str, int]]:
    result = []
    for name in groups_str.replace("،", ",").split(","):
        name = name.strip()
        matched = NORMALIZED_GROUPS.get(normalize_text(name))
        if matched and matched[1]:
            result.append(matched)
        elif name:
            print(f"⚠️ مجموعة غير معرّفة في الكود: {name}")
    return result


def get_all_course_chat_ids(lesson: dict) -> list[tuple[str, int]]:
    if TEST_MODE:
        return [("مراقبة", GROUPS["مراقبة"])]
    seen:  set[int]              = set()
    chats: list[tuple[str, int]] = []
    for name, cid in parse_groups(lesson.get("المجموعات", "")):
        if cid not in seen:
            chats.append((name, cid)); seen.add(cid)
    for name in FIXED_COURSE_GROUPS:
        cid = GROUPS.get(name)
        if cid and cid not in seen:
            chats.append((name, cid)); seen.add(cid)
    return chats


def get_session_chat_ids(sessions: list[dict]) -> list[tuple[str, int]]:
    """اتحاد وجهات كل حصص الدورة، بدل الاعتماد على وجهات الصف الأول."""
    seen: set[int] = set()
    chats: list[tuple[str, int]] = []
    for session in sessions:
        for name, cid in get_all_course_chat_ids(session):
            if cid not in seen:
                chats.append((name, cid))
                seen.add(cid)
    return chats


def course_route_key(lesson: dict) -> tuple:
    """مفتاح الدورة ووجهاتها؛ يُستخدم لتجميع صفوف الدورة نفسها."""
    return (
        normalize_text(lesson.get("الدورة", "")),
        tuple(sorted(cid for _, cid in get_all_course_chat_ids(lesson))),
    )


def weekly_session_signature(lesson: dict) -> tuple:
    """تمييز الحصة الأسبوعية دون عدّ الصف المكرر كحصة جديدة."""
    try:
        h, m = parse_lesson_time(lesson)
    except ValueError:
        h = str(lesson.get("الساعة", "")).strip()
        m = str(lesson.get("الدقيقة", "")).strip()
    return (
        day_number(lesson.get("اليوم", "")),
        h,
        m,
        normalize_text(lesson.get("المعلمة", "")),
        normalize_text(lesson.get("الرواية", "")),
    )


def merge_same_day_reminder_rows(lessons: list[dict]) -> list[dict]:
    """دمج صفوف تذكير اليوم التي لها نفس الدورة والوقت والوجهات."""
    grouped: dict[tuple, list[dict]] = {}
    passthrough: list[dict] = []
    for lesson in lessons:
        try:
            h, m = parse_lesson_time(lesson)
        except ValueError:
            passthrough.append(lesson)
            continue
        key = (
            day_number(lesson.get("اليوم", "")),
            h,
            m,
            normalize_text(lesson.get("الدورة", "")),
            tuple(sorted(cid for _, cid in get_all_course_chat_ids(lesson))),
            normalize_text(lesson.get("الحالة", "")),
            is_multi(lesson),
        )
        grouped.setdefault(key, []).append(lesson)

    merged: list[dict] = []
    for rows in grouped.values():
        if len(rows) == 1:
            merged.append(rows[0])
            continue

        base = dict(rows[0])
        teacher_lines = []
        for row in rows:
            teacher = row.get("المعلمة", "").strip()
            riwaya = row.get("الرواية", "").strip()
            if not teacher and not riwaya:
                continue
            teacher_lines.append(f"{teacher}{' — ' + riwaya if riwaya else ''}".strip())
        if teacher_lines:
            base["المعلمة"] = "\n👑 ".join(dict.fromkeys(teacher_lines))
            base["الرواية"] = ""

        for field in ("رابط الغرفة", "صورة الدورة", "صورة_اليوم", "صورة_غد"):
            base[field] = next((row.get(field, "").strip() for row in rows if row.get(field, "").strip()), base.get(field, ""))

        try:
            h, m = parse_lesson_time(base)
            print(f"🔗 دمج تذكير اليوم | {base.get('الدورة', '').strip()} | {h}:{str(m).zfill(2)} | {len(rows)} صفوف")
        except ValueError:
            pass
        merged.append(base)

    return passthrough + merged


def day_delivery_key(lesson: dict, chat_id: int, h: int, m: int) -> tuple:
    """مفتاح تذكير اليوم: لا يعتمد على المعلمة/الرواية حتى لا يتكرر صف واحد منطقي."""
    return (
        "day",
        chat_id,
        day_number(lesson.get("اليوم", "")),
        h,
        m,
        normalize_text(lesson.get("الدورة", "")),
        normalize_text(lesson.get("الحالة", "")),
    )


async def audit_course_routes(bot: Bot, lessons: list[dict], course: str = "سمير الطالبين"):
    """يطبع الوجهات الفعلية لدورة محددة دون إرسال أي رسالة."""
    wanted = normalize_text(course)
    matching = [
        lesson for lesson in lessons
        if normalize_text(lesson.get("الدورة", "")) == wanted
    ]
    if not matching:
        print(f"🔎 فحص الوجهات: لم يُعثر على دورة {course!r} في الصفوف النشطة/الاعتذار")
        return

    seen: set[tuple[str, int]] = set()
    print(f"🔎 فحص وجهات الدورة: {course}")
    for lesson in matching:
        raw_groups = lesson.get("المجموعات", "").strip()
        print(f"   📄 المجموعات في الشيت: {raw_groups}")
        for alias, chat_id in get_all_course_chat_ids(lesson):
            key = (alias, chat_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                chat = await bot.get_chat(chat_id)
                actual_title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or "بدون اسم"
                chat_type = getattr(chat, "type", "غير معروف")
                print(
                    f"   ✅ الاسم في الكود={alias} | chat_id={chat_id} | "
                    f"اسم تيليغرام={actual_title} | النوع={chat_type}"
                )
            except Exception as exc:
                print(f"   ❌ تعذر التحقق من {alias} | chat_id={chat_id}: {exc}")


# ══════════════════════════════════════════════
#  دوال مساعدة
# ══════════════════════════════════════════════

def is_multi(lesson: dict) -> bool:
    """نوع العرض = متعدد → True | فارغ أو صورة → False"""
    return normalize_text(lesson.get("نوع العرض", "")) == normalize_text("متعدد")


def excuse_sticker(teacher: str, course: str | None = None) -> str:
    """نفس قاعدة build_excuse: «شيخة» ← معلمة، «شيخ» ← شيخ، وغيرهما ← معلمة.
    وللدورة ملصقها الخاص إن وُجد (الدور نفسه ثم اعتذار الدورة العام) قبل العام."""
    role = "EXCUSE_SHEIKH" if ("شيخ" in teacher and "شيخة" not in teacher) else "EXCUSE_TEACHER"
    if course:
        mine = COURSE_STICKERS.get(normalize_text(course), {})
        if mine.get(role):
            return mine[role]
        if mine.get("EXCUSE"):
            return mine["EXCUSE"]
    return S_EXCUSE_SHEIKH if role == "EXCUSE_SHEIKH" else S_EXCUSE_TEACHER


async def ping_makeup_reminders():
    """يطلب من اللوحة إرسال تذكير «حدّدي موعد التعويض» للمعلمات اللاتي اخترن «لاحقًا» (اللوحة تحدد من يستحق وتمنع التكرار)."""
    if not (PANEL_URL and PANEL_API_KEY) or DRY_RUN:
        return
    try:
        r = await asyncio.get_running_loop().run_in_executor(
            None, lambda: requests.post(f"{PANEL_URL}/api/makeup-reminders/run", headers=_panel_headers(), timeout=30))
        if r.status_code == 200:
            j = r.json()
            if j.get("sent") or j.get("failed"):
                print(f"🔔 تذكير التعويض: أُرسل {j.get('sent')} | فشل {j.get('failed')}")
        else:
            print(f"⚠️ تذكير التعويض: HTTP {r.status_code}")
    except Exception as exc:
        print(f"⚠️ تعذر طلب تذكير التعويض: {exc}")


def build_makeup_block(lesson) -> str:
    """سطور الحصة التعويضية في إعلان الاعتذار؛ فارغة إن لم تُذكر (اعتذار الإدارة/قديم)."""
    if not lesson:
        return ""
    mk = str(lesson.get("تعويض", "") or "").strip()
    if mk == "yes":
        try:
            md = datetime.strptime(str(lesson.get("تعويض_التاريخ", "")), "%Y-%m-%d")
            h, m = parse_lesson_time(lesson)
        except Exception:
            return ""
        mecca, egypt, morocco = format_times(h, m, target_date=md)
        return (
            f"{SEP}\n"
            f"{RLM}📌 حصة تعويضية بإذن الله\n"
            f"{RLM}🗓 {DAY_NAMES_AR[md.weekday()]} {LRE}{md.strftime('%d/%m/%Y')}{PDF}\n\n"
            f"{RLM}🇸🇦 مكة: {mecca}\n"
            f"{RLM}🇪🇬 مصر: {egypt}\n"
            f"{RLM}🇲🇦 المغرب: {morocco}\n\n"
        )
    if mk == "no":
        return (f"{SEP}\n{RLM}ℹ️ لا توجد حصة تعويضية لهذا الموعد، "
                f"وننتظركم في مجلسكم القادم بإذن الله 🌿\n\n")
    if mk == "later":
        return f"{SEP}\n{RLM}⏳ سيُعلن موعد الحصة التعويضية لاحقًا بإذن الله\n\n"
    return ""


def build_excuse(teacher: str, course: str, time_word: str, lesson=None) -> str:
    if "شيخة" in teacher:
        prefix = f"تعتذر {teacher}"
        pronoun = "أمرها"
    elif "شيخ" in teacher:
        prefix = f"يعتذر {teacher}"
        pronoun = "أمره"
    else:
        prefix = f"تعتذر الأستاذة {teacher}"
        pronoun = "أمرها"
    return (
        f"{BISMILLAH}\n\n"
        f"{RLM}😔 {prefix} عن مجلس {course} {time_word}\n\n"
        f"{build_makeup_block(lesson)}"
        f"{SEP}\n"
        f"يسّر الله {pronoun} وعوّضكم خيراً 🤍"
    )


def format_times(h, m, target_date=None) -> tuple[str, str, str]:
    """يحوّل وقت مكة إلى التوقيتات الثلاث."""
    base = target_date or datetime.now(TZ)
    base = base.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    base = datetime(
        base.year, base.month, base.day, int(h), int(m),
        tzinfo=ZI_RIYADH,
    )
    eg   = base.astimezone(ZI_CAIRO)
    ma   = base.astimezone(ZI_CASABLANCA)
    fmt  = lambda dt: f"{dt.hour}:{str(dt.minute).zfill(2)}"
    return fmt(base), fmt(eg), fmt(ma)


def parse_lesson_time(lesson: dict) -> tuple[int, int]:
    try:
        hour = int(str(lesson.get("الساعة", "")).strip())
        minute = int(str(lesson.get("الدقيقة", "")).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("الساعة أو الدقيقة ليست رقماً صحيحاً") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"وقت خارج النطاق: {hour:02d}:{minute:02d}")
    return hour, minute


def minutes_until_lesson(lesson: dict, now: datetime) -> float:
    """موعد الحصة الأسبوعي التالي، بما في ذلك الحصص بعد منتصف الليل."""
    lesson_day = day_number(lesson.get("اليوم", ""))
    if lesson_day is None:
        raise ValueError(f"يوم غير معروف: {lesson.get('اليوم', '')!r}")
    hour, minute = parse_lesson_time(lesson)
    days_ahead = (lesson_day - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return (target - now).total_seconds() / 60


def lesson_diff_minutes(h: int, m: int, now: datetime) -> float:
    """الفرق بالدقائق — يعالج عبور منتصف الليل."""
    diff = (h * 60 + m) - (now.hour * 60 + now.minute)
    if diff < -720:  diff += 1440
    elif diff > 720: diff -= 1440
    return diff


# ══════════════════════════════════════════════
#  التذكير المسائي — 11 مساء مكة
#  GitHub Actions cron: 0 18 * * *
# ══════════════════════════════════════════════

async def send_evening_reminders():
    bot       = Bot(token=TOKEN)
    now       = datetime.now(TZ)
    tmrw      = (now.weekday() + 1) % 7
    tmrw_name = DAY_NAMES_AR.get(tmrw, "")
    tmrw_date_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        lessons = get_lessons()
    except Exception as e:
        await report_failure(bot, str(e))
        raise
    if not FORCE_SEND and not claim_job_window("evening", now.strftime("%Y-%m-%d")):
        return
    sent      = 0
    failures  = 0
    shared_stickers: set = set()

    # ── تجميع الدورات حسب نوع العرض ──────────────────────────────────
    courses: dict = {}
    for lesson in lessons:
        if not course_selected(lesson):
            continue
        if not FORCE_SEND and day_number(lesson.get("اليوم", "")) != tmrw:
            continue
        key = (
            normalize_text(lesson.get("الدورة", "")),
            tuple(sorted(cid for _, cid in get_all_course_chat_ids(lesson))),
        )
        status = lesson.get("الحالة", "").strip()
        if key not in courses:
            courses[key] = {
                "name":      lesson.get("الدورة", "").strip(),
                "multi":     is_multi(lesson),
                "img_tmrw":  "",
                "img_course": "",
                "sessions":  [],
                "apologies": [],
                "lesson":    lesson,
            }
        if lesson.get("صورة_غد", "").strip():
            courses[key]["img_tmrw"]   = lesson.get("صورة_غد", "").strip()
        if lesson.get("صورة الدورة", "").strip():
            courses[key]["img_course"] = lesson.get("صورة الدورة", "").strip()

        if status == "اعتذار":
            courses[key]["apologies"].append(lesson)
        elif status == "نشط":
            courses[key]["sessions"].append(lesson)

    course_items = list(courses.items())
    if TEST_MODE or DRY_RUN:
        course_items = course_items[:TEST_LIMIT]
    target_date = now + timedelta(days=1)

    for key, data in course_items:
        lesson      = data["lesson"]
        course_name = data["name"]

        # ── اعتذارات (مشترك بين النوعين) ─────────────────────────────
        for ap in data["apologies"]:
            teacher = ap.get("المعلمة", "").strip()
            for group_name, chat_id in get_all_course_chat_ids(ap):
                try:
                    if not claim_delivery(now.strftime("%Y-%m-%d"), "evening-apology", chat_id, course_name, teacher):
                        continue
                    await send_sticker(bot, chat_id, excuse_sticker(teacher, course_name))
                    await asyncio.sleep(0.5)
                    await send_text(bot, chat_id, build_excuse(teacher, course_name, "غداً", ap))
                    print(f"✅ اعتذار ليلي → {group_name} | {course_name} | {teacher}")
                    sent += 1
                except Exception as e:
                    failures += 1
                    print(f"❌ فشل اعتذار → {group_name}: {e}")

        if not data["sessions"]:
            continue
        session_chats = get_session_chat_ids(data["sessions"])
        if not session_chats:
            print(f"⚠️ {course_name}: لا توجد مجموعة صالحة للإرسال")
            continue

        # ════════════════════════════════════════════════════
        #  نوع العرض = متعدد → تصميم صورة_غد
        # ════════════════════════════════════════════════════
        if data["multi"]:
            img = data["img_tmrw"]
            fallback_text = (
                f"{BISMILLAH}\n\n{RLM}📅 تذكير بحصص غد {tmrw_name}\n"
                f"{RLM}📆 {tmrw_date_str}\n🌿 {display_course_label(course_name)}\n"
                f"{SEP}\nالمدرسة النافعية لتحفيظ القرآن الكريم"
            )
            for group_name, chat_id in session_chats:
                try:
                    if not claim_delivery(now.strftime("%Y-%m-%d"), "evening", chat_id, course_name):
                        continue
                    await send_shared_stickers(bot, chat_id, (S_NAFEAYA,), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                    await send_shared_stickers(bot, chat_id, (sticker_for("SALAM", course_name),), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                    await send_photo_or_fallback(
                        bot, chat_id, img, caption=None, fallback_text=fallback_text
                    )
                    print(f"✅ ليلي (متعدد/تصميم) → {group_name} | {course_name}")
                    sent += 1
                except Exception as e:
                    failures += 1
                    print(f"❌ فشل → {group_name} | {course_name}: {e}")

        # ════════════════════════════════════════════════════
        #  نوع العرض = فارغ → السلوك القديم (نص الأوقات)
        # ════════════════════════════════════════════════════
        else:
            sessions = data["sessions"]

            # حصص متعددة → نص تفصيلي
            if len(sessions) > 1:
                # ترتيب الحصص تصاعدياً حسب توقيت مكة، ثم فصلها لصباحية/مسائية
                valid_sessions = []
                for session in sessions:
                    try:
                        parse_lesson_time(session)
                        valid_sessions.append(session)
                    except ValueError as exc:
                        print(f"⚠️ صف متجاوز في {course_name}: {exc}")
                if not valid_sessions:
                    continue
                sorted_sessions = sorted(valid_sessions, key=parse_lesson_time)
                # الفترة الصباحية: من 8:00 حتى قبل 15:00 بتوقيت مكة — وما عداها مسائية
                morning_sessions = [s for s in sorted_sessions if 8 <= int(s.get("الساعة", 0)) < 15]
                evening_sessions = [s for s in sorted_sessions if not (8 <= int(s.get("الساعة", 0)) < 15)]

                def render_session_block(s, num):
                    teacher = s.get("المعلمة", "").strip()
                    riwaya  = s.get("الرواية", "").strip()
                    t_sa, t_eg, t_ma = format_times(
                        s.get("الساعة", 0), s.get("الدقيقة", 0), target_date
                    )
                    riwaya_line = f" — {riwaya}" if riwaya else ""
                    return (
                        f"👑 {num}- {teacher}{riwaya_line}\n"
                        f"{RLM}🇸🇦 مكة: {t_sa}  🇪🇬 مصر: {t_eg}  🇲🇦 المغرب: {t_ma}\n"
                    )

                def render_period(title, period_sessions):
                    blocks = [render_session_block(s, i + 1) for i, s in enumerate(period_sessions)]
                    return f"{SEP}\n{RLM}{title}\n{SEP}\n\n" + f"{SEP}\n".join(blocks)

                text = (
                    f"{BISMILLAH}\n\n"
                    f"{RLM}🔻 مواعيد حصص غد {tmrw_name} 🔻\n"
                    f"{RLM}📆 {tmrw_date_str}\n"
                    f"🌿 {display_course_label(course_name)}\n\n"
                )

                if morning_sessions:
                    text += render_period("☀️ الفترة الصباحية", morning_sessions)

                if morning_sessions and evening_sessions:
                    text += "\n"

                if evening_sessions:
                    text += render_period("🌙 الفترة المسائية", evening_sessions)

                text += f"\n{SEP}\nالمدرسة النافعية لتحفيظ القرآن الكريم"

                for group_name, chat_id in session_chats:
                    try:
                        if not claim_delivery(now.strftime("%Y-%m-%d"), "evening", chat_id, course_name):
                            continue
                        await send_shared_stickers(bot, chat_id, (S_NAFEAYA,), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                        await send_shared_stickers(bot, chat_id, (sticker_for("SALAM", course_name),), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                        await send_shared_stickers(bot, chat_id, (sticker_for("REMINDER", course_name),), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                        await send_text(bot, chat_id, text)
                        print(f"✅ ليلي (قديم/متعدد) → {group_name} | {course_name}")
                        sent += 1
                    except Exception as e:
                        failures += 1
                        print(f"❌ فشل → {group_name} | {course_name}: {e}")

            # حصة واحدة → صورة الدورة + نص
            else:
                s       = sessions[0]
                teacher = s.get("المعلمة", "").strip()
                riwaya  = s.get("الرواية", "").strip()
                t_sa, t_eg, t_ma = format_times(h=s.get("الساعة", 0), m=s.get("الدقيقة", 0), target_date=target_date)
                riwaya_line = f" — {riwaya}" if riwaya else ""
                time_text = (
                    f"{BISMILLAH}\n\n"
                    f"{RLM}📅 تذكير بحصة غد {tmrw_name}\n"
                    f"{RLM}📆 {tmrw_date_str}\n"
                    f"🌿 {display_course_label(course_name)}\n"
                    f"{SEP}\n"
                    f"👑 {teacher}{riwaya_line}\n"
                    f"{SEP}\n"
                    f"{RLM}🇸🇦 مكة: {t_sa}\n{RLM}🇪🇬 مصر: {t_eg}\n{RLM}🇲🇦 المغرب: {t_ma}\n"
                    f"{SEP}\nالمدرسة النافعية لتحفيظ القرآن الكريم"
                )
                for group_name, chat_id in session_chats:
                    try:
                        if not claim_delivery(now.strftime("%Y-%m-%d"), "evening", chat_id, course_name):
                            continue
                        await send_shared_stickers(bot, chat_id, (S_NAFEAYA,), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                        await send_shared_stickers(bot, chat_id, (sticker_for("SALAM", course_name),), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                        await send_shared_stickers(bot, chat_id, (sticker_for("TOMORROW", course_name),), ("evening" if normalize_text(group_name) in {normalize_text("تفاعلية"), normalize_text("نافعية")} else course_name), shared_stickers)
                        await send_photo_or_fallback(
                            bot, chat_id, data["img_course"],
                            caption=None, fallback_text=time_text,
                        )
                        print(f"✅ ليلي (قديم/واحدة) → {group_name} | {course_name}")
                        sent += 1
                    except Exception as e:
                        failures += 1
                        print(f"❌ فشل → {group_name} | {course_name}: {e}")

    if failures and not sent:
        raise RuntimeError(f"فشلت جميع محاولات الإرسال المسائية ({failures})")
    print(f"{'✅ أُرسل ' + str(sent) + ' تذكير ليلي' if sent else 'ℹ️ لا توجد دورات غداً'}")

# ══════════════════════════════════════════════
#  تذكير اليوم
#  متعدد  → 30 دقيقة قبل الحصة + صورة_اليوم + رابط
#  قديم   → 30 دقيقة قبل الحصة + صورة الدورة + رابط
#  ملاحظة: نافذة الإرسال (27-32 دقيقة) أعرض من الفاصل الزمني بين
#  تشغيلات الجدولة الداخلية على Railway (كل 5 دقائق)، فبدون تتبع
#  دائم عبر التشغيلات المتتالية كانت نفس الحصة تُرسَل مرتين. تحفظ
#  مفاتيح التسليم لكل مجموعة في SQLite؛ لدوامها عبر نشر جديد، يجب
#  ضبط REMINDER_STATE_DB على مسار داخل قرص Railway الدائم.
# ══════════════════════════════════════════════

_DAY_SENT_KEYS_BY_DATE: dict[str, set] = {}
STATE_DB = os.getenv("REMINDER_STATE_DB", os.path.join(os.getcwd(), "reminder_state.sqlite3"))


def _state_insert_once(table: str, key: str) -> bool:
    db = sqlite3.connect(STATE_DB, timeout=15)
    try:
        with db:
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY)")
            return db.execute(f"INSERT OR IGNORE INTO {table} (key) VALUES (?)", (key,)).rowcount == 1
    finally:
        db.close()


def claim_delivery(*parts) -> bool:
    """Atomic claim shared by simultaneous jobs and surviving process restarts."""
    if DRY_RUN:
        key = json.dumps(("dry-run", *parts), ensure_ascii=False)
        if key in _DRY_RUN_CLAIMS:
            print(f"🧾 [DRY_RUN] منع تكرار → {key}")
            return False
        _DRY_RUN_CLAIMS.add(key)
        return True
    key = json.dumps(("test" if TEST_MODE else "production", *parts), ensure_ascii=False)
    inserted = _state_insert_once("sent", key)
    if not inserted:
        print(f"⏭️ تكرار متجاوز | instance={INSTANCE_ID} | key={key[:220]}")
    return inserted


def claim_job_window(job_name: str, window_key: str) -> bool:
    """Prevent a whole reminder job from running twice in the same time window."""
    if DRY_RUN:
        return True
    key = json.dumps(("job", "test" if TEST_MODE else "production", job_name, window_key), ensure_ascii=False)
    inserted = _state_insert_once("job_runs", key)
    if not inserted:
        print(f"⏭️ قفل تشغيل متجاوز | instance={INSTANCE_ID} | job={job_name} | window={window_key}")
    return inserted


def release_delivery(*parts):
    if DRY_RUN:
        key = json.dumps(("dry-run", *parts), ensure_ascii=False)
        _DRY_RUN_CLAIMS.discard(key)
        return
    key = json.dumps(("test" if TEST_MODE else "production", *parts), ensure_ascii=False)
    db = sqlite3.connect(STATE_DB, timeout=15)
    try:
        with db:
            db.execute("DELETE FROM sent WHERE key = ?", (key,))
    finally:
        db.close()


async def send_shared_stickers(bot, chat_id, stickers, marker, sent_stickers):
    """Send each introduction once per destination and reminder window."""
    for sticker in stickers:
        key = (chat_id, marker, sticker)
        if key in sent_stickers:
            continue
        durable_key = (datetime.now(TZ).strftime("%Y-%m-%d"), "sticker", chat_id, marker, sticker)
        if not claim_delivery(*durable_key):
            sent_stickers.add(key)
            continue
        sent_stickers.add(key)
        try:
            await send_sticker(bot, chat_id, sticker)
        except Exception as exc:
            if SKIP_STICKER_ERRORS and is_bad_media_error(exc):
                print(f"⚠️ تخطّي ملصق تعذّر إرساله (SKIP_STICKER_ERRORS): {exc}")
                continue
            if not isinstance(exc, NetworkError):
                release_delivery(*durable_key)
                sent_stickers.discard(key)
            raise
        await asyncio.sleep(0.5)


def _get_day_sent_keys(today_str: str) -> set:
    # إبقاء بيانات اليوم الحالي فقط، وحذف أي تاريخ سابق لتفادي تراكم الذاكرة
    for old_date in [d for d in _DAY_SENT_KEYS_BY_DATE if d.split(":")[0] != today_str.split(":")[0]]:
        del _DAY_SENT_KEYS_BY_DATE[old_date]
    return _DAY_SENT_KEYS_BY_DATE.setdefault(today_str, set())


def class_join_markup(lesson: dict):
    """زر «أنضم إلى طالبات هذه الحصة» تحت رسالة التذكير؛ معطّل ما لم يكن CLASS_JOIN_BUTTON=true.
    لا يؤثر فيه أي فشل، فتُرسل رسالة التذكير بلا زر."""
    if DRY_RUN or not (ROLES_ENABLED and env_bool("CLASS_JOIN_BUTTON", False)):
        return None
    try:
        import students_module
        return students_module.join_markup(lesson.get("الدورة", "").strip(), lesson.get("المعلمة", "").strip())
    except Exception as exc:
        print(f"⚠️ تعذر إعداد زر الانضمام إلى طالبات الحصة: {exc}")
        return None


def class_courses_in_dest(chat_id: int, thread_id: int) -> list[tuple[str, str]]:
    """(الدورة، المعلمة) للحصص المرتبطة بهذه المجموعة أو هذا الموضوع؛ يعتمد آخر نسخة من اللوحة إن وُجدت."""
    cached = _load_panel_cache()
    rows = [r for r in cached[0] if _is_active(r)] if cached else get_lessons()
    out = []
    for lesson in rows:
        for _, dest in get_all_course_chat_ids(lesson):
            chat, thread = split_route(dest)
            if chat == chat_id and (thread or 0) == (thread_id or 0):
                out.append((lesson.get("الدورة", "").strip(), lesson.get("المعلمة", "").strip()))
    return out


async def roles_auto_open(bot, lesson: dict, dest: int) -> None:
    """بعد نجاح تذكير الثلاثين دقيقة: يفتح أخذ الأدوار في الوجهة نفسها إن فُعّلت الحصة في اللوحة. لا يؤثر فيه أي فشل."""
    if DRY_RUN or not ROLES_ENABLED or not env_bool("ROLES_AUTO_OPEN", True):
        return
    try:
        import roles_module
        chat_id, thread_id = split_route(dest)
        await asyncio.sleep(1)
        ok, why = await roles_module.auto_open_session(
            bot, lesson.get("الدورة", "").strip(), lesson.get("المعلمة", "").strip(), chat_id, thread_id or 0)
        print(f"{'🎙️ فُتحت جلسة الأدوار' if ok else 'ℹ️ لم تُفتح جلسة الأدوار'} → {fmt_dest(dest)} | {lesson.get('الدورة', '').strip()}"
              + ("" if ok else f" ({why})"))
    except Exception as exc:
        print(f"⚠️ تعذر فتح جلسة الأدوار تلقائيًا → {fmt_dest(dest)}: {exc}")


async def send_day_reminders():
    bot  = Bot(token=TOKEN)
    now  = datetime.now(TZ)

    try:
        lessons = merge_same_day_reminder_rows(get_lessons())
    except Exception as e:
        await report_failure(bot, str(e))
        raise
    day_slot = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    if not FORCE_SEND and not claim_job_window("day", day_slot.strftime("%Y-%m-%d %H:%M")):
        return
    sent      = 0
    failures  = 0
    shared_stickers: set = _get_day_sent_keys(now.strftime("%Y-%m-%d") + ":stickers")

    for lesson in lessons:
        if (TEST_MODE or DRY_RUN) and sent >= TEST_LIMIT:
            break
        if not course_selected(lesson):
            continue
        try:
            h, m = parse_lesson_time(lesson)
            diff = minutes_until_lesson(lesson, now)
        except ValueError as exc:
            print(f"⚠️ صف متجاوز في {lesson.get('الدورة', '')}: {exc}")
            continue
        multi = is_multi(lesson)

        # جميع أنواع العرض: التذكير قبل 30 دقيقة
        # Half-open five-minute window matches the five-minute scheduler interval.
        if not FORCE_SEND and not (27 < diff <= 32):
            continue

        course_key = (
            normalize_text(lesson.get("الدورة", "")), h, m,
            normalize_text(lesson.get("المعلمة", "")),
            normalize_text(lesson.get("الرواية", "")),
            tuple(sorted(cid for _, cid in get_all_course_chat_ids(lesson))),
            normalize_text(lesson.get("الحالة", "")),
        )

        course_name  = lesson.get("الدورة",     "").strip()
        teacher      = lesson.get("المعلمة",    "").strip()
        riwaya       = lesson.get("الرواية",    "").strip()
        status       = lesson.get("الحالة",     "").strip()
        img_today    = lesson.get("صورة_اليوم", "").strip()
        img_course   = lesson.get("صورة الدورة","").strip()
        link         = lesson.get("رابط الغرفة","").strip()
        today_str    = datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%d/%m/%Y")
        room_text    = (
            f"{BISMILLAH}\n\n"
            f"تفضلي رابط الغرفة 👇\n\n"
            f"{SEP}\n\n"
            f"{RLM}📌 رابط الغرفة:\n\n"
            f"{link}\n\n"
            f"{SEP}\n"
            f"✨ حياكن الله ومرحباً بكن\n"
            f"📆 التاريخ: {LRE}{today_str}{PDF}"
        ) if link else None

        # ── اعتذار (مشترك) ──────────────────────────────────────────
        if status == "اعتذار":
            for group_name, chat_id in get_all_course_chat_ids(lesson):
                delivery_key = day_delivery_key(lesson, chat_id, h, m)
                claimed = False
                content_sent = False
                try:
                    claimed = claim_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                    if not claimed:
                        continue
                    await send_sticker(bot, chat_id, excuse_sticker(teacher, course_name))
                    await asyncio.sleep(0.5)
                    await send_text(bot, chat_id, build_excuse(teacher, course_name, "اليوم", lesson))
                    content_sent = True
                    print(f"✅ اعتذار اليوم → {group_name} | {course_name}")
                    sent += 1
                except Exception as e:
                    if claimed and not content_sent and not isinstance(e, NetworkError):
                        release_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                    failures += 1
                    print(f"❌ فشل اعتذار → {group_name}: {e}")
            continue

        # ════════════════════════════════════════════════════
        #  متعدد → صورة_اليوم + رابط (30 دقيقة)
        # ════════════════════════════════════════════════════
        if multi:
            fallback_text = (
                f"{BISMILLAH}\n\n{RLM}⏰ الحصة بعد 30 دقيقة\n"
                f"🌿 {display_course_label(course_name)}\n{SEP}\n👑 {teacher}\n"
                + (f"{RLM}📌 رابط الغرفة:\n{link}\n" if link else "")
            )
            for group_name, chat_id in get_all_course_chat_ids(lesson):
                delivery_key = day_delivery_key(lesson, chat_id, h, m)
                claimed = False
                content_sent = False
                try:
                    claimed = claim_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                    if not claimed:
                        continue
                    await send_shared_stickers(bot, chat_id, (S_NAFEAYA, sticker_for("SALAM", course_name), sticker_for("HALF_HR", course_name)), (h, m), shared_stickers)
                    await send_photo_or_fallback(
                        bot, chat_id, img_today, caption=room_text,
                        fallback_text=fallback_text, markup=class_join_markup(lesson),
                    )
                    content_sent = True
                    if link:
                        await asyncio.sleep(0.5)
                        await send_shared_stickers(bot, chat_id, (sticker_for("OPEN", course_name),), (h, m), shared_stickers)
                    await roles_auto_open(bot, lesson, chat_id)
                    print(f"✅ اليوم (متعدد/تصميم) → {group_name} | {course_name} | {teacher}")
                    sent += 1
                except Exception as e:
                    if claimed and not content_sent and not isinstance(e, NetworkError):
                        release_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                    failures += 1
                    print(f"❌ فشل → {group_name}: {e}")

        # ════════════════════════════════════════════════════
        #  قديم → صورة الدورة + نص (30 دقيقة)
        # ════════════════════════════════════════════════════
        else:
            t_sa, t_eg, t_ma = format_times(h, m)
            riwaya_line = f"📖 الرواية: {riwaya}\n" if riwaya else ""

            # نص مع الأوقات للدورات التي لا صورة لها
            if not img_course:
                text = (
                    f"{BISMILLAH}\n\n"
                    f"{RLM}⏰ الحصة بعد 30 دقيقة\n"
                    f"🌿 {display_course_label(course_name)}\n"
                    f"{SEP}\n"
                    f"👑 {teacher}\n"
                    f"{riwaya_line}"
                    f"{SEP}\n"
                    f"{RLM}🇸🇦 مكة: {t_sa}\n{RLM}🇪🇬 مصر: {t_eg}\n{RLM}🇲🇦 المغرب: {t_ma}\n"
                    f"{SEP}\n"
                    + (f"{RLM}📌 رابط الغرفة:\n{link}\n{SEP}\n" if link else "")
                    + "المدرسة النافعية لتحفيظ القرآن الكريم"
                )
                for group_name, chat_id in get_all_course_chat_ids(lesson):
                    delivery_key = day_delivery_key(lesson, chat_id, h, m)
                    claimed = False
                    content_sent = False
                    try:
                        claimed = claim_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                        if not claimed:
                            continue
                        await send_shared_stickers(bot, chat_id, (S_NAFEAYA, sticker_for("SALAM", course_name), sticker_for("HALF_HR", course_name)), (h, m), shared_stickers)
                        await send_text(bot, chat_id, text, markup=class_join_markup(lesson))
                        content_sent = True
                        if link:
                            await asyncio.sleep(0.5)
                            await send_shared_stickers(bot, chat_id, (sticker_for("OPEN", course_name),), (h, m), shared_stickers)
                        await roles_auto_open(bot, lesson, chat_id)
                        print(f"✅ اليوم (قديم/نص) → {group_name} | {course_name}")
                        sent += 1
                    except Exception as e:
                        if claimed and not content_sent and not isinstance(e, NetworkError):
                            release_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                        failures += 1
                        print(f"❌ فشل → {group_name}: {e}")

            # صورة الدورة + رابط الغرفة
            else:
                caption = room_text
                for group_name, chat_id in get_all_course_chat_ids(lesson):
                    delivery_key = day_delivery_key(lesson, chat_id, h, m)
                    claimed = False
                    content_sent = False
                    try:
                        claimed = claim_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                        if not claimed:
                            continue
                        await send_shared_stickers(bot, chat_id, (S_NAFEAYA, sticker_for("SALAM", course_name), sticker_for("HALF_HR", course_name)), (h, m), shared_stickers)
                        fallback_text = (
                            f"{BISMILLAH}\n\n{RLM}⏰ الحصة بعد 30 دقيقة\n"
                            f"🌿 {display_course_label(course_name)}\n{SEP}\n👑 {teacher}\n"
                            + (f"{RLM}📌 رابط الغرفة:\n{link}\n" if link else "")
                        )
                        print(
                            f"🖼️ قبل الإرسال | الدورة={course_name!r} | "
                            f"المجموعة={group_name!r} | chat_id={chat_id} | "
                            f"صورة الدورة={img_course!r}"
                        )
                        await send_photo_or_fallback(
                            bot, chat_id, img_course, caption=caption,
                            fallback_text=fallback_text, markup=class_join_markup(lesson),
                        )
                        content_sent = True
                        if link:
                            await asyncio.sleep(0.5)
                            await send_shared_stickers(bot, chat_id, (sticker_for("OPEN", course_name),), (h, m), shared_stickers)
                        await roles_auto_open(bot, lesson, chat_id)
                        print(f"✅ اليوم (قديم/صورة) → {group_name} | {course_name}")
                        sent += 1
                    except Exception as e:
                        if claimed and not content_sent and not isinstance(e, NetworkError):
                            release_delivery(now.strftime("%Y-%m-%d"), *delivery_key)
                        failures += 1
                        print(f"❌ فشل → {group_name}: {e}")

    if failures and not sent:
        raise RuntimeError(f"فشلت جميع محاولات إرسال تذكير اليوم ({failures})")
    print(f"{'✅ أُرسل ' + str(sent) + ' تذكير' if sent else 'ℹ️ لا توجد حصص في النافذة الحالية'}")


# ══════════════════════════════════════════════
#  التذكير الصباحي (8 صباحاً مكة) — صورة الدورة فقط
# ══════════════════════════════════════════════

async def send_morning_reminders():
    bot  = Bot(token=TOKEN)
    now  = datetime.now(TZ)
    wday = now.weekday()

    try:
        lessons = get_lessons()
    except Exception as e:
        await report_failure(bot, str(e))
        raise
    if not FORCE_SEND and not claim_job_window("morning", now.strftime("%Y-%m-%d")):
        return

    sent      = 0
    failures  = 0
    sent_keys: set = set()
    shared_stickers: set = set()
    weekly_sessions_by_course: dict[tuple, set] = {}
    for row in lessons:
        if row.get("الحالة", "").strip() == "نشط":
            weekly_sessions_by_course.setdefault(course_route_key(row), set()).add(
                weekly_session_signature(row)
            )

    for lesson in lessons:
        if (TEST_MODE or DRY_RUN) and sent >= TEST_LIMIT:
            break
        if not course_selected(lesson):
            continue
        if is_standing_session(lesson):
            continue
        if not FORCE_SEND and day_number(lesson.get("اليوم", "")) != wday:
            continue
        if lesson.get("الحالة", "").strip() != "نشط":
            continue

        # التذكير الصباحي خاص بالدورات ذات حصة أسبوعية واحدة فقط.
        course_key = course_route_key(lesson)
        if len(weekly_sessions_by_course.get(course_key, set())) != 1:
            continue
        if course_key in sent_keys:
            continue
        sent_keys.add(course_key)

        course_sessions = [
            row for row in lessons
            if course_route_key(row) == course_key
            and row.get("الحالة", "").strip() == "نشط"
            and (FORCE_SEND or day_number(row.get("اليوم", "")) == wday)
        ]
        img_course = next((
            row.get("صورة الدورة", "").strip() or row.get("صورة_اليوم", "").strip()
            for row in course_sessions
            if row.get("صورة الدورة", "").strip() or row.get("صورة_اليوم", "").strip()
        ), "")

        course_name = lesson.get("الدورة", "").strip()
        _now_ar = datetime.now(pytz.timezone("Asia/Riyadh"))
        morning_date_str = f"{_now_ar.day} {AR_MONTHS[_now_ar.month]} {_now_ar.year}"
        morning_caption = f"📆 التاريخ: {morning_date_str}"
        morning_phrase = (
            f"{SEP_MORNING}\n"
            f"🌸 يسّر الله أمركن، ووفّقكن لكل خير\n"
            f"{SEP_MORNING}"
        )

        fallback_text = f"{morning_caption}\n🌿 دورة {course_name}"
        for group_name, chat_id in get_session_chat_ids(course_sessions):
            claimed = False
            content_sent = False
            try:
                claimed = claim_delivery(now.strftime("%Y-%m-%d"), "morning", chat_id, course_key)
                if not claimed:
                    continue
                await asyncio.sleep(0.5)
                await send_shared_stickers(bot, chat_id, (S_REMINDER,), "morning", shared_stickers)
                await send_shared_stickers(bot, chat_id, (S_TODAY,), "morning", shared_stickers)
                await send_photo_or_fallback(
                    bot, chat_id, img_course, caption=morning_caption,
                    fallback_text=fallback_text,
                )
                content_sent = True
                await asyncio.sleep(0.5)
                await send_text(bot, chat_id, morning_phrase)
                print(f"✅ صباحي → {group_name} | {course_name}")
                sent += 1
            except Exception as e:
                if claimed and not content_sent and not isinstance(e, NetworkError):
                    release_delivery(now.strftime("%Y-%m-%d"), "morning", chat_id, course_key)
                failures += 1
                print(f"❌ فشل (صباحي) → {group_name}: {e}")

    if failures and not sent:
        raise RuntimeError(f"فشلت جميع محاولات الإرسال الصباحية ({failures})")
    print(f"{'✅ أُرسل ' + str(sent) + ' تذكير صباحي' if sent else 'ℹ️ لا توجد دورات للتذكير الصباحي اليوم'}")


# ══════════════════════════════════════════════
#  وضع Railway — جدولة داخلية 24/7
# ══════════════════════════════════════════════

ROLES_ENABLED = env_bool("ROLES_ENABLED", False)


async def start_roles_app():
    """يشغّل إضافة «أخذ الأدوار» (تحتاج استقبال أزرار تيليغرام). معطّلة ما لم يكن ROLES_ENABLED=true."""
    discovery = bool(PANEL_URL and PANEL_API_KEY) and env_bool("GROUPS_DISCOVERY", True)
    if DRY_RUN or not (ROLES_ENABLED or discovery):
        return None
    from telegram.ext import Application, ChatMemberHandler, CommandHandler
    application = Application.builder().token(TOKEN).build()
    if ROLES_ENABLED:
        from roles_module import register_roles
        register_roles(application)
        from students_module import register_students
        register_students(application, courses_in_dest=class_courses_in_dest)
    if discovery:
        application.add_handler(ChatMemberHandler(on_bot_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        application.add_handler(CommandHandler("register_group", register_group_command))
        application.add_handler(CommandHandler("test_dest", test_dest_command))
        application.add_handler(CommandHandler("start", start_command))
        if env_bool("TOPICS_DISCOVERY", True):
            from telegram.ext import MessageHandler, filters
            application.add_handler(MessageHandler(filters.ChatType.GROUPS, on_topic_message, block=False), group=-5)
        if TEACHER_PLACES:
            from telegram.ext import MessageHandler, filters
            application.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_teacher_seen, block=False), group=-4)
    await application.initialize()
    try:
        print(f"🤖 البوت المتصل: @{application.bot.username}")
    except Exception:
        pass
    await application.start()
    await application.updater.start_polling(
        allowed_updates=["message", "callback_query", "my_chat_member"], drop_pending_updates=True)
    if ROLES_ENABLED:
        from roles_module import panel_retry_loop
        application.bot_data["panel_retry_task"] = asyncio.create_task(panel_retry_loop())   # إعادة إرسال ما لم يصل إلى اللوحة
    print(f"🎙️ التطبيق يعمل (polling): أدوار={'نعم' if ROLES_ENABLED else 'لا'}، اكتشاف المجموعات={'نعم' if discovery else 'لا'}")
    return application


async def stop_roles_app(application):
    if application is None:
        return
    task = application.bot_data.get("panel_retry_task")
    if task:
        task.cancel()
    try:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    except Exception as exc:
        print(f"⚠️ تعذر إيقاف إضافة الأدوار بسلاسة: {exc}")


async def run_scheduler():
    """
    يعمل باستمرار على Railway بدل GitHub Actions cron.
    الجدول (توقيت مكة = UTC+3):
      - مسائي:  كل يوم 23:00 مكة = 20:00 UTC
      - صباحي:  كل يوم 08:00 مكة = 05:00 UTC
      - اليوم:  كل 5 دقائق
    """
    if "REMINDER_STATE_DB" not in os.environ:
        print("⚠️ سجل منع التكرار على قرص الحاوية؛ اضبطي REMINDER_STATE_DB على قرص دائم لحمايته عند إعادة النشر")
    print(f"🗃️ سجل منع التكرار: {STATE_DB}")
    # ── خادم صحة لـ Railway (يمنع إيقاف الحاوية) ──────────────────
    from aiohttp import web as aio_web

    async def _health(request):
        return aio_web.Response(text="ok")

    _app = aio_web.Application()
    _app.router.add_get("/", _health)
    _runner = aio_web.AppRunner(_app)
    await _runner.setup()
    _port = int(os.getenv("PORT", 8080))
    _site = aio_web.TCPSite(_runner, "0.0.0.0", _port)
    await _site.start()
    print(f"🌐 خادم الصحة يعمل على port {_port}")

    scheduler = AsyncIOScheduler(timezone="UTC")

    # تشخيص آمن عند بدء التشغيل: يقرأ وجهات سمير الطالبين ولا يرسل شيئاً.
    if not DRY_RUN:
        try:
            await audit_course_routes(Bot(token=TOKEN), get_lessons())
        except Exception as exc:
            print(f"❌ تعذر تنفيذ فحص وجهات سمير الطالبين: {exc}")

    # المسائي — 23:00 مكة = 20:00 UTC
    scheduler.add_job(
        send_evening_reminders,
        CronTrigger(hour=20, minute=0, timezone="UTC"),
        id="evening",
        name="تذكير مسائي",
        max_instances=1,
        misfire_grace_time=300,
    )

    # الصباحي — 08:00 مكة = 05:00 UTC
    scheduler.add_job(
        send_morning_reminders,
        CronTrigger(hour=5, minute=0, timezone="UTC"),
        id="morning",
        name="تذكير صباحي",
        max_instances=1,
        misfire_grace_time=300,
    )

    # تذكير اليوم — فحص كل 5 دقائق
    scheduler.add_job(
        send_day_reminders,
        CronTrigger(minute="*/5", timezone="Asia/Riyadh"),
        id="day",
        name="تذكير اليوم",
        max_instances=1,
        misfire_grace_time=60,
    )

    # تذكير المعلمات بتحديد موعد التعويض — كل ساعة (اللوحة تقرّر من يستحق)
    scheduler.add_job(
        ping_makeup_reminders,
        CronTrigger(minute=7, timezone="Asia/Riyadh"),
        id="makeup_reminders",
        name="تذكير تحديد التعويض",
        max_instances=1,
        misfire_grace_time=300,
    )

    roles_app = await start_roles_app()

    scheduler.start()
    print("✅ APScheduler يعمل — المواعيد:")
    for job in scheduler.get_jobs():
        print(f"   • {job.name}: {job.next_run_time}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    # إبقاء البوت يعمل إلى أن يطلب Railway أو النظام إيقافه.
    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        await stop_roles_app(roles_app)
        await _runner.cleanup()
        print(f"⏹️ تم إيقاف الجدولة | instance={INSTANCE_ID}")



# ══════════════════════════════════════════════
#  نقطة الدخول
# ══════════════════════════════════════════════

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "day"

    if PAUSE_REMINDERS and not TEST_MODE:
        print("⏸️ البوت متوقف: PAUSE_REMINDERS=true")
        sys.exit(0)
    if not TOKEN and not DRY_RUN:
        raise RuntimeError("المتغير السري BOT_TOKEN غير موجود أو فارغ")

    if mode == "scheduler":
        asyncio.run(run_scheduler())
    elif mode == "evening":
        asyncio.run(send_evening_reminders())
    elif mode == "day":
        asyncio.run(send_day_reminders())
    elif mode == "morning":
        asyncio.run(send_morning_reminders())
    else:
        print(f"❌ وضع غير معروف: {mode}")
        sys.exit(1)
