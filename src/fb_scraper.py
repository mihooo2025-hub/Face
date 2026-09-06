"""
جلب المنشورات من صفحات Facebook عبر نسخة RSS-Bridge المحلية.

هذا الملف لا يعتمد على rss-bridge.org العامة.
يستخدم نسخة RSS-Bridge التي يتم تشغيلها محليًا داخل GitHub Actions.

الهدف من هذا الإصدار:
- إعطاء تقرير واضح لكل مصدر.
- التفريق بين:
  1) نجاح الجسر ووجود منشورات مؤهلة.
  2) نجاح الجسر لكن عدم وجود منشورات خلال النافذة الزمنية.
  3) فشل الجسر.
  4) فشل تحليل الـ Feed.
- عدم طباعة صفحات HTML الطويلة في سجل GitHub.
- تجربة FacebookBridge وFB2Bridge بصيغ Atom وRSS.
"""

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote, urljoin
import xml.etree.ElementTree as ET

import requests

from . import config


# ============================================================
# إعدادات RSS-Bridge
# ============================================================

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


# ============================================================
# أدوات المصدر
# ============================================================

def _normalize_source(source):
    """
    يحول اسم صفحة Facebook أو رابطها إلى القيمة المناسبة
    لـ RSS-Bridge.
    """

    source = str(source).strip()

    if not source:
        return ""

    # profile.php?id=123
    match = re.search(
        r"facebook\.com/profile\.php\?id=([^/?&#]+)",
        source,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # facebook.com/username
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

    encoded_username = quote(
        username,
        safe="",
    )

    return (
        f"{RSS_BRIDGE_BASE}"
        f"?action=display"
        f"&bridge={quote(bridge_name)}"
        f"&context=User"
        f"&u={encoded_username}"
        f"&limit={MAX_ITEMS_PER_SOURCE}"
        f"&format={quote(output_format)}"
    )


# ============================================================
# التاريخ
# ============================================================

def _parse_datetime(value):
    """
    يحاول تحويل قيمة التاريخ إلى UTC.
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


# ============================================================
# تنظيف النص والصور
# ============================================================

def _clean_html(value):
    """
    يحول HTML إلى نص نظيف.
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
    يحاول استخراج صورة من HTML.
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
            image_url = (
                match.group(1)
                .strip()
            )

            if image_url.startswith(
                "http://"
            ) or image_url.startswith(
                "https://"
            ):
                return image_url

    return None


# ============================================================
# XML
# ============================================================

def _local_name(tag):
    """
    إزالة namespace من اسم عنصر XML.
    """

    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[1]

    return tag


def _child_text(element, names):
    """
    استخراج نص أول عنصر يطابق أحد الأسماء.
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
    استخراج رابط من Atom.
    """

    for child in element.iter():
        if (
            _local_name(child.tag).lower()
            != "link"
        ):
            continue

        href = child.attrib.get(
            "href"
        )

        if href:
            return unescape(
                href.strip()
            )

        if child.text:
            return child.text.strip()

    return ""


def _extract_media_urls(element):
    """
    استخراج الصور من enclosure/media/content.
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


# ============================================================
# إنشاء ID
# ============================================================

def _build_post_id(
    source,
    link,
    timestamp,
    text,
):
    """
    إنشاء ID ثابت للمنشور.
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
        base.encode("utf-8")
    ).hexdigest()

    return f"rss:{digest}"


# ============================================================
# تحليل Feed
# ============================================================

def _parse_feed(
    xml_text,
    source,
    cutoff,
):
    """
    يرجع:
        {
            "posts": [...],
            "feed_type": "Atom/RSS",
            "raw_entries": عدد العناصر الموجودة,
            "dated_entries": عدد العناصر التي لها تاريخ,
            "time_filtered": عدد العناصر التي استبعدت بسبب التاريخ,
        }
    """

    try:
        root = ET.fromstring(
            xml_text
        )
    except ET.ParseError as exc:
        return {
            "posts": [],
            "feed_type": "غير صالح",
            "raw_entries": 0,
            "dated_entries": 0,
            "time_filtered": 0,
            "parse_error": str(exc),
        }

    root_name = _local_name(
        root.tag
    ).lower()

    results = []

    raw_entries = 0
    dated_entries = 0
    time_filtered = 0

    # ========================================================
    # Atom
    # ========================================================

    if root_name == "feed":

        feed_type = "Atom"

        entries = [
            element
            for element in root.iter()
            if _local_name(
                element.tag
            ).lower()
            == "entry"
        ]

        raw_entries = len(
            entries
        )

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

            dated_entries += 1

            if post_time < cutoff:
                time_filtered += 1
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

            image_url = (
                _extract_image_from_html(
                    content_raw
                )
            )

            media_urls = (
                _extract_media_urls(
                    entry
                )
            )

            if (
                not image_url
                and media_urls
            ):
                image_url = media_urls[0]

            # main.py يتطلب صورة
            if not image_url:
                continue

            post_id = _build_post_id(
                source=source,
                link=link,
                timestamp=post_time.isoformat(),
                text=text,
            )

            if link:
                source_url = urljoin(
                    "https://www.facebook.com/",
                    link,
                )
            else:
                source_url = (
                    "https://www.facebook.com/"
                    + _normalize_source(source)
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

        return {
            "posts": results,
            "feed_type": feed_type,
            "raw_entries": raw_entries,
            "dated_entries": dated_entries,
            "time_filtered": time_filtered,
            "parse_error": None,
        }

    # ========================================================
    # RSS
    # ========================================================

    if root_name in {
        "rss",
        "rdf",
        "rdf:rdf",
    }:

        feed_type = "RSS"

        items = [
            element
            for element in root.iter()
            if _local_name(
                element.tag
            ).lower()
            == "item"
        ]

        raw_entries = len(
            items
        )

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
                ["link"],
            )

            if not link:
                guid = _child_text(
                    item,
                    ["guid"],
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

            dated_entries += 1

            if post_time < cutoff:
                time_filtered += 1
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

            image_url = (
                _extract_image_from_html(
                    content_raw
                )
            )

            media_urls = (
                _extract_media_urls(
                    item
                )
            )

            if (
                not image_url
                and media_urls
            ):
                image_url = media_urls[0]

            if not image_url:
                continue

            post_id = _build_post_id(
                source=source,
                link=link,
                timestamp=post_time.isoformat(),
                text=text,
            )

            if link:
                source_url = urljoin(
                    "https://www.facebook.com/",
                    link,
                )
            else:
                source_url = (
                    "https://www.facebook.com/"
                    + _normalize_source(source)
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

        return {
            "posts": results,
            "feed_type": feed_type,
            "raw_entries": raw_entries,
            "dated_entries": dated_entries,
            "time_filtered": time_filtered,
            "parse_error": None,
        }

    return {
        "posts": [],
        "feed_type": f"غير معروف ({root_name})",
        "raw_entries": 0,
        "dated_entries": 0,
        "time_filtered": 0,
        "parse_error": "نوع Feed غير معروف",
    }


# ============================================================
# تنفيذ محاولة واحدة
# ============================================================

def _request_feed(
    session,
    source,
    bridge_name,
    output_format,
    attempt_number,
):
    """
    تنفيذ طلب واحد إلى RSS-Bridge.

    يرجع:
        response
    أو:
        None
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

        # لا نطبع HTML كاملًا.
        preview = (
            response.text[:300]
            .replace("\n", " ")
            .replace("\r", " ")
            .strip()
        )

        print(
            f"[rss_bridge] {source}: "
            f"الجسر أعاد HTTP {response.status_code}."
        )

        if preview:
            print(
                f"[rss_bridge] {source}: "
                f"بداية الاستجابة: {preview}"
            )

        return None

    return response


# ============================================================
# مصدر واحد
# ============================================================

def _fetch_source(
    session,
    source,
    cutoff,
):
    """
    تجربة الجسور المختلفة لمصدر واحد.

    يرجع:
        posts
        report
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
        (
            "FB2Bridge",
            "RSS",
        ),
    ]

    report = {
        "source": source,
        "successful_requests": 0,
        "failed_requests": 0,
        "valid_empty_feeds": 0,
        "parse_failures": 0,
        "accepted": 0,
        "status": "فشل",
        "method": None,
    }

    all_candidates = []

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

            report[
                "failed_requests"
            ] += 1

            continue

        report[
            "successful_requests"
        ] += 1

        parsed = _parse_feed(
            xml_text=response.text,
            source=source,
            cutoff=cutoff,
        )

        if parsed["parse_error"]:

            report[
                "parse_failures"
            ] += 1

            print(
                f"[rss_bridge] {source}: "
                f"{bridge_name}/{output_format} "
                "أعاد استجابة لكن تحليلها فشل: "
                f"{parsed['parse_error']}"
            )

            continue

        raw_entries = parsed[
            "raw_entries"
        ]

        dated_entries = parsed[
            "dated_entries"
        ]

        time_filtered = parsed[
            "time_filtered"
        ]

        posts = parsed[
            "posts"
        ]

        if not posts:

            report[
                "valid_empty_feeds"
            ] += 1

            print(
                f"[rss_bridge] {source}: "
                f"{bridge_name}/{output_format} "
                f"Feed صالح — "
                f"العناصر: {raw_entries}، "
                f"ذات تاريخ: {dated_entries}، "
                f"أقدم من النافذة: {time_filtered}، "
                "المؤهل بالصور: 0."
            )

            # لا نتوقف مباشرة؛ قد يعطي الجسر الآخر
            # نتيجة أفضل.
            continue

        # وجدنا منشورات مؤهلة.
        all_candidates.extend(
            posts
        )

        report[
            "method"
        ] = (
            f"{bridge_name}/{output_format}"
        )

        print(
            f"[rss_bridge] {source}: "
            f"{bridge_name}/{output_format} "
            f"نجح — "
            f"العناصر: {raw_entries}، "
            f"المؤهل: {len(posts)}."
        )

        # إذا وجدنا نتيجة حقيقية فلا حاجة لتجربة
        # بقية الجسور.
        break

    # --------------------------------------------------------
    # إزالة التكرار داخل المصدر
    # --------------------------------------------------------

    unique_posts = []
    local_seen = set()

    for post in all_candidates:

        post_id = post.get(
            "id"
        )

        if not post_id:
            continue

        if post_id in local_seen:
            continue

        local_seen.add(
            post_id
        )

        unique_posts.append(
            post
        )

    report[
        "accepted"
    ] = len(unique_posts)

    if unique_posts:

        report[
            "status"
        ] = "نجح"

    elif (
        report["successful_requests"] > 0
        and report["valid_empty_feeds"] > 0
    ):

        report[
            "status"
        ] = "Feed صالح بلا منشورات مؤهلة"

    else:

        report[
            "status"
        ] = "فشل جميع الجسور"

    return (
        unique_posts,
        report,
    )


# ============================================================
# الدالة الرئيسية
# ============================================================

def fetch_recent_posts():
    """
    يجلب المنشورات الجديدة من جميع صفحات Facebook.

    يتم تطبيق نافذة:
        config.FETCH_WINDOW_HOURS

    ويُرجع نفس البنية التي يعتمد عليها main.py.
    """

    cutoff = (
        datetime.now(
            timezone.utc
        )
        - timedelta(
            hours=config.FETCH_WINDOW_HOURS
        )
    )

    results = []
    global_seen = set()

    reports = []

    print(
        ""
    )

    print(
        "============================================================"
    )

    print(
        "[rss_bridge] بدء الجلب"
    )

    print(
        f"[rss_bridge] العنوان: {RSS_BRIDGE_BASE}"
    )

    print(
        f"[rss_bridge] النافذة الزمنية: "
        f"آخر {config.FETCH_WINDOW_HOURS} ساعات"
    )

    print(
        "============================================================"
    )

    session = requests.Session()

    try:

        for source in config.FACEBOOK_PAGES:

            source = str(
                source
            ).strip()

            if not source:
                continue

            print(
                ""
            )

            try:

                posts, report = _fetch_source(
                    session=session,
                    source=source,
                    cutoff=cutoff,
                )

                reports.append(
                    report
                )

                for post in posts:

                    post_id = post.get(
                        "id"
                    )

                    if not post_id:
                        continue

                    if post_id in global_seen:
                        continue

                    text = str(
                        post.get(
                            "text",
                            "",
                        )
                    ).strip()

                    if not text:
                        continue

                    global_seen.add(
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

                print(
                    f"[rss_bridge] {source}: "
                    f"الحالة النهائية = "
                    f"{report['status']} | "
                    f"طلبات ناجحة = "
                    f"{report['successful_requests']} | "
                    f"طلبات فاشلة = "
                    f"{report['failed_requests']} | "
                    f"Feeds صالحة فارغة = "
                    f"{report['valid_empty_feeds']} | "
                    f"المقبول = "
                    f"{report['accepted']}"
                )

            except Exception as exc:

                print(
                    f"[rss_bridge] {source}: "
                    f"خطأ غير متوقع: {exc}"
                )

                reports.append(
                    {
                        "source": source,
                        "successful_requests": 0,
                        "failed_requests": 0,
                        "valid_empty_feeds": 0,
                        "parse_failures": 0,
                        "accepted": 0,
                        "status": "خطأ غير متوقع",
                        "method": None,
                    }
                )

    finally:

        session.close()

    # ========================================================
    # التقرير النهائي
    # ========================================================

    print(
        ""
    )

    print(
        "============================================================"
    )

    print(
        "[rss_bridge] تقرير جميع المصادر"
    )

    print(
        "============================================================"
    )

    total_successful = 0
    total_failed = 0
    total_empty = 0
    total_accepted = 0

    for report in reports:

        source = report[
            "source"
        ]

        status = report[
            "status"
        ]

        accepted = report[
            "accepted"
        ]

        successful = report[
            "successful_requests"
        ]

        failed = report[
            "failed_requests"
        ]

        empty = report[
            "valid_empty_feeds"
        ]

        method = (
            report["method"]
            or "-"
        )

        total_successful += (
            successful
        )

        total_failed += (
            failed
        )

        total_empty += (
            empty
        )

        total_accepted += (
            accepted
        )

        print(
            f"[تقرير المصدر] {source}"
        )

        print(
            f"  الحالة       : {status}"
        )

        print(
            f"  الطريقة       : {method}"
        )

        print(
            f"  طلبات ناجحة   : {successful}"
        )

        print(
            f"  طلبات فاشلة   : {failed}"
        )

        print(
            f"  Feeds فارغة   : {empty}"
        )

        print(
            f"  منشورات مقبولة: {accepted}"
        )

    print(
        "------------------------------------------------------------"
    )

    print(
        f"[تقرير إجمالي] "
        f"المصادر: {len(reports)} | "
        f"طلبات ناجحة: {total_successful} | "
        f"طلبات فاشلة: {total_failed} | "
        f"Feeds فارغة: {total_empty} | "
        f"منشورات مقبولة: {total_accepted}"
    )

    print(
        "============================================================"
    )

    print(
        f"[rss_bridge] انتهى الجلب: "
        f"{len(results)} منشور مؤهل."
    )

    return results
