"""
يدير هذا الملف "الذاكرة" بين كل دورة وأخرى:
- منع تكرار نشر نفس الخبر.
- الاحتفاظ بالأخبار التي فشلت مؤقتًا لإعادة تجربتها في الدورات الست التالية.
الحالة تُحفظ في data/state.json ويجب أن يقوم الـ workflow بعمل commit لهذا الملف
بعد كل تشغيل حتى تبقى الذاكرة محفوظة بين الدورات (راجع ملف الـ workflow المرفق).
"""

import json
import os
from datetime import datetime, timezone

from . import config


def _empty_state():
    return {
        "published": [],   # روابط/معرّفات المنشورات التي نُشرت بنجاح (لا تُعاد أبدًا)
        "ignored": [],      # منشورات جرى تجاهلها نهائيًا (بدون صورة / أقل من 90 كلمة / انتهت محاولاتها)
        "pending": {},       # منشورات لا تزال قيد إعادة المحاولة عبر الدورات: {post_id: {"cycle_attempts": N, "first_seen": iso}}
    }


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return _empty_state()
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, default in _empty_state().items():
            data.setdefault(key, default)
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_state()


def save_state(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_known(state, post_id):
    """هل سبق التعامل مع هذا المنشور بشكل نهائي (منشور أو مُتجاهَل)؟"""
    return post_id in state["published"] or post_id in state["ignored"]


def get_cycle_attempts(state, post_id):
    entry = state["pending"].get(post_id)
    return entry["cycle_attempts"] if entry else 0


def mark_published(state, post_id):
    state["published"].append(post_id)
    state["pending"].pop(post_id, None)


def mark_ignored_permanently(state, post_id):
    state["ignored"].append(post_id)
    state["pending"].pop(post_id, None)


def mark_pending_retry(state, post_id):
    """فشل الخبر بعد المحاولات الفورية داخل هذه الدورة: يُحفظ لإعادة تجربته بالدورة القادمة."""
    entry = state["pending"].get(post_id, {
        "cycle_attempts": 0,
        "first_seen": datetime.now(timezone.utc).isoformat(),
    })
    entry["cycle_attempts"] += 1
    if entry["cycle_attempts"] >= config.MAX_CYCLE_RETRIES:
        # استنفد عدد الدورات المسموح بها -> يُتجاهل نهائيًا
        state["ignored"].append(post_id)
        state["pending"].pop(post_id, None)
        return "ignored"
    state["pending"][post_id] = entry
    return "pending"
