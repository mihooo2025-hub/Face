"""
جلب المنشورات العامة من صفحات فيسبوك باستخدام Apify.

يعتمد هذا الملف على Apify Facebook Page Posts Scraper بدلًا من
facebook-scraper أو Playwright.

المشروع يطلب أحدث المنشورات ثم يقوم محليًا بتصفية المنشورات
بحسب نافذة FETCH_WINDOW_HOURS.

المخرجات تبقى بنفس الشكل الذي يتوقعه main.py:
{
    "id": "...",
    "source_url": "...",
    "text": "...",
    "image_url": "..."
}
"""

import os
from datetime import datetime, timedelta, timezone

import requests

from . import config


APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "").strip()

APIFY_ENDPOINT = (
    "https://api.apify.com/v2/acts/"
    "simpleapi~facebook-page-posts-scraper/"
    "run-sync-get-dataset-items"
)

APIFY_TIMEOUT_SECONDS = 600

MAX_POSTS_PER_PAGE = 30


def _normalize_page_url(source):
    """
    يحول اسم الصفحة أو المعرف الرقمي إلى رابط Facebook كامل.
    """

    source = str(source).strip()

    if not source:
        return ""

    if source.startswith("http://") or source.startswith("https://"):
        return source

    if source.isdigit():
        return f"https://www.facebook.com/profile.php?id={source}"

    return f"https://www.facebook.com/{source}/"


def _parse_post_time(post):
    """
    يحاول قراءة وقت المنشور من الحقول التي يعيدها Apify.
    """

    unix_value = post.get("postCreatedAtUnix")

    if unix_value is not None:
        try:
            return datetime.fromtimestamp(
                int(unix_value),
                tz=timezone.utc,
            )
        except (ValueError, TypeError, OverflowError):
            pass

    value = post.get("postCreatedAt")

    if not value:
        return None

    try:
        value = str(value).strip()

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def _extract_image_url(post):
    """
    يحاول استخراج أفضل صورة متاحة للمنشور.

    الأولوية:
    1. صورة المنشور الأساسية.
    2. صورة من ألبوم.
    3. صورة الفيديو.
    """

    image = post.get("image")

    if isinstance(image, dict):
        uri = image.get("uri")

        if uri:
            return uri

    if isinstance(image, str) and image.strip():
        return image.strip()

    album_preview = post.get("album_preview")

    if isinstance(album_preview, dict):
        images = album_preview.get("images")

        if isinstance(images, list):
            for item in images:
                if isinstance(item, dict) and item.get("uri"):
                    return item["uri"]

    video_thumbnail = post.get("video_thumbnail")

    if isinstance(video_thumbnail, dict):
        uri = video_thumbnail.get("uri")

        if uri:
            return uri

    if isinstance(video_thumbnail, str) and video_thumbnail.strip():
        return video_thumbnail.strip()

    return None


def _extract_text(post):
    """
    يقرأ نص المنشور من message.
    """

    text = post.get("message")

    if text is None:
        text = post.get("text")

    if not text:
        return ""

    return str(text).strip()


def _is_post_record(item):
    """
    Apify قد يعيد منشورات وتعليقات وملخصات صفحات.
    نحن نريد المنشورات فقط.
    """

    record_type = item.get("recordType")

    if record_type == "post":
        return True

    # حماية إضافية إذا تغير شكل النتيجة.
    if record_type in {"comment", "page_summary"}:
        return False

    # إذا لم يوجد recordType لكن توجد الحقول الأساسية للمنشور.
    return bool(
        item.get("post_id")
        and (
            item.get("message")
            or item.get("text")
        )
        and (
            item.get("postCreatedAt")
            or item.get("postCreatedAtUnix")
        )
    )


def _fetch_from_apify():
    """
    يشغل Apify Actor مرة واحدة لجميع الصفحات الموجودة في الإعدادات.
    """

    if not APIFY_API_TOKEN:
        raise RuntimeError(
            "لم يتم العثور على APIFY_API_TOKEN في متغيرات البيئة. "
            "أضفه إلى GitHub Secrets."
        )

    page_urls = []

    for source in config.FACEBOOK_PAGES:
        url = _normalize_page_url(source)

        if url:
            page_urls.append(url)

    if not page_urls:
        print("[apify] لا توجد صفحات في FACEBOOK_PAGES")
        return []

    payload = {
        "pageUrls": page_urls,
        "maxPostsPerPage": MAX_POSTS_PER_PAGE,
        "postsFrom": "1 day",
        "postsUntil": "",
        "includeComments": False,
        "maxCommentsPerPost": 0,
        "maxRepliesPerComment": 0,
        "proxyConfiguration": {
            "useApifyProxy": True
        },
    }

    headers = {
        "Authorization": f"Bearer {APIFY_API_TOKEN}",
        "Content-Type": "application/json",
    }

    print(
        f"[apify] بدء جلب المنشورات من "
        f"{len(page_urls)} مصدرًا..."
    )

    try:
        response = requests.post(
            APIFY_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=APIFY_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"تعذر الاتصال بـ Apify: {exc}"
        ) from exc

    if response.status_code != 200:
        body = response.text[:2000]

        raise RuntimeError(
            f"Apify أعاد HTTP {response.status_code}: {body}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Apify أعاد استجابة ليست JSON."
        ) from exc

    if not isinstance(data, list):
        raise RuntimeError(
            "صيغة استجابة Apify غير متوقعة: "
            f"{type(data).__name__}"
        )

    print(
        f"[apify] تم استلام {len(data)} سجل من Apify"
    )

    return data


def fetch_recent_posts():
    """
    يرجع المنشورات المؤهلة خلال آخر FETCH_WINDOW_HOURS ساعة.

    يتم جلب أحدث منشورات الصفحات من Apify ثم التصفية محليًا
    للتأكد من الالتزام بنافذة المشروع.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=config.FETCH_WINDOW_HOURS
    )

    try:
        raw_items = _fetch_from_apify()
    except Exception as exc:
        print(f"[apify] فشل جلب المنشورات: {exc}")
        return []

    results = []
    seen_ids = set()

    source_stats = {}

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        if not _is_post_record(item):
            continue

        post_id = item.get("post_id")

        if not post_id:
            post_id = item.get("url")

        if not post_id:
            continue

        post_id = str(post_id)

        if post_id in seen_ids:
            continue

        post_time = _parse_post_time(item)

        if post_time is None:
            print(
                f"[apify] تجاهل المنشور {post_id}: "
                "تعذر معرفة وقت النشر"
            )
            continue

        if post_time < cutoff:
            continue

        text = _extract_text(item)

        if not text:
            print(
                f"[apify] تجاهل المنشور {post_id}: "
                "لا يوجد نص"
            )
            continue

        image_url = _extract_image_url(item)

        if not image_url:
            print(
                f"[apify] تجاهل المنشور {post_id}: "
                "لا توجد صورة"
            )
            continue

        source_url = item.get("url")

        if not source_url:
            source_url = item.get("profileUrl")

        if not source_url:
            source_url = ""

        source_url = str(source_url)

        seen_ids.add(post_id)

        results.append(
            {
                "id": post_id,
                "source_url": source_url,
                "text": text,
                "image_url": image_url,
            }
        )

        profile_url = item.get("profileUrl") or "غير معروف"

        source_stats[profile_url] = (
            source_stats.get(profile_url, 0) + 1
        )

    print(
        f"[apify] بعد تصفية آخر "
        f"{config.FETCH_WINDOW_HOURS} ساعات: "
        f"{len(results)} منشور مؤهل"
    )

    for source, count in source_stats.items():
        print(
            f"[apify] {source}: "
            f"{count} منشور مؤهل"
        )

    return results
