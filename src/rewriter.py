"""
إعادة صياغة نص الخبر عبر Gemini، مع تبديل تلقائي بين نموذجين ومفتاحين
عند فشل الأول.
"""

import time

import requests

from . import config

with open(config.REWRITE_PROMPT_FILE, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()


class RewriteError(Exception):
    pass


def _call_gemini(model, api_key, source_text):
    if not api_key:
        raise RewriteError(f"لا يوجد مفتاح API لنموذج {model}")

    url = config.GEMINI_ENDPOINT_TEMPLATE.format(model=model, key=api_key)
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"{SYSTEM_PROMPT}\n\nالنص الأصلي:\n{source_text}"}]}
        ],
        "generationConfig": {"temperature": 0.4},
    }
    response = requests.post(url, json=payload, timeout=60)
    if response.status_code != 200:
        raise RewriteError(f"{model} فشل بالحالة {response.status_code}: {response.text[:300]}")

    data = response.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RewriteError(f"استجابة غير متوقعة من {model}: {exc}") from exc

    if not text:
        raise RewriteError(f"{model} أعاد نصًا فارغًا")
    return text


def rewrite_article(source_text):
    """
    يحاول النموذج الأساسي أولاً ثم الاحتياطي عند الفشل.
    يرجع (العنوان, نص_الخبر) أو يرفع RewriteError إذا فشل الاثنان.
    """
    last_error = None
    for model, key in (
        (config.GEMINI_PRIMARY_MODEL, config.GEMINI_PRIMARY_KEY),
        (config.GEMINI_FALLBACK_MODEL, config.GEMINI_FALLBACK_KEY),
    ):
        try:
            raw = _call_gemini(model, key, source_text)
            return _split_title_and_body(raw)
        except RewriteError as exc:
            print(f"[rewriter] {exc}")
            last_error = exc
            continue
    raise RewriteError(f"فشل كل من النموذج الأساسي والاحتياطي: {last_error}")


def _split_title_and_body(raw_text):
    """المخرجات المطلوبة: السطر الأول عنوان، والباقي نص الخبر."""
    lines = [line for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise RewriteError("لا يمكن فصل العنوان عن النص من مخرجات النموذج")
    title = lines[0].strip().lstrip("#").strip()
    body = "\n\n".join(lines[1:]).strip()
    if not body:
        raise RewriteError("نص الخبر فارغ بعد فصل العنوان")
    return title, body


def sleep_between_rewrites():
    time.sleep(config.REWRITE_DELAY_SECONDS)
