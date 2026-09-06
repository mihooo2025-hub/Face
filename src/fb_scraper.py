"""
جلب منشورات Facebook عبر RSS-Bridge.

الهدف:
- الاعتماد على RSS-Bridge كحل مجاني بدون FACEBOOK_COOKIES.
- تجربة أكثر من صيغة لطلب FacebookBridge.
- عدم إيقاف الدورة إذا فشل مصدر واحد.
- قبول Atom/RSS فقط إذا كانت الاستجابة Feed فعلية.
- استخراج العنوان والنص والرابط والصورة ووقت النشر.
- الاقتصار على المنشورات خلال نافذة FETCH_WINDOW_HOURS.
- إزالة العناصر غير الصالحة قبل إرسالها إلى بقية المشروع.

ملاحظة:
RSS-Bridge يعتمد على إمكانية الوصول إلى Facebook، لذلك لا يمكن ضمان
نجاح جميع الصفحات إذا كان Facebook يمنع الوصول إليها أو يتطلب تسجيل الدخول.
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import quote, urljoin, urlparse

import requests
import xml.etree.ElementTree as ET

from . import config


# ---------------------------------------------------------------------
# إعدادات RSS-Bridge
# ---------------------------------------------------------------------

RSS_BRIDGE_URL = "https://rss-bridge.org/bridge01/"

REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/atom+xml, application/rss+xml, "
        "application/xml, text/xml;q=0.9, */*;q=0.8"
    ),
    "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


# ---------------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------------

def _normalize_source(source):
    """
    يحول اسم الصفحة أو الرابط إلى قيمة مناسبة لـ RSS-Bridge.
    """

    source = str(source).strip()

    if not source:
        return ""

    # إذا كان المستخدم وضع رابط Facebook كاملًا،
    # نحاول استخراج الجزء الأخير منه.
    if source.startswith("http://") or source.startswith("https://"):
        parsed = urlparse(source)

        path = parsed.path.strip("/")

        if path:
            parts = [part for part in path.split("/") if part]

            if parts:
                return parts[0]

        return source

    return source.strip("/")


def _build_urls(source):
    """
    يبني عدة طلبات مجانية محتملة لنفس المصدر.

    الصيغ تعتمد على FacebookBridge/FB2Bridge الموجودة في RSS-Bridge.
    """

    username = _normalize_source(source)

    if not username:
        return []

    encoded_username = quote(username, safe="")

    urls = [
        (
            f"{RSS_BRIDGE_URL}"
            f"?action=display"
            f"&bridge=FacebookBridge"
            f"&context=User"
            f"&u={encoded_username}"
            f"&media_type=all"
            f"&limit=-1"
            f"&format=Atom"
        ),
        (
            f"{RSS_BRIDGE_URL}"
            f"?action=display"
            f"&bridge=FacebookBridge"
            f"&context=User"
            f"&u={encoded_username}"
            f"&media_type=all"
            f"&limit=-1"
            f"&format=RSS"
        ),
        (
            f"{RSS_BRIDGE_URL}"
            f"?action=display"
            f"&bridge=FB2Bridge"
            f"&u={encoded_username}"
            f"&abbrev_name=on"
            f"&format=Atom"
        ),
        (
            f"{RSS_BRIDGE_URL}"
            f"?action=display"
            f"&bridge=FB2Bridge"
            f"&u={encoded_username}"
            f"&abbrev_name=on"
            f"&format=RSS"
        ),
    ]

    # إزالة أي تكرار مع الحفاظ على الترتيب.
    unique_urls = []
    seen = set()

    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return unique_urls


def _clean_html(value):
    """
    يحول HTML البسيط إلى نص نظيف.
    """

    if not value:
        return ""

    value = unescape(str(value))

    value = re.sub(
        r"<br\s*/?>",
        "\n",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"</p\s*>",
        "\n\n",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def _extract_image_from_html(value):
    """
    يحاول استخراج أول صورة من محتوى RSS/Atom.
    """

    if not value:
        return None

    value = unescape(str(value))

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
            image_url = match.group(1).strip()

            if image_url:
                return image_url

    return None


def _parse_datetime(value):
    """
    يدعم أشهر صيغ التاريخ في RSS/Atom.
    """

    if not value:
        return None

    value = str(value).strip()

    # Unix timestamp
    try:
        timestamp = int(value)

        if timestamp > 1000000000:
            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            )
    except (ValueError, TypeError, OverflowError):
        pass

    # ISO 8601
    try:
        normalized = value.replace("Z", "+00:00")

        parsed = datetime.fromisoformat(normalized)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except ValueError:
        pass

    # RFC 2822 / RSS
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError, OverflowError):
        return None


def _find_text(element, names):
    """
    يبحث عن أول عنصر XML من الأسماء المحددة.
    """

    for name in names:
        found = element.find(name)

        if found is not None and found.text:
            return found.text.strip()

    return ""


def _find_atom_link(element):
    """
    يستخرج رابط entry من Atom.
    """

    atom_namespace = "{http://www.w3.org/2005/Atom}"

    for link in element.findall(f"{atom_namespace}link"):
        href = link.attrib.get("href")

        if href:
            return href.strip()

    # fallback
    for link in element.findall("link"):
        href = link.attrib.get("href")

        if href:
            return href.strip()

        if link.text:
            return link.text.strip()

    return ""


def _parse_feed(xml_text, source, cutoff):
    """
    يحلل Atom أو RSS ويعيد العناصر الصالحة.
    """

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(
            f"[rss_bridge] {source}: "
            f"تعذر تحليل XML: {exc}"
        )
        return []

    results = []

    root_tag = root.tag.lower()

    is_atom = (
        "feed" in root_tag
        or "atom" in root_tag
    )

    if is_atom:
        atom_namespace = "{http://www.w3.org/2005/Atom}"

        entries = root.findall(f"{atom_namespace}entry")

        if not entries:
            entries = root.findall("entry")

        for entry in entries:
            title = _find_text(
                entry,
                [
                    f"{atom_namespace}title",
                    "title",
                ],
            )

            content = _find_text(
                entry,
                [
                    f"{atom_namespace}content",
                    f"{atom_namespace}summary",
                    "content",
                    "summary",
                ],
            )

            link = _find_atom_link(entry)

            published = _find_text(
                entry,
                [
                    f"{atom_namespace}published",
                    f"{atom_namespace}updated",
                    "published",
                    "updated",
                ],
            )

            author = ""

            author_element = entry.find(
                f"{atom_namespace}author/"
                f"{atom_namespace}name"
            )

            if author_element is not None and author_element.text:
                author = author_element.text.strip()

            _add_feed_item(
                results=results,
                title=title,
                content=content,
                link=link,
                published=published,
                source=source,
                author=author,
                cutoff=cutoff,
            )

    else:
        # RSS 2.0 / RSS 1.0
        channel = root.find("channel")

        if channel is not None:
            items = channel.findall("item")
        else:
            items = root.findall(".//item")

        for item in items:
            title = _find_text(
                item,
                ["title"],
            )

            content = _find_text(
                item,
                [
                    "description",
                    "content:encoded",
                ],
            )

            link = _find_text(
                item,
                ["link"],
            )

            if not link:
                guid = _find_text(
                    item,
                    ["guid"],
                )

                if guid.startswith("http"):
                    link = guid

            published = _find_text(
                item,
                [
                    "pubDate",
                    "published",
                    "updated",
                ],
            )

            author = _find_text(
                item,
                [
                    "author",
                    "dc:creator",
                ],
            )

            _add_feed_item(
                results=results,
                title=title,
                content=content,
                link=link,
                published=published,
                source=source,
                author=author,
                cutoff=cutoff,
            )

    return results


def _add_feed_item(
    results,
    title,
    content,
    link,
    published,
    source,
    author,
    cutoff,
):
    """
    يحول عنصر RSS/Atom إلى الشكل الذي يستخدمه المشروع.
    """

    post_time = _parse_datetime(published)

    # المشروع يعتمد على نافذة زمنية؛
    # لذلك لا نقبل عنصرًا بلا تاريخ.
    if post_time is None:
        return

    if post_time < cutoff:
        return

    title = _clean_html(title)
    content_html = content or ""

    text = _clean_html(content_html)

    if not text:
        text = title

    if not text:
        return

    link = unescape(str(link or "")).strip()

    if link:
        source_url = urljoin(
            "https://www.facebook.com/",
            link,
        )
    else:
        source_url = f"https://www.facebook.com/{source}"

    image_url = _extract_image_from_html(content_html)

    # بعض خلاصات RSS-Bridge تضع الصورة داخل enclosure.
    # هذه القيمة ستُضاف لاحقًا من XML إن كانت متاحة، لذلك لا نعتمد
    # عليها هنا وحدها.

    unique_base = (
        link
        or f"{source}|{post_time.isoformat()}|{text[:200]}"
    )

    post_id = hashlib.sha1(
        unique_base.encode("utf-8")
    ).hexdigest()

    results.append(
        {
            "id": f"rss:{post_id}",
            "source_url": source_url,
            "text": text,
            "image_url": image_url,
            "published_at": post_time,
            "title": title,
            "author": author,
        }
    )


def _extract_feed_images(xml_text):
    """
    يستخرج صور enclosure/media من XML.

    يعيد الصور بنفس ترتيب عناصر RSS/Atom تقريبًا.
    """

    images = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return images

    for element in root.iter():
        tag = element.tag.lower()

        if (
            tag.endswith("enclosure")
            or tag.endswith("content")
            or tag.endswith("thumbnail")
        ):
            url = (
                element.attrib.get("url")
                or element.attrib.get("href")
            )

            if url:
                url = unescape(url.strip())

                if url.startswith("http"):
                    images.append(url)

    return images


def _response_is_feed(response):
    """
    يتحقق من أن الاستجابة تبدو كـ RSS/Atom فعلية.
    """

    content_type = (
        response.headers.get(
            "Content-Type",
            "",
        )
        .lower()
    )

    text = response.text.lstrip()

    if (
        "xml" in content_type
        or "atom" in content_type
        or "rss" in content_type
    ):
        return (
            text.startswith("<?xml")
            or "<feed" in text[:1000]
            or "<rss" in text[:1000]
        )

    return (
        text.startswith("<?xml")
        or "<feed" in text[:1000]
        or "<rss" in text[:1000]
    )


def _request_feed(session, url, source, attempt_number):
    """
    ينفذ طلبًا واحدًا إلى RSS-Bridge.
    """

    try:
        response = session.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

    except requests.RequestException as exc:
        print(
            f"[rss_bridge] {source}: "
            f"خطأ اتصال في المحاولة {attempt_number}: {exc}"
        )
        return None

    content_type = response.headers.get(
        "Content-Type",
        "",
    )

    print(
        f"[rss_bridge] {source}: "
        f"المحاولة {attempt_number} "
        f"HTTP={response.status_code} "
        f"Content-Type={content_type}"
    )

    if response.status_code != 200:
        # لا نطبع كامل صفحة HTML حتى لا يمتلئ سجل GitHub.
        preview = response.text[:500].replace(
            "\n",
            " ",
        )

        print(
            f"[rss_bridge] {source}: "
            f"الاستجابة غير ناجحة: {preview}"
        )

        return None

    if not _response_is_feed(response):
        preview = response.text[:500].replace(
            "\n",
            " ",
        )

        print(
            f"[rss_bridge] {source}: "
            "الاستجابة ليست RSS/Atom صالحة. "
            f"المحتوى: {preview}"
        )

        return None

    return response


def _fetch_source(session, source, cutoff):
    """
    يحاول جميع صيغ RSS-Bridge المتاحة للمصدر.
    """

    urls = _build_urls(source)

    if not urls:
        print(
            f"[rss_bridge] {source}: "
            "معرّف الصفحة فارغ."
        )
        return []

    for index, url in enumerate(urls, start=1):
        response = _request_feed(
            session,
            url,
            source,
            index,
        )

        if response is None:
            continue

        posts = _parse_feed(
            response.text,
            source,
            cutoff,
        )

        if not posts:
            print(
                f"[rss_bridge] {source}: "
                f"الاستجابة صحيحة لكن لم نجد منشورات "
                f"ضمن آخر {config.FETCH_WINDOW_HOURS} ساعات."
            )

            # لا ننتقل بالضرورة إلى جسر آخر إذا كان الـ Feed صالحًا
            # لكنه لا يحتوي منشورات حديثة.
            continue

        # إضافة الصور من enclosure/media إذا كانت متوفرة.
        feed_images = _extract_feed_images(
            response.text
        )

        if feed_images:
            for index, item in enumerate(posts):
                if (
                    not item.get("image_url")
                    and index < len(feed_images)
                ):
                    item["image_url"] = feed_images[index]

        return posts

    print(
        f"[rss_bridge] {source}: "
        "فشلت جميع محاولات RSS-Bridge."
    )

    return []


# ---------------------------------------------------------------------
# الدالة الرئيسية التي يستخدمها src.main
# ---------------------------------------------------------------------

def fetch_recent_posts():
    """
    يجلب المنشورات الجديدة من جميع صفحات Facebook
    خلال آخر FETCH_WINDOW_HOURS ساعة.

    يرجع قائمة بالشكل:

    {
        "id": "...",
        "source_url": "...",
        "text": "...",
        "image_url": "...",
    }
    """

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=config.FETCH_WINDOW_HOURS
        )
    )

    results = []
    global_seen = set()

    session = requests.Session()

    try:
        print("[rss_bridge] بدء الجلب...")

        for source in config.FACEBOOK_PAGES:
            source = str(source).strip()

            if not source:
                continue

            print(
                f"[rss_bridge] {source}: "
                "بدء محاولة الجلب"
            )

            try:
                posts = _fetch_source(
                    session,
                    source,
                    cutoff,
                )

                accepted = 0

                for item in posts:
                    item_id = item.get("id")

                    if not item_id:
                        continue

                    if item_id in global_seen:
                        continue

                    # يجب أن يحتوي المنشور على نص.
                    if not item.get("text", "").strip():
                        continue

                    global_seen.add(item_id)

                    results.append(
                        {
                            "id": item_id,
                            "source_url": item.get(
                                "source_url",
                                source,
                            ),
                            "text": item.get(
                                "text",
                                "",
                            ).strip(),
                            "image_url": item.get(
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
                # فشل صفحة واحدة لا يجب أن يوقف بقية الصفحات.
                print(
                    f"[rss_bridge] {source}: "
                    f"خطأ غير متوقع: {exc}"
                )
                continue

    finally:
        session.close()

    print(
        f"[rss_bridge] انتهى الجلب: "
        f"{len(results)} منشور مؤهل."
    )

    return results
