"""
جلب المنشورات العامة من فيسبوك عبر RSS-Bridge.

لا يستخدم:
- Playwright
- facebook-scraper
- Apify
- Facebook Cookies

يحاول الوصول إلى RSS-Bridge باستخدام FacebookBridge ثم FB2Bridge،
ويُبقي فقط المنشورات الواقعة ضمن آخر FETCH_WINDOW_HOURS ساعة.

شكل النتيجة المتوافق مع main.py:
{
    "id": "...",
    "source_url": "...",
    "text": "...",
    "image_url": "..."
}
"""

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import feedparser
import requests

from . import config


RSS_BRIDGE_BASE = "https://rss-bridge.org/bridge01/"

REQUEST_TIMEOUT_SECONDS = 45
MAX_ITEMS_PER_SOURCE = 30


def _normalize_source(source):
    """
    يحول اسم المصدر أو المعرف الرقمي أو رابط Facebook
    إلى قيمة مناسبة للـFacebookBridge.
    """

    source = str(source).strip()

    if not source:
        return ""

    # رابط profile.php?id=123...
    match = re.search(
        r"facebook\.com/profile\.php\?id=([^/?&]+)",
        source,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # رابط عادي مثل:
    # https://www.facebook.com/example
    match = re.search(
        r"facebook\.com/([^/?#]+)",
        source,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return source.strip("/")


def _build_bridge_url(source, bridge_name, output_format):
    """
    يبني رابط RSS-Bridge بطريقة متوافقة مع FacebookBridge.
    """

    username = _normalize_source(source)

    if not username:
        return ""

    params = {
        "action": "display",
        "bridge": bridge_name,
        "context": "User",
        "u": username,
        "format": output_format,
        "limit": str(MAX_ITEMS_PER_SOURCE),
    }

    return RSS_BRIDGE_BASE + "?" + urlencode(params)


def _clean_text(value):
    """
    يحول محتوى HTML إلى نص نظيف.
    """

    if value is None:
        return ""

    text = html.unescape(str(value))

    text = re.sub(
        r"<br\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"</p\s*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    text = re.sub(
        r"\n[ \t]+",
        "\n",
        text,
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


def _parse_entry_time(entry):
    """
    استخراج وقت المنشور من RSS/Atom.
    """

    for field in (
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ):
        parsed_struct = entry.get(field)

        if parsed_struct:
            try:
                return datetime(
                    parsed_struct.tm_year,
                    parsed_struct.tm_mon,
                    parsed_struct.tm_mday,
                    parsed_struct.tm_hour,
                    parsed_struct.tm_min,
                    parsed_struct.tm_sec,
                    tzinfo=timezone.utc,
                )
            except (
                AttributeError,
                TypeError,
                ValueError,
            ):
                pass

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
                parsed = parsed.replace(
                    tzinfo=timezone.utc
                )

            return parsed.astimezone(timezone.utc)

        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
            pass

        try:
            iso_value = value.replace(
                "Z",
                "+00:00",
            )

            parsed = datetime.fromisoformat(
                iso_value
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(
                    tzinfo=timezone.utc
                )

            return parsed.astimezone(timezone.utc)

        except (
            TypeError,
            ValueError,
        ):
            pass

    return None


def _extract_image(entry):
    """
    يحاول استخراج صورة المنشور من RSS/Atom.
    """

    media_content = entry.get(
        "media_content"
    )

    if isinstance(media_content, list):
        for media in media_content:
            if not isinstance(media, dict):
                continue

            url = media.get("url")

            if url:
                return str(url)

    media_thumbnail = entry.get(
        "media_thumbnail"
    )

    if isinstance(media_thumbnail, list):
        for media in media_thumbnail:
            if not isinstance(media, dict):
                continue

            url = media.get("url")

            if url:
                return str(url)

    enclosures = entry.get(
        "enclosures"
    )

    if isinstance(enclosures, list):
        for enclosure in enclosures:
            if not isinstance(enclosure, dict):
                continue

            url = (
                enclosure.get("href")
                or enclosure.get("url")
            )

            if not url:
                continue

            content_type = str(
                enclosure.get("type", "")
            ).lower()

            if (
                not content_type
                or content_type.startswith("image/")
            ):
                return str(url)

    raw_parts = []

    summary = entry.get("summary")

    if summary:
        raw_parts.append(str(summary))

    description = entry.get("description")

    if description:
        raw_parts.append(
            str(description)
        )

    content = entry.get("content")

    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue

            value = item.get("value")

            if value:
                raw_parts.append(
                    str(value)
                )

    raw_html = "\n".join(raw_parts)

    patterns = [
        r'<img[^>]+src=["\']([^"\']+)["\']',
        r'<image[^>]*>\s*<url>([^<]+)</url>',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            raw_html,
            flags=re.IGNORECASE,
        )

        if match:
            return html.unescape(
                match.group(1)
            ).strip()

    return None


def _extract_text(entry):
    """
    استخراج نص المنشور.
    """

    values = []

    for field in (
        "summary",
        "description",
    ):
        value = entry.get(field)

        if value:
            values.append(
                _clean_text(value)
            )

    content = entry.get("content")

    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue

            value = item.get("value")

            if value:
                values.append(
                    _clean_text(value)
                )

    unique_values = []

    for value in values:
        if value and value not in unique_values:
            unique_values.append(value)

    if unique_values:
        return "\n\n".join(
            unique_values
        ).strip()

    title = entry.get("title")

    if title:
        return _clean_text(title)

    return ""


def _extract_entry_id(entry):
    """
    معرف ثابت للمنشور.
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


def _response_has_login_or_block(response):
    """
    التحقق من أن RSS-Bridge أعاد صفحة تسجيل دخول أو حظر
    بدل خلاصة RSS حقيقية.
    """

    text = response.text.lower()

    markers = [
        "captcha",
        "facebook wants rss-bridge",
        "log in",
        "login",
        "checkpoint",
        "security check",
        "unable to fetch",
        "could not fetch",
    ]

    return any(
        marker in text
        for marker in markers
    )


def _fetch_feed(url):
    """
    تحميل وتحليل RSS/Atom.
    """

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; FacebookRSSFetcher/1.0)"
        ),
        "Accept": (
            "application/atom+xml,"
            "application/rss+xml,"
            "application/xml,"
            "text/xml,"
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
        return None

    print(
        "[rss_bridge] "
        f"HTTP={response.status_code} "
        f"Content-Type="
        f"{response.headers.get('content-type', '')}"
    )

    if response.status_code != 200:
        print(
            "[rss_bridge] "
            f"محتوى الخطأ: "
            f"{response.text[:500]}"
        )
        return None

    if _response_has_login_or_block(
        response
    ):
        print(
            "[rss_bridge] "
            "الخدمة أعادت صفحة تسجيل دخول/حماية "
            "بدل خلاصة RSS."
        )
        return None

    parsed = feedparser.parse(
        response.content
    )

    if parsed.bozo:
        error = getattr(
            parsed,
            "bozo_exception",
            None,
        )

        if error:
            print(
                "[rss_bridge] "
                f"تحذير تحليل RSS: {error}"
            )

    return parsed


def _fetch_source(source, cutoff):
    """
    يجرب عدة صيغ للجلب.
    """

    attempts = [
        (
            "FacebookBridge",
            "Atom",
        ),
        (
            "FacebookBridge",
            "RSS",
        ),
        (
            "FB2Bridge",
            "Atom",
        ),
    ]

    for bridge_name, output_format in attempts:
        url = _build_bridge_url(
            source,
            bridge_name,
            output_format,
        )

        if not url:
            continue

        print(
            f"[rss_bridge] {source}: "
            f"محاولة {bridge_name} / "
            f"{output_format}"
        )

        parsed = _fetch_feed(url)

        if parsed is None:
            continue

        entries = parsed.entries

        if not entries:
            print(
                f"[rss_bridge] {source}: "
                f"{bridge_name} لم يعطِ عناصر."
            )
            continue

        accepted = []

        for entry in entries:
            post_id = _extract_entry_id(
                entry
            )

            if not post_id:
                continue

            post_time = _parse_entry_time(
                entry
            )

            if post_time is None:
                print(
                    f"[rss_bridge] {source}: "
                    f"{post_id}: لا يمكن تحديد وقت النشر."
                )
                continue

            if post_time < cutoff:
                continue

            text = _extract_text(
                entry
            )

            if not text:
                print(
                    f"[rss_bridge] {source}: "
                    f"{post_id}: بدون نص."
                )
                continue

            image_url = _extract_image(
                entry
            )

            if not image_url:
                print(
                    f"[rss_bridge] {source}: "
                    f"{post_id}: بدون صورة."
                )
                continue

            source_url = _extract_source_url(
                entry,
                (
                    "https://www.facebook.com/"
                    + _normalize_source(source)
                ),
            )

            accepted.append(
                {
                    "id": str(post_id),
                    "source_url": source_url,
                    "text": text,
                    "image_url": image_url,
                }
            )

        if accepted:
            print(
                f"[rss_bridge] {source}: "
                f"تم قبول {len(accepted)} منشور."
            )
            return accepted

        print(
            f"[rss_bridge] {source}: "
            "تم العثور على عناصر، "
            "لكن لم يوجد منشور مؤهل خلال "
            f"آخر {config.FETCH_WINDOW_HOURS} ساعات."
        )

    print(
        f"[rss_bridge] {source}: "
        "فشلت جميع محاولات RSS-Bridge."
    )

    return []


def fetch_recent_posts():
    """
    جلب منشورات جميع المصادر خلال آخر
    FETCH_WINDOW_HOURS ساعة.
    """

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=config.FETCH_WINDOW_HOURS
        )
    )

    results = []
    seen_ids = set()

    print(
        "[rss_bridge] بدء الجلب..."
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

                seen_ids.add(
                    item["id"]
                )

                results.append(item)

        except Exception as exc:
            print(
                f"[rss_bridge] {source}: "
                f"خطأ: {exc}"
            )

    results.sort(
        key=lambda item: item.get(
            "source_url",
            "",
        )
    )

    print(
        "[rss_bridge] انتهى الجلب: "
        f"{len(results)} منشور مؤهل."
    )

    return results
