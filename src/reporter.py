"""
إرسال تقرير نهاية الدورة إلى قناة تيليجرام عبر بوت.
كل سطر خبر: العنوان الجديد كنص، والرابط القديم (المختصر) موضوع خلف النص كرابط.
"""

import requests

from . import config


def _shorten(url):
    try:
        resp = requests.get("https://tinyurl.com/api-create.php", params={"url": url}, timeout=15)
        if resp.status_code == 200 and resp.text.startswith("http"):
            return resp.text.strip()
    except requests.RequestException:
        pass
    return url  # في حال فشل الاختصار نستخدم الرابط الأصلي كحل بديل


def send_report(stats, published_items):
    """
    stats: dict فيه checked / published / failed / drafted (drafted = published هنا لأن كل ما ينشر يكون مسودة)
    published_items: قائمة [{"new_title": ..., "old_url": ...}, ...]
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[reporter] لا يوجد إعدادات تيليجرام، تم تخطي إرسال التقرير.")
        return

    lines = [
        "<b>تقرير دورة نشر الأخبار</b>",
        f"عدد الأخبار المفحوصة: {stats['checked']}",
        f"عدد الأخبار المنشورة (كمسودة): {stats['published']}",
        f"عدد الأخبار الفاشلة: {stats['failed']}",
        "",
    ]

    for item in published_items:
        short_link = _shorten(item["old_url"])
        lines.append(f'<a href="{short_link}">{item["new_title"]}</a>')

    text = "\n".join(lines)

    resp = requests.post(
        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"[reporter] فشل إرسال التقرير: {resp.status_code} {resp.text[:200]}")
