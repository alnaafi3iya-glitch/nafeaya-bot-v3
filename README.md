# nafeaya-bot-v3 (خدمة تجريبية — لا تُرفع إلى nafeaya-bot-v2)

الملفات: bot.py + roles_module.py + requirements.txt + Procfile

## متغيرات Railway
BOT_TOKEN
PANEL_URL=رابط اللوحة (بدون / في النهاية)
PANEL_API_KEY
TEST_MODE=true
ROLES_ENABLED=true
ROLES_ADMIN_IDS=معرّفك الرقمي (وللمشرفات بفواصل)
ROLES_ALLOWED_CHAT_IDS=-1004470982964
ROLES_DB_PATH=/data/roles.db
REMINDER_STATE_DB=/data/state.sqlite3

أضيفي Volume على /data.

ROLES_TELEGRAM_ADMINS=true   (اختياري؛ الافتراضي true: مشرفات المجموعة في تيليغرام = مديرات الجلسات)
