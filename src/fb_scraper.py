"""
جلب المنشورات من صفحات فيسبوك العامة.

ملاحظة مهمة: فيسبوك لا يوفر طريقة رسمية مجانية لجلب منشورات صفحة عامة بدون
موافقة (Graph API يتطلب أن تكون مالك/مدير الصفحة ومراجعة من ميتا). هذا الملف
يعتمد على مكتبة "facebook-scraper" غير الرسمية التي تقرأ نسخة mbasic من
فيسبوك، وهي قد تتوقف عن العمل إذا غيّر فيسبوك شكل الصفحة، أو قد تطلب أحيانًا
كوكيز جلسة تسجيل دخول لتفادي صفحات التحقق. إن توقفت المكتبة عن العمل مستقبلًا
فالحل الأنسب هو استبدال هذا الملف فقط دون تغيير بقية المشروع.

لضمان الاستقرار يمكن اختياريًا إضافة ملف كوكيز جلسة فيسبوك عبر GitHub Secret
باسم FACEBOOK_COOKIES (محتوى ملف كوكيز بصيغة Netscape) — هذا اختياري.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

from facebook_scraper import get_posts

from . import config


def _cookies_path():
    raw = os.environ.get("FACEBOOK_COOKIES", "")
    if not raw:
        return None
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    return path


def fetch_recent_posts():
    """
    يرجع قائمة عناصر بهذا الشكل لكل منشور جديد خلال آخر FETCH_WINDOW_HOURS ساعة:
    {
        "id": معرف فريد للمنشور (يُستخدم لمنع التكرار),
        "source_url": رابط المنشور الأصلي على فيسبوك,
        "text": نص المنشور,
        "image_url": رابط الصورة البارزة أو None إن لم توجد,
    }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.FETCH_WINDOW_HOURS)
    cookies = _cookies_path()
    results = []

    for page in config.FACEBOOK_PAGES:
        page_name = _extract_page_identifier(page)
        try:
            for post in get_posts(
                page_name,
                pages=3,
                cookies=cookies,
                options={"posts_per_page": 20, "allow_extra_requests": False},
            ):
                post_time = post.get("time")
                if post_time is None:
                    continue
                if post_time.tzinfo is None:
                    post_time = post_time.replace(tzinfo=timezone.utc)
                if post_time < cutoff:
                    break  # المنشورات مرتبة زمنيًا تنازليًا، لا داعي للاستمرار

                post_id = post.get("post_id") or post.get("post_url")
                if not post_id:
                    continue

                image_url = post.get("image") or (post.get("images") or [None])[0]

                results.append({
                    "id": str(post_id),
                    "source_url": post.get("post_url") or page,
                    "text": (post.get("text") or "").strip(),
                    "image_url": image_url,
                })
        except Exception as exc:  # noqa: BLE001
            print(f"[fb_scraper] تعذر جلب صفحة {page}: {exc}")
            continue

    return results


def _extract_page_identifier(page):
    """الإعدادات الآن تحتوي أسماء/أرقام صفحات جاهزة مباشرة، فقط نعيدها كما هي."""
    return page
