"""
إعدادات المشروع. كل القيم الحساسة تُقرأ من متغيرات البيئة (GitHub Secrets).
لا تضع أي مفتاح أو كلمة مرور هنا مباشرة.
"""

import os

# ---------- صفحات فيسبوك المراد جلب المنشورات منها ----------
FACEBOOK_PAGES = [
    "https://www.facebook.com/share/19bWM9MVtB/",
    "https://www.facebook.com/share/1BwKHuaH1G/",
    "https://www.facebook.com/share/19VWNrb3yk/",
    "https://www.facebook.com/profile.php?id=100063679327981",
    "https://www.facebook.com/share/19rHPXZGg3/",
    "https://www.facebook.com/share/1E2QwrdurV/",
    "https://www.facebook.com/profile.php?id=61587497035915",
]

# ---------- نافذة الجلب ----------
FETCH_WINDOW_HOURS = 6          # يجلب فقط منشورات آخر 6 ساعات
MIN_WORDS = 90                  # أي خبر أقل من هذا العدد يتم تجاهله
MAX_CYCLE_RETRIES = 6           # عدد الدورات التي يُعاد فيها محاولة الخبر الفاشل قبل تجاهله نهائيًا
IMMEDIATE_RETRIES = 2           # عدد محاولات إعادة المحاولة الفورية داخل نفس الدورة قبل تأجيله للدورة القادمة

# ---------- فواصل زمنية لتفادي الحظر/الأخطاء ----------
REWRITE_DELAY_SECONDS = 10      # بين كل عملية إعادة صياغة والتي تليها
PUBLISH_DELAY_SECONDS = 3       # بين كل عملية نشر والتي تليها

# ---------- نماذج جوجل جيمناي لإعادة الصياغة ----------
GEMINI_PRIMARY_MODEL = "gemini-3.6-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEMINI_PRIMARY_KEY = os.environ.get("GEMINI_API_KEY_PRIMARY", "")
GEMINI_FALLBACK_KEY = os.environ.get("GEMINI_API_KEY_FALLBACK", "")
GEMINI_ENDPOINT_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)

# ---------- ووردبريس ----------
WP_URL = os.environ.get("WP_URL", "").rstrip("/")          # مثال: https://example.com
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")    # كلمة مرور تطبيقات ووردبريس، وليست كلمة المرور الرئيسية
# اسم التصنيف الثابت الذي يجب استخدامه دائمًا (لا يُنشأ تصنيف جديد أبدًا)
WP_CATEGORY_NAME = "مقالات وتحليلات"
# إن كنت تعرف رقم التصنيف مسبقًا يمكنك وضعه هنا لتفادي البحث عنه في كل مرة
WP_CATEGORY_ID = os.environ.get("WP_CATEGORY_ID", "").strip()

# ---------- تيليجرام (تقرير النهاية) ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------- ملفات ----------
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "state.json")
REWRITE_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "..", "prompts", "rewrite_rules.md")
