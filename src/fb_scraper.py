"""
جلب المنشورات العامة من فيسبوك عبر RSS-Bridge.

هذه النسخة لا تستخدم:
- facebook-scraper
- Playwright
- Apify
- Facebook Cookies

تعتمد على نسخة RSS-Bridge العامة لتحويل صفحات فيسبوك العامة
إلى RSS/Atom ثم يقرأ المشروع النتائج ويطبق عليها نافذة آخر 6 ساعات.

المخرجات تبقى بنفس الشكل الذي يتوقعه main.py:
{
    "id": "...",
    "source_url": "...",
    "text": "...",
    "image_url": "..."
}

ملاحظة:
RSS-Bridge نفسه يوضح أن Facebook قد يطلب تسجيل الدخول أو CAPTCHA
لبعض المصادر. لذلك قد تعمل بعض الصفحات بينما تفشل صفحات أخرى.
"""

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

import feedparser
import requests

from . import config


RSS_BRIDGE_BASE = "https://rss-bridge.org/bridge01/"

REQUEST_TIMEOUT_SECONDS = 45

MAX_ITEMS_PER_SOURCE = 30


def _normalize_source(source):
    """
    يحول المصدر إلى قيمة يفهمها FacebookBridge.

    أمثلة:
    sada.altactic.2025
    100086387929146
    https://www.facebook.com/example
    """

    source = str(source).strip()

    if not source:
        return ""

    # إذا كان رابطًا كاملًا
    if source.startswith("http://") or source.startswith("https://"):
        source = source.rstrip("/")

        match = re.search(
            r"facebook\.com/(?:profile\.php\?id=)?([^/?&]+)",
            source,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    return source.strip("/")


def _build_bridge_urls(source):
    """
    يبني أكثر من رابط محتمل لـ RSS-Bridge.

    نجرب FacebookBridge أولًا ثم FB2Bridge كخطة احتياطية.
    """

    username = _normalize_source(source)

    if not username:
        return []

    common = {
        "action": "display",
        "context": "User",
        "u": username,
        "limit": str(MAX_ITEMS_PER_SOURCE),
    }

    urls = []

    # الجسر الرئيسي
    params = dict(common)
    params["bridge"] = "FacebookBridge"
    params["format"] = "Atom"

    urls.append(
        RSS_BRIDGE_BASE + "?" + urlencode(params)
    )

    # محاولة RSS بدل Atom
    params = dict(common)
    params["bridge"] = "FacebookBridge"
    params["format"] = "RSS"

    urls.append(
        RSS_BRIDGE_BASE + "?" + urlencode(params)
    )

    # الجسر البديل
    params = {
        "action": "display",
        "bridge": "FB2Bridge",
        "context": "User",
        "u": username,
        "format": "Atom",
        "limit": str(MAX_ITEMS_PER_SOURCE),
    }

    urls.append(
        RSS_BRIDGE_BASE + "?" + urlencode(params)
    )

    return urls


def _clean_text(value):
    """
    تنظيف نص RSS/Atom وتحويل HTML إلى نص بسيط.
    """

    if value is None:
        return ""

    value = str(value)

    value = html.unescape(value)

    value = re.sub(
        r"<br\s*/?>",
        "\n",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"</p\s*>",
        "\n",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = re.sub(
        r"[ \t]+",
        " ",
        value,
    )

    value = re.sub(
        r"\n\s*\n+",
        "\n\n",
        value,
    )

    return value.strip()


def _parse_entry_time(entry):
    """
    يحاول استخراج وقت المنشور من Atom/RSS.
    """

    # feedparser يعطي parsed time في هذه الحقول عادة.
    for field in (
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ):
        value = entry.get(field)

        if value:
            try:
                return datetime(
                    value.tm_year,
                    value.tm_mon,
                    value.tm_mday,
                    value.tm_hour,
                    value.tm_min,
                    value.tm_sec,
                    tzinfo=timezone.utc,
                )
            except (AttributeError, TypeError, ValueError):
                pass

    # محاولة النص الأصلي
    for field in (
        "published",
        "updated",
        "created",
        "pubDate",
    ):
        value = entry.get(field)

        if not value:
            continue

        value = str(value).strip()

        try:
            parsed = parsedate_to_datetime(value)

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except (TypeError, ValueError, OverflowError):
            pass

        try:
            value_iso = value.replace("Z", "+00:00")

            parsed = datetime.fromisoformat(value_iso)

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except (TypeError, ValueError):
            pass

    return None


def _extract_image(entry):
    """
    استخراج صورة المنشور من RSS/Atom.
    """

    # media_content
    media_content = entry.get("media_content")

    if isinstance(media_content, list):
        for media in media_content:
            if not isinstance(media, dict):
                continue

            url = media.get("url")

            if url:
                return url

    # media_thumbnail
    media_thumbnail = entry.get("media_thumbnail")

    if isinstance(media_thumbnail, list):
        for media in media_thumbnail:
            if not isinstance(media, dict):
                continue

            url = media.get("url")

            if url:
                return url

    # enclosure
    enclosures = entry.get("enclosures")

    if isinstance(enclosures, list):
        for enclosure in enclosures:
            if not isinstance(enclosure, dict):
                continue

            url = enclosure.get("href") or enclosure.get("url")

            if url:
                content_type = str(
                    enclosure.get("type", "")
                ).lower()

                if (
                    not content_type
                    or content_type.startswith("image/")
                ):
                    return url

    # links
    links = entry.get("links")

    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue

            url = link.get("href")

            if not url:
                continue

            content_type = str(
                link.get("type", "")
            ).lower()

            if content_type.startswith("image/"):
                return url

    # محاولة أخيرة من المحتوى نفسه
    raw_html = (
        entry.get("summary")
        or entry.get("description")
        or entry.get("content", [{}])[0].get("value", "")
        if entry.get("content")
        else entry.get("summary")
        or entry.get("description")
        or ""
    )

    match = re.search(
        r'<img[^>]+src=["\']([^"\']+)["\']',
        str(raw_html),
        flags=re.IGNORECASE,
    )

    if match:
        return html.unescape(match.group(1))

    return None


def _extract_text(entry):
    """
    استخراج نص المنشور.
    """

    candidates = []

    title = entry.get("title")

    if title:
        candidates.append(str(title))

    summary = entry.get("summary")

    if summary:
        candidates.append(str(summary))

    description = entry.get("description")

    if description:
        candidates.append(str(description))

    content = entry.get("content")

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                value = item.get("value")

                if value:
                    candidates.append(str(value))

    cleaned = []

    for candidate in candidates:
        text = _clean_text(candidate)

        if text and text not in cleaned:
            cleaned.append(text)

    return "\n\n".join(cleaned).strip()


def _extract_entry_id(entry):
    """
    استخراج معرف ثابت للمنشور.
    """

    for field in (
        "id",
        "guid",
        "link",
    ):
        value = entry.get(field)

        if value:
            return str(value).strip()

    return None


def _extract_source_url(entry, fallback):
    """
    رابط المنشور الأصلي.
    """

    link = entry.get("link")

    if link:
        return str(link).strip()

    links = entry.get("links")

    if isinstance(links, list):
        for item in links:
            if not isinstance(item, dict):
                continue

            href = item.get("href")

            if href:
                return str(href).strip()

    return fallback


def _fetch_feed(url):
    """
    تحميل RSS/Atom من RSS-Bridge.
    """

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; FacebookNewsRSS/1.0)"
        ),
        "Accept": (
            "application/rss+xml, "
            "application/atom+xml, "
            "application/xml, "
            "text/xml, "
            "*/*"
        ),
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        print(
            f"[rss_bridge] فشل الاتصال: {exc}"
        )
        return None, None

    content_type = response.headers.get(
        "content-type",
        "",
    ).lower()

    print(
        f"[rss_bridge] HTTP {response.status_code} | "
        f"Content-Type: {content_type}"
    )

    if response.status_code != 200:
        print(
            f"[rss_bridge] استجابة غير ناجحة: "
            f"{response.text[:500]}"
        )
        return None, None

    parsed = feedparser.parse(response.content)

    return response, parsed


def _feed_has_error(parsed, response):
    """
    اكتشاف استجابات RSS-Bridge التي تكون في الحقيقة رسالة خطأ.
    """

    if parsed.bozo:
        bozo_exception = getattr(
            parsed,
            "bozo_exception",
            None,
        )

        if bozo_exception:
            print(
                f"[rss_bridge] تحذير تحليل XML: "
                f"{bozo_exception}"
            )

    feed_title = str(
        parsed.feed.get("title", "")
    ).lower()

    feed_description = str(
        parsed.feed.get("description", "")
    ).lower()

    combined = (
        feed_title
        + " "
        + feed_description
    )

    error_markers = [
        "error",
        "exception",
        "captcha",
        "log in",
        "login",
        "must be logged",
        "unable to find",
        "failed finding",
        "not supported",
    ]

    return any(
        marker in combined
        for marker in error_markers
    )


def _fetch_source(source, cutoff):
    """
    يجرب FacebookBridge ثم FB2Bridge.
    """

    urls = _build_bridge_urls(source)

    if not urls:
        return []

    for attempt_number, url in enumerate(urls, start=1):
        bridge_name = (
            "FacebookBridge"
            if attempt_number <= 2
            else "FB2Bridge"
        )

        print(
            f"[rss_bridge] {source}: "
            f"محاولة {attempt_number} "
            f"({bridge_name})"
        )

        response, parsed = _fetch_feed(url)

        if response is None or parsed is None:
            continue

        if _feed_has_error(parsed, response):
            print(
                f"[rss_bridge] {source}: "
                f"النتيجة تبدو كرسالة خطأ من {bridge_name}"
            )
            continue

        entries = parsed.entries

        if not entries:
            print(
                f"[rss_bridge] {source}: "
                f"{bridge_name} أعاد 0 منشور"
            )
            continue

        results = []

        for entry in entries:
            post_id = _extract_entry_id(entry)

            if not post_id:
                continue

            post_time = _parse_entry_time(entry)

            if post_time is None:
                print(
                    f"[rss_bridge] {source}: "
                    f"تجاهل {post_id}: "
                    "تعذر معرفة وقت المنشور"
                )
                continue

            if post_time < cutoff:
                continue

            text = _extract_text(entry)

            if not text:
                print(
                    f"[rss_bridge] {source}: "
                    f"تجاهل {post_id}: لا يوجد نص"
                )
                continue

            image_url = _extract_image(entry)

            if not image_url:
                print(
                    f"[rss_bridge] {source}: "
                    f"تجاهل {post_id}: لا توجد صورة"
                )
                continue

            source_url = _extract_source_url(
                entry,
                _normalize_source(source),
            )

            results.append(
                {
                    "id": post_id,
                    "source_url": source_url,
                    "text": text,
                    "image_url": image_url,
                }
            )

        if results:
            print(
                f"[rss_bridge] {source}: "
                f"تم قبول {len(results)} منشور"
            )

            return results

        print(
            f"[rss_bridge] {source}: "
            f"{bridge_name} أعاد منشورات، "
            "لكن لم يوجد منشور مؤهل خلال نافذة الوقت."
        )

    print(
        f"[rss_bridge] {source}: "
        "فشلت جميع محاولات RSS-Bridge."
    )

    return []


def fetch_recent_posts():
    """
    يجلب منشورات جميع المصادر خلال آخر FETCH_WINDOW_HOURS ساعة.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=config.FETCH_WINDOW_HOURS
    )

    results = []
    seen_ids = set()

    print(
        "[rss_bridge] "
        f"بدء جلب منشورات آخر "
        f"{config.FETCH_WINDOW_HOURS} ساعات..."
    )

    for source in config.FACEBOOK_PAGES:
        try:
            source_results = _fetch_source(
                source,
                cutoff,
            )

            for item in source_results:
                if item["id"] in seen_ids:
                    continue

                seen_ids.add(item["id"])
                results.append(item)

        except Exception as exc:
            print(
                f"[rss_bridge] {source}: "
                f"خطأ غير متوقع: {exc}"
            )

    print(
        "[rss_bridge] انتهى الجلب: "
        f"{len(results)} منشور مؤهل"
    )

    return results

2. "requirements.txt" — كامل

لم نعد بحاجة إلى Playwright أو "facebook-scraper":

:::writing{variant="document" id="64103" title="requirements.txt"}

requests>=2.31
feedparser>=6.0.11
lxml[html_clean]
lxml_html_clean

3. ".github/workflows/pipeline.yml" — كامل

أزلت تثبيت Chromium وأزلت "FACEBOOK_COOKIES" لأن هذه النسخة لا تحتاجهما:

name: نشر أخبار رياضية من فيسبوك

on:
  schedule:
    - cron: "0 */6 * * *"
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  run-pipeline:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: تثبيت المتطلبات
        run: |
          pip install -r requirements.txt

      - name: تشغيل الدورة
        env:
          GEMINI_API_KEY_PRIMARY: ${{ secrets.GEMINI_API_KEY_PRIMARY }}
          GEMINI_API_KEY_FALLBACK: ${{ secrets.GEMINI_API_KEY_FALLBACK }}

          WP_URL: ${{ secrets.WP_URL }}
          WP_USERNAME: ${{ secrets.WP_USERNAME }}
          WP_APP_PASSWORD: ${{ secrets.WP_APP_PASSWORD }}
          WP_CATEGORY_ID: ${{ secrets.WP_CATEGORY_ID }}

          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}

        run: python -m src.main

      - name: حفظ حالة التكرار/إعادة المحاولة
        run: |
          git config user.name "news-bot"
          git config user.email "news-bot@users.noreply.github.com"
          git add data/state.json
          git diff --cached --quiet || git commit -m "تحديث حالة الدورة [skip ci]"
          git push

4. "config.py"

لا تعدّل "config.py". القائمة الحالية للمصادر ونافذة الست ساعات مناسبة، ولذلك لا يوجد سبب لتغييرها.

ماذا سيحدث الآن؟

الدورة ستعمل بهذا الشكل:

GitHub → RSS-Bridge → Facebook → RSS/Atom → "fb_scraper.py" → آخر 6 ساعات → "main.py" → Gemini → WordPress

وسيحاول الكود لكل مصدر:

1. "FacebookBridge + Atom"
2. "FacebookBridge + RSS"
3. "FB2Bridge + Atom"

ثم ينتقل للمصدر التالي.

وهذا مهم لأن RSS-Bridge لديه بالفعل جسر Facebook رئيسي وجسر Facebook بديل، وكلاهما موجودان حاليًا في المشروع.

لا تضف أي Secret جديد. فقط استبدل الملفات الثلاثة وشغّل الـWorkflow يدويًا.

إذا فشلت الطريقة، سيكون السجل هذه المرة مفيدًا جدًا؛ أرسل لي الأسطر التي تبدأ بـ:

[rss_bridge]

وسنعرف تحديدًا هل RSS-Bridge نفسه استطاع الوصول إلى كل صفحة أم أن Facebook يمنعه. وهذا احتمال حقيقي؛ فالمشروع نفسه لديه معالجة صريحة لحالة تسجيل الدخول وCAPTCHA في Facebook Bridge.

"مستودع RSS-Bridge الرسمي" (https://github.com/RSS-Bridge/rss-bridge?utm_source=chatgpt.com)
