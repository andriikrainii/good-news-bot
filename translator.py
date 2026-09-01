"""Переклад новин українською: через Claude API або безкоштовно через Google."""

import json
import os
import re

import requests

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
REQUEST_TIMEOUT = 60

SYSTEM_PROMPT = """Ти редактор телеграм-каналу добрих новин для української родини.

Тобі дають заголовок і опис новини англійською (або іншою мовою).
Ти робиш дві речі одразу:

1. ОЦІНЮЄШ, чи ця новина справді добра і доречна.
   Постав "ok": false, якщо новина:
   - насправді сумна, тривожна, страшна;
   - про війну, зброю, смерть, хворобу з поганим кінцем, катастрофу, злочин;
   - про політику, вибори, суди;
   - це реклама, розпродаж, промокод;
   - це підбірка посилань, дайджест, анонс подій, гороскоп, вікторина;
   - у ній немає жодної конкретної доброї події.

2. Якщо новина добра — ПЕРЕКЛАДАЄШ її українською.
   - Заголовок: короткий, живий, до 90 символів, з ОДНИМ емодзі на початку.
   - Текст: 2-4 речення, {style}.
   - Пиши природною українською, не калькою з англійської.
   - НЕ ВИГАДУЙ фактів. Тільки те, що є в оригіналі.
   - Цифри, імена, назви міст і країн зберігай точно.
   - Без слів "стаття", "матеріал", "читайте далі".

Відповідай ЛИШЕ одним JSON-об'єктом, без пояснень і без розмітки:
{{"ok": true, "reason": "", "title": "заголовок", "text": "2-4 речення"}}

Якщо ok: false — у "reason" коротко напиши причину українською,
а "title" і "text" залиш порожніми."""


class Translator:
    """Обгортка над перекладом. Сама вирішує, який спосіб доступний."""

    def __init__(self, config):
        settings = config.get("translation", {}) or {}
        self.model = settings.get("model") or DEFAULT_MODEL
        self.style = settings.get("style") or "тепло, просто і по-людськи"
        self.max_chars = int(settings.get("max_chars") or 600)
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        provider = (settings.get("provider") or "google").strip().lower()
        if provider == "claude" and not self.api_key:
            print("УВАГА: у config.yaml вибрано provider: claude, але ключа "
                  "ANTHROPIC_API_KEY немає. Переходжу на безкоштовний Google-переклад.")
            provider = "google"
        self.provider = provider

        self._google = None
        if self.provider != "claude":
            self._google = self._make_google()

    # ---------- Google ----------

    @staticmethod
    def _make_google():
        try:
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source="auto", target="uk")
        except Exception as exc:  # pragma: no cover - залежить від мережі
            print(f"УВАГА: Google-перекладач недоступний: {exc}")
            return None

    def _google_text(self, text):
        if not text:
            return ""
        if self._google is None:
            self._google = self._make_google()
        if self._google is None:
            return text
        try:
            # У Google обмеження ~5000 символів на запит.
            return self._google.translate(text[:4500]) or text
        except Exception as exc:
            print(f"    Google-переклад не спрацював: {exc}")
            return text

    def _translate_google(self, item):
        summary = item.get("summary", "")
        if len(summary) > self.max_chars:
            summary = _cut_by_sentence(summary, self.max_chars)
        return {
            "ok": True,
            "reason": "",
            "title": self._google_text(item.get("title", "")),
            "text": self._google_text(summary),
        }

    # ---------- Claude ----------

    def _translate_claude(self, item):
        user_text = (
            f"ЗАГОЛОВОК: {item.get('title', '')}\n\n"
            f"ОПИС: {item.get('summary', '')[:2000]}\n\n"
            f"ДЖЕРЕЛО: {item.get('source', '')}"
        )
        payload = {
            "model": self.model,
            "max_tokens": 700,
            "system": SYSTEM_PROMPT.format(style=self.style),
            "messages": [{"role": "user", "content": user_text}],
        }
        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"    Claude API недоступний ({exc}), беру Google-переклад")
            return self._translate_google(item)

        if resp.status_code != 200:
            print(f"    Claude API повернув {resp.status_code}: {resp.text[:200]}")
            return self._translate_google(item)

        try:
            body = resp.json()
            raw = "".join(
                block.get("text", "")
                for block in body.get("content", [])
                if block.get("type") == "text"
            )
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, KeyError, TypeError) as exc:
            print(f"    не вдалося розібрати відповідь Claude ({exc}), беру Google")
            return self._translate_google(item)

        if not data.get("ok"):
            return {
                "ok": False,
                "reason": data.get("reason") or "модель вважає новину недоречною",
                "title": "",
                "text": "",
            }

        title = (data.get("title") or "").strip()
        text = (data.get("text") or "").strip()
        if not title or not text:
            return self._translate_google(item)
        if len(text) > self.max_chars:
            text = _cut_by_sentence(text, self.max_chars)
        return {"ok": True, "reason": "", "title": title, "text": text}

    # ---------- Загальний вхід ----------

    def process(self, item):
        """Повертає {'ok', 'reason', 'title', 'text'} українською."""
        if self.provider == "claude":
            return self._translate_claude(item)
        return self._translate_google(item)


def _strip_code_fence(raw):
    """Прибрати ```json ... ``` навколо відповіді моделі."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()


def _cut_by_sentence(text, limit):
    """Обрізати текст по межі речення, а якщо не вийшло — по межі слова."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for mark in (". ", "! ", "? ", "…"):
        idx = head.rfind(mark)
        if idx > limit * 0.5:
            return head[:idx + 1].strip()
    idx = head.rfind(" ")
    if idx > 0:
        head = head[:idx]
    return head.rstrip(" ,;:—-") + "…"
