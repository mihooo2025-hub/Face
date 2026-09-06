"""
جلب المنشورات العامة من فيسبوك باستخدام متصفح Chromium عبر Playwright.

هذا الجالب لا يستخدم تسجيل الدخول أو FACEBOOK_COOKIES.
يقرأ فقط المحتوى الذي يعرضه فيسبوك للزائر العادي.

ملاحظة:
- فيسبوك قد يعرض تسجيل دخول أو تحققًا لبعض الصفحات.
- بنية فيسبوك تتغير باستمرار، لذلك يحتوي الكود على عدة طرق لاستخراج
  النص والرابط والصورة ووقت المنشور.
- لا يتم اعتبار المنشور صالحًا إذا تعذر معرفة وقت نشره، لأن المشروع
  يعتمد على نافذة آخر 6 ساعات.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from . import config


BASE_URL = "https://www.facebook.com/"


def _normalize_url(page):
    page = str(page).strip()

    if page.startswith("http://") or page.startswith("https://"):
        return page

    return urljoin(BASE_URL, page.lstrip("/"))


def _parse_timestamp(value):
    if not value:
        return None

    try:
        timestamp = int(str(value).strip())
        if timestamp > 1000000000:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass

    return None


def _extract_post_time(article):
    """
    يحاول استخراج وقت المنشور من أكثر من موضع شائع في HTML.
    """

    # 1) data-utime
    selectors = [
        "[data-utime]",
        "abbr[data-utime]",
        "time[data-utime]",
    ]

    for selector in selectors:
        try:
            elements = article.locator(selector).all()
            for element in elements:
                value = element.get_attribute("data-utime")
                parsed = _parse_timestamp(value)
                if parsed:
                    return parsed
        except Exception:
            pass

    # 2) datetime داخل <time>
    try:
        elements = article.locator("time[datetime]").all()
        for element in elements:
            value = element.get_attribute("datetime")
            if not value:
                continue

            try:
                value = value.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass

    return None


def _extract_post_url(article, page_url):
    """
    يحاول العثور على الرابط المباشر للمنشور.
    """

    selectors = [
        'a[href*="/posts/"]',
        'a[href*="/permalink/"]',
        'a[href*="story.php"]',
        'a[href*="permalink.php"]',
    ]

    for selector in selectors:
        try:
            links = article.locator(selector).all()

            for link in links:
                href = link.get_attribute("href")
                if not href:
                    continue

                href = urljoin(BASE_URL, href)

                if "facebook.com" in href:
                    return href
        except Exception:
            pass

    # محاولة أخيرة من جميع الروابط داخل المنشور
    try:
        links = article.locator("a[href]").all()

        for link in links:
            href = link.get_attribute("href")

            if not href:
                continue

            if any(
                marker in href
                for marker in (
                    "/posts/",
                    "/permalink/",
                    "story.php",
                    "permalink.php",
                )
            ):
                return urljoin(BASE_URL, href)
    except Exception:
        pass

    return page_url


def _extract_image(article):
    """
    يستخرج أكبر صورة محتملة من المنشور.
    """

    candidates = []

    try:
        images = article.locator("img").all()

        for image in images:
            src = image.get_attribute("src")

            if not src:
                continue

            width = image.get_attribute("width")
            height = image.get_attribute("height")

            try:
                area = int(width or 0) * int(height or 0)
            except ValueError:
                area = 0

            candidates.append((area, src))
    except Exception:
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)

    return candidates[0][1]


def _extract_text(article):
    """
    يستخرج النص الظاهر داخل المنشور.
    """

    selectors = [
        '[data-ad-preview="message"]',
        '[data-ad-comet-preview="message"]',
    ]

    for selector in selectors:
        try:
            element = article.locator(selector).first

            if element.count():
                text = element.inner_text(timeout=2000).strip()

                if text:
                    return text
        except Exception:
            pass

    # طريقة احتياطية
    try:
        text = article.inner_text(timeout=3000).strip()

        if text:
            return text
    except Exception:
        pass

    return ""


def _looks_like_login_page(page):
    try:
        current_url = page.url.lower()

        if "login" in current_url:
            return True

        body_text = page.locator("body").inner_text(timeout=3000).lower()

        markers = [
            "log in",
            "تسجيل الدخول",
            "create new account",
            "إنشاء حساب جديد",
        ]

        return any(marker in body_text for marker in markers)

    except Exception:
        return False


def _looks_like_challenge_page(page):
    try:
        current_url = page.url.lower()

        markers = [
            "checkpoint",
            "challenge",
            "captcha",
            "security",
        ]

        if any(marker in current_url for marker in markers):
            return True

        html = page.content().lower()

        return any(marker in html for marker in markers)

    except Exception:
        return False


def _collect_page_posts(page, source, cutoff):
    """
    يجمع المنشورات الظاهرة حاليًا في الصفحة.
    """

    results = []
    seen_ids = set()

    try:
        articles = page.locator('[role="article"]').all()
    except Exception:
        articles = []

    for article in articles:
        try:
            post_time = _extract_post_time(article)

            # لا نقبل منشورًا لا نستطيع تحديد وقته،
            # لأن المشروع يعمل على نافذة زمنية محددة.
            if post_time is None:
                continue

            if post_time < cutoff:
                continue

            text = _extract_text(article)

            if not text:
                continue

            post_url = _extract_post_url(article, source)

            image_url = _extract_image(article)

            if not image_url:
                continue

            post_id = post_url

            if not post_id:
                post_id = f"{source}:{post_time.isoformat()}:{text[:100]}"

            if post_id in seen_ids:
                continue

            seen_ids.add(post_id)

            results.append(
                {
                    "id": str(post_id),
                    "source_url": post_url,
                    "text": text,
                    "image_url": image_url,
                }
            )

        except Exception:
            continue

    return results


def fetch_recent_posts():
    """
    يرجع المنشورات العامة الجديدة خلال آخر FETCH_WINDOW_HOURS ساعة.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=config.FETCH_WINDOW_HOURS
    )

    results = []
    global_seen = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            locale="ar-SA",
            timezone_id="Asia/Riyadh",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        try:
            for source in config.FACEBOOK_PAGES:
                page_url = _normalize_url(source)

                raw_count = 0
                kept_count = 0

                print(f"[fb_browser] بدء جلب: {source}")

                try:
                    response = page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )

                    if response:
                        print(
                            f"[fb_browser] {source}: "
                            f"HTTP {response.status}"
                        )

                    page.wait_for_timeout(5000)

                    if _looks_like_login_page(page):
                        print(
                            f"[fb_browser] {source}: "
                            "فيسبوك أعاد صفحة تسجيل الدخول."
                        )
                        continue

                    if _looks_like_challenge_page(page):
                        print(
                            f"[fb_browser] {source}: "
                            "فيسبوك أعاد صفحة تحقق/حماية."
                        )
                        continue

                    # تحميل المزيد من المنشورات عن طريق التمرير.
                    for _ in range(3):
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(2500)

                    try:
                        raw_count = page.locator(
                            '[role="article"]'
                        ).count()
                    except Exception:
                        raw_count = 0

                    source_posts = _collect_page_posts(
                        page,
                        page_url,
                        cutoff,
                    )

                    for item in source_posts:
                        if item["id"] in global_seen:
                            continue

                        global_seen.add(item["id"])
                        results.append(item)
                        kept_count += 1

                    print(
                        f"[fb_browser] {source}: "
                        f"تم العثور على {raw_count} عنصر منشور، "
                        f"تم قبول {kept_count}"
                    )

                    if raw_count == 0:
                        print(
                            f"[fb_browser] {source}: "
                            "لم تظهر عناصر role=article. "
                            "قد يكون فيسبوك أخفى المحتوى أو غيّر بنية الصفحة."
                        )

                except PlaywrightTimeoutError:
                    print(
                        f"[fb_browser] {source}: "
                        "انتهت مهلة تحميل الصفحة."
                    )

                except Exception as exc:
                    print(
                        f"[fb_browser] {source}: "
                        f"خطأ أثناء الجلب: {exc}"
                    )

        finally:
            context.close()
            browser.close()

    print(
        f"[fb_browser] انتهى الجلب: "
        f"{len(results)} منشور مؤهل"
    )

    return results
