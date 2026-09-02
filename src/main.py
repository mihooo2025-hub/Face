"""
نقطة تشغيل الدورة الواحدة. يستدعيها GitHub Actions كل 6 ساعات.
"""

from . import config, fb_scraper, reporter, rewriter, state_manager, wp_publisher


def _word_count(text):
    return len(text.split())


def _process_one(item, state, stats, published_items):
    """
    يعالج خبرًا واحدًا: تحقق من الشروط -> إعادة صياغة -> نشر.
    يحاول عند الفشل حتى IMMEDIATE_RETRIES مرة داخل نفس الدورة قبل تأجيله.
    يرجع True عند النجاح أو التجاهل النهائي (أي انتهى أمره في هذه الدورة)،
    و False إذا وجب تأجيله لمحاولة لاحقة داخل نفس الدورة.
    """
    post_id = item["id"]

    # فحوصات رخيصة أولاً قبل استهلاك أي طلب API
    if not item.get("image_url"):
        print(f"[main] تجاهل {post_id}: لا توجد صورة بارزة")
        state_manager.mark_ignored_permanently(state, post_id)
        return True

    if _word_count(item["text"]) < config.MIN_WORDS:
        print(f"[main] تجاهل {post_id}: أقل من {config.MIN_WORDS} كلمة")
        state_manager.mark_ignored_permanently(state, post_id)
        return True

    try:
        title, body = rewriter.rewrite_article(item["text"])
        rewriter.sleep_between_rewrites()

        body_html = "".join(f"<p>{p}</p>" for p in body.split("\n\n") if p.strip())
        wp_publisher.publish_draft(title, body_html, item["image_url"])
        wp_publisher.sleep_between_publishes()

        state_manager.mark_published(state, post_id)
        stats["published"] += 1
        published_items.append({"new_title": title, "old_url": item["source_url"]})
        return True

    except (rewriter.RewriteError, wp_publisher.PublishError) as exc:
        print(f"[main] فشل معالجة {post_id}: {exc}")
        return False


def run_cycle():
    state = state_manager.load_state()
    stats = {"checked": 0, "published": 0, "failed": 0}
    published_items = []

    fresh_items = fb_scraper.fetch_recent_posts()
    # نضيف عناصر الدورات السابقة التي ما زالت قيد إعادة المحاولة ولم تُجلب مجددًا
    seen_ids = {i["id"] for i in fresh_items}
    # ملاحظة: لإعادة محاولة عناصر "pending" التي لم تعد ضمن آخر 6 ساعات،
    # يجب أن يحتفظ fb_scraper بنسخة كاملة منها إن رغبت لاحقًا بتوسيع الجلب؛
    # حاليًا نكتفي بمعالجة كل ما يُجلب من آخر 6 ساعات ونطبق منطق الدورات
    # على ما يفشل منها فقط.

    candidates = [i for i in fresh_items if not state_manager.is_known(state, i["id"])]
    stats["checked"] = len(candidates)

    for item in candidates:
        succeeded_or_final = False
        attempts = 0
        while attempts <= config.IMMEDIATE_RETRIES and not succeeded_or_final:
            attempts += 1
            succeeded_or_final = _process_one(item, state, stats, published_items)

        if not succeeded_or_final:
            outcome = state_manager.mark_pending_retry(state, item["id"])
            if outcome == "ignored":
                print(f"[main] تجاهل نهائي بعد استنفاد الدورات: {item['id']}")
            stats["failed"] += 1

    state_manager.save_state(state)
    reporter.send_report(stats, published_items)
    print(f"[main] انتهت الدورة: {stats}")


if __name__ == "__main__":
    run_cycle()
