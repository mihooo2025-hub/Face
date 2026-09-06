"""
جلب المنشورات العامة من Facebook عبر نسخة RSS-Bridge محلية.

المشروع لا يعتمد على:
- rss-bridge.org العامة
- Playwright
- facebook-scraper
- Apify
- FACEBOOK_COOKIES

يتم تشغيل RSS-Bridge داخل نفس GitHub Actions عبر Docker،
ثم يتصل هذا الملف به على localhost.

المخرجات المتوافقة مع main.py:

{
    "id": "...",
    "source_url": "...",
    "text": "...",
    "image_url": "..."
}
"""

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote, urljoin

import requests
import xml.etree.ElementTree as ET

from . import config


# ---------------------------------------------------------------------
# إعدادات الاتصال
# ---------------------------------------------------------------------

# في GitHub Actions سيُضبط هذا المتغير إلى:
# http://127.0.0.1:8080/
#
# يوجد fallback محلي فقط حتى لا نستخدم الخدمة العامة دون قصد.
RSS_BRIDGE_BASE = os.environ.get(
    "RSS_BRIDGE_BASE",
    "http://127.0.0.1:8080/",
).rstrip("/") + "/"

REQUEST_TIMEOUT_SECONDS = 45

MAX_ITEMS_PER_SOURCE = 50

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/atom+xml,"
        "application/rss+xml,"
        "application/xml,"
        "text/xml,"
        "*/*"
    ),
    "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


# ---------------------------------------------------------------------
# أدوات المصدر
# ---------------------------------------------------------------------

def _normalize_source(source):
    """
    يحول اسم المصدر أو رقمه أو رابط Facebook
    إلى قيمة يستخدمها RSS-Bridge.
    """

    source = str(source).strip()

    if not source:
        return ""

    # profile.php?id=...
    match = re.search(
        r"facebook\.com/profile\.php\?id=([^/?&#]+)",
        source,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # رابط Facebook عادي
    match = re.search(
        r"facebook\.com/([^/?#]+)",
        source,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return source.strip("/")


def _build_bridge_url(source, bridge_name, output_format):
    """
    يبني رابط RSS-Bridge.
    """

    username = _normalize_source(source)

    if not username:
        return ""

    encoded = quote(
        username,
        safe="",
    )

    params = (
        f"?action=display"
        f"&bridge={quote(bridge_name)}"
        f"&context=User"
        f"&u={encoded}"
        f"&limit={MAX_ITEMS_PER_SOURCE}"
        f"&format={quote(output_format)}"
    )

    return RSS_BRIDGE_BASE + params


# ---------------------------------------------------------------------
# أدوات التاريخ
# ---------------------------------------------------------------------

def _parse_datetime(value):
    """
    محاولة تحليل وقت المنشور بعدة صيغ.
    """

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    # Unix timestamp
    try:
        timestamp = int(value)

        if timestamp > 1000000000:
            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            )
    except (
        ValueError,
        TypeError,
        OverflowError,
    ):
        pass

    # ISO 8601
    try:
        normalized = value.replace(
            "Z",
            "+00:00",
        )

        parsed = datetime.fromisoformat(
            normalized
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except ValueError:
        pass

    # RFC 2822
    try:
        parsed = parsedate_to_datetime(
            value
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None


# ---------------------------------------------------------------------
# أدوات النص
# ---------------------------------------------------------------------

def _clean_html(value):
    """
    تحويل HTML إلى نص نظيف.
    """

    if not value:
        return ""

    text = unescape(
        str(value)
    )

    text = re.sub(
        r"<br\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"</p\s*>",
        "\n\n",
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


def _extract_image_from_html(value):
    """
    استخراج صورة من HTML.
    """

    if not value:
        return None

    value = unescape(
        str(value)
    )

    patterns = [
        r'<img[^>]+src=["\']([^"\']+)["\']',
        r'<img[^>]+src=([^ >]+)',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            value,
            flags=re.IGNORECASE,
        )

        if match:
            url = (
                match.group(1)
                .strip()
            )

            if url.startswith("http"):
                return url

    return None


# ---------------------------------------------------------------------
# أدوات XML
# ---------------------------------------------------------------------

def _local_name(tag):
    """
    يعيد اسم XML بدون namespace.
    """

    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[1]

    return tag


def _child_text(element, names):
    """
    استخراج نص أول عنصر يطابق الاسم.
    """

    wanted = {
        str(name).lower()
        for name in names
    }

    for child in element.iter():
        if child is element:
            continue

        if (
            _local_name(child.tag).lower()
            in wanted
        ):
            if child.text:
                return child.text.strip()

    return ""


def _atom_link(element):
    """
    استخراج رابط Atom.
    """

    for child in element.iter():
        if (
            _local_name(child.tag).lower()
            != "link"
        ):
            continue

        href = child.attrib.get("href")

        if href:
            return unescape(
                href.strip()
            )

        if child.text:
            return child.text.strip()

    return ""


def _extract_media_urls(element):
    """
    استخراج روابط الصور من enclosure/media.
    """

    images = []

    for child in element.iter():
        local = _local_name(
            child.tag
        ).lower()

        if local not in {
            "enclosure",
            "content",
            "thumbnail",
        }:
            continue

        url = (
            child.attrib.get("url")
            or child.attrib.get("href")
        )

        if not url:
            continue

        url = unescape(
            str(url).strip()
        )

        if url.startswith(
            "http://"
        ) or url.startswith(
            "https://"
        ):
            images.append(url)

    return images


# ---------------------------------------------------------------------
# تحليل المنشورات
# ---------------------------------------------------------------------

def _build_post_id(
    source,
    link,
    timestamp,
    text,
):
    """
    إنشاء معرف ثابت للمنشور إذا لم يعط RSS-Bridge معرفًا واضحًا.
    """

    base = (
        link
        or (
            f"{source}|"
            f"{timestamp}|"
            f"{text[:300]}"
        )
    )

    digest = hashlib.sha1(
        base.encode(
            "utf-8"
        )
    ).hexdigest()

    return f"rss:{digest}"


def _parse_feed(
    xml_text,
    source,
    cutoff,
):
    """
    تحليل RSS أو Atom.
    """

    try:
        root = ET.fromstring(
            xml_text
        )
    except ET.ParseError as exc:
        print(
            f"[rss_bridge] {source}: "
            f"تعذر تحليل XML: {exc}"
        )
        return []

    root_name = _local_name(
        root.tag
    ).lower()

    results = []

    # ---------------------------------------------------------------
    # Atom
    # ---------------------------------------------------------------

    if root_name == "feed":
        entries = [
            element
            for element in root.iter()
            if _local_name(
                element.tag
            ).lower()
            == "entry"
        ]

        for entry in entries:
            title = _child_text(
                entry,
                ["title"],
            )

            content_raw = _child_text(
                entry,
                [
                    "content",
                    "summary",
                ],
            )

            published_raw = _child_text(
                entry,
                [
                    "published",
                    "updated",
                ],
            )

            link = _atom_link(
                entry
            )

            post_time = _parse_datetime(
                published_raw
            )

            if post_time is None:
                continue

            if post_time < cutoff:
                continue

            text = _clean_html(
                content_raw
            )

            if not text:
                text = _clean_html(
                    title
                )

            if not text:
                continue

            image_url = _extract_image_from_html(
                content_raw
            )

            media_urls = _extract_media_urls(
                entry
            )

            if not image_url and media_urls:
                image_url = media_urls[0]

            if not image_url:
                continue

            post_id = _build_post_id(
                source=source,
                link=link,
                timestamp=post_time.isoformat(),
                text=text,
            )

            source_url = (
                urljoin(
                    "https://www.facebook.com/",
                    link,
                )
                if link
                else (
                    "https://www.facebook.com/"
                    + _normalize_source(source)
                )
            )

            results.append(
                {
                    "id": post_id,
                    "source_url": source_url,
                    "text": text,
                    "image_url": image_url,
                    "_published_at": post_time,
                }
            )

        return results

    # ---------------------------------------------------------------
    # RSS
    # ---------------------------------------------------------------

    items = [
        element
        for element in root.iter()
        if _local_name(
            element.tag
        ).lower()
        == "item"
    ]

    for item in items:
        title = _child_text(
            item,
            ["title"],
        )

        content_raw = _child_text(
            item,
            [
                "description",
                "encoded",
                "content",
            ],
        )

        published_raw = _child_text(
            item,
            [
                "pubDate",
                "published",
                "updated",
                "date",
            ],
        )

        link = _child_text(
            item,
            [
                "link",
            ],
        )

        if not link:
            guid = _child_text(
                item,
                [
                    "guid",
                ],
            )

            if guid.startswith(
                "http"
            ):
                link = guid

        post_time = _parse_datetime(
            published_raw
        )

        if post_time is None:
            continue

        if post_time < cutoff:
            continue

        text = _clean_html(
            content_raw
        )

        if not text:
            text = _clean_html(
                title
            )

        if not text:
            continue

        image_url = _extract_image_from_html(
            content_raw
        )

        media_urls = _extract_media_urls(
            item
        )

        if not image_url and media_urls:
            image_url = media_urls[0]

        if not image_url:
            continue

        post_id = _build_post_id(
            source=source,
            link=link,
            timestamp=post_time.isoformat(),
            text=text,
        )

        source_url = (
            urljoin(
                "https://www.facebook.com/",
                link,
            )
            if link
            else (
                "https://www.facebook.com/"
                + _normalize_source(source)
            )
        )

        results.append(
            {
                "id": post_id,
                "source_url": source_url,
                "text": text,
                "image_url": image_url,
                "_published_at": post_time,
            }
        )

    return results


# ---------------------------------------------------------------------
# الاتصال بـ RSS-Bridge
# ---------------------------------------------------------------------

def _request_feed(
    session,
    source,
    bridge_name,
    output_format,
    attempt_number,
):
    """
    ينفذ محاولة واحدة.
    """

    url = _build_bridge_url(
        source,
        bridge_name,
        output_format,
    )

    if not url:
        return None

    print(
        f"[rss_bridge] {source}: "
        f"المحاولة {attempt_number} "
        f"{bridge_name}/{output_format}"
    )

    try:
        response = session.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(
            f"[rss_bridge] {source}: "
            f"فشل الاتصال: {exc}"
        )
        return None

    content_type = response.headers.get(
        "Content-Type",
        "",
    )

    print(
        f"[rss_bridge] {source}: "
        f"HTTP={response.status_code} "
        f"Content-Type={content_type}"
    )

    if response.status_code != 200:
        # نطبع بداية الخطأ فقط.
        preview = (
            response.text[:700]
            .replace("\n", " ")
            .strip()
        )

        print(
            f"[rss_bridge] {source}: "
            f"خطأ من RSS-Bridge: {preview}"
        )

        return None

    return response


def _fetch_source(
    session,
    source,
    cutoff,
):
    """
    يجرب FacebookBridge ثم FB2Bridge
    بصيغة Atom ثم RSS.
    """

    attempts = [
        ("FacebookBridge", "Atom"),
        ("FacebookBridge", "RSS"),
        ("FB2Bridge", "Atom"),
        ("FB2Bridge", "RSS"),
    ]

    for number, (
        bridge_name,
        output_format,
    ) in enumerate(
        attempts,
        start=1,
    ):
        response = _request_feed(
            session=session,
            source=source,
            bridge_name=bridge_name,
            output_format=output_format,
            attempt_number=number,
        )

        if response is None:
            continue

        posts = _parse_feed(
            xml_text=response.text,
            source=source,
            cutoff=cutoff,
        )

        if posts:
            return posts

        print(
            f"[rss_bridge] {source}: "
            f"{bridge_name}/{output_format} "
            "أعاد Feed صالحًا لكن بدون منشور "
            f"مؤهل خلال آخر "
            f"{config.FETCH_WINDOW_HOURS} ساعات."
        )

    print(
        f"[rss_bridge] {source}: "
        "فشلت جميع محاولات RSS-Bridge."
    )

    return []


# ---------------------------------------------------------------------
# الدالة الرئيسية
# ---------------------------------------------------------------------

def fetch_recent_posts():
    """
    يجلب منشورات المصادر خلال آخر
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
        "[rss_bridge] "
        f"بدء الجلب عبر النسخة المحلية: "
        f"{RSS_BRIDGE_BASE}"
    )

    session = requests.Session()

    try:
        for source in config.FACEBOOK_PAGES:
            source = str(
                source
            ).strip()

            if not source:
                continue

            try:
                posts = _fetch_source(
                    session=session,
                    source=source,
                    cutoff=cutoff,
                )

                accepted = 0

                for post in posts:
                    post_id = post.get(
                        "id"
                    )

                    if not post_id:
                        continue

                    if post_id in seen_ids:
                        continue

                    text = str(
                        post.get(
                            "text",
                            "",
                        )
                    ).strip()

                    if not text:
                        continue

                    seen_ids.add(
                        post_id
                    )

                    results.append(
                        {
                            "id": post_id,
                            "source_url": post.get(
                                "source_url",
                                "",
                            ),
                            "text": text,
                            "image_url": post.get(
                                "image_url"
                            ),
                        }
                    )

                    accepted += 1

                print(
                    f"[rss_bridge] {source}: "
                    f"تم قبول {accepted} منشور."
                )

            except Exception as exc:
                print(
                    f"[rss_bridge] {source}: "
                    f"خطأ غير متوقع: {exc}"
                )

    finally:
        session.close()

    print(
        "[rss_bridge] انتهى الجلب: "
        f"{len(results)} منشور مؤهل."
    )

    return results
