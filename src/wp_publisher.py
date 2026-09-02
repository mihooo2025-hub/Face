"""
النشر على ووردبريس كمسودة، مع رفع الصورة البارزة وربطها بالتصنيف الثابت
"مقالات وتحليلات" فقط (لا يتم إنشاء أي تصنيف جديد إطلاقًا).
"""

import time

import requests
from requests.auth import HTTPBasicAuth

from . import config

_auth = HTTPBasicAuth(config.WP_USERNAME, config.WP_APP_PASSWORD)
_category_id_cache = None


class PublishError(Exception):
    pass


def _get_category_id():
    global _category_id_cache
    if config.WP_CATEGORY_ID:
        return config.WP_CATEGORY_ID
    if _category_id_cache:
        return _category_id_cache

    resp = requests.get(
        f"{config.WP_URL}/wp-json/wp/v2/categories",
        params={"search": config.WP_CATEGORY_NAME, "per_page": 20},
        auth=_auth,
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(f"تعذر جلب التصنيفات: {resp.status_code} {resp.text[:200]}")

    for cat in resp.json():
        if cat.get("name", "").strip() == config.WP_CATEGORY_NAME:
            _category_id_cache = cat["id"]
            return _category_id_cache

    raise PublishError(
        f'لم يتم العثور على تصنيف باسم "{config.WP_CATEGORY_NAME}" في ووردبريس. '
        "يجب إنشاؤه يدويًا مرة واحدة (الأداة لن تنشئ تصنيفات جديدة أبدًا)."
    )


def _upload_featured_image(image_url):
    img_resp = requests.get(image_url, timeout=30)
    if img_resp.status_code != 200 or not img_resp.content:
        raise PublishError(f"تعذر تحميل الصورة البارزة من {image_url}")

    filename = "featured-" + str(int(time.time())) + ".jpg"
    media_resp = requests.post(
        f"{config.WP_URL}/wp-json/wp/v2/media",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": img_resp.headers.get("Content-Type", "image/jpeg"),
        },
        data=img_resp.content,
        auth=_auth,
        timeout=60,
    )
    if media_resp.status_code not in (200, 201):
        raise PublishError(f"تعذر رفع الصورة: {media_resp.status_code} {media_resp.text[:200]}")

    return media_resp.json()["id"]


def publish_draft(title, body_html, image_url):
    """ينشر مقالًا كمسودة، ويعيد رابط تعديل المسودة في ووردبريس."""
    category_id = _get_category_id()
    media_id = _upload_featured_image(image_url)

    payload = {
        "title": title,
        "content": body_html,
        "status": "draft",
        "categories": [category_id],
        "featured_media": media_id,
    }
    resp = requests.post(
        f"{config.WP_URL}/wp-json/wp/v2/posts",
        json=payload,
        auth=_auth,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise PublishError(f"فشل إنشاء المسودة: {resp.status_code} {resp.text[:300]}")

    post = resp.json()
    return post.get("link") or post.get("id")


def sleep_between_publishes():
    time.sleep(config.PUBLISH_DELAY_SECONDS)
