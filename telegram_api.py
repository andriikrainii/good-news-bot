"""Надсилання постів у Telegram."""

import html

import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
REQUEST_TIMEOUT = 45

# Ліміт Telegram на підпис під фото.
CAPTION_LIMIT = 1024
# Ліміт на звичайне текстове повідомлення.
MESSAGE_LIMIT = 4096


def escape(text):
    """Екранувати HTML, щоб Telegram не сприйняв символи як розмітку."""
    return html.escape(text or "", quote=False)


def _trim_to_words(text, limit):
    """Обрізати текст по межі слова."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    idx = head.rfind(" ")
    if idx > limit * 0.5:
        head = head[:idx]
    return head.rstrip(" ,;:—-.") + "…"


def build_post(title, text, link, source_name, footer):
    """Зібрати HTML-текст поста."""
    parts = [f"<b>{escape(title)}</b>", "", escape(text), ""]
    parts.append(f'🔗 <a href="{escape(link)}">{escape(source_name)}</a>')
    if footer:
        parts.append(escape(footer))
    return "\n".join(parts)


def _fit(body, limit):
    """Вкласти пост у ліміт, вкорочуючи саме основний текст."""
    if len(body) <= limit:
        return body
    lines = body.split("\n")
    # lines[2] — основний текст поста.
    if len(lines) > 2:
        overflow = len(body) - limit
        target = max(40, len(lines[2]) - overflow - 1)
        lines[2] = _trim_to_words(lines[2], target)
        body = "\n".join(lines)
    return body[:limit]


class TelegramSender:
    def __init__(self, token, chat_id):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def _call(self, method, payload):
        url = API_BASE.format(token=self.token, method=method)
        try:
            resp = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            return False, str(exc)
        try:
            body = resp.json()
        except ValueError:
            return False, f"HTTP {resp.status_code}"
        if body.get("ok"):
            return True, body
        return False, body.get("description", f"HTTP {resp.status_code}")

    def send_message(self, text):
        ok, info = self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": _fit(text, MESSAGE_LIMIT),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        if not ok:
            print(f"    sendMessage не спрацював: {info}")
        return ok

    def send_photo(self, photo_url, caption):
        ok, info = self._call("sendPhoto", {
            "chat_id": self.chat_id,
            "photo": photo_url,
            "caption": _fit(caption, CAPTION_LIMIT),
            "parse_mode": "HTML",
        })
        if not ok:
            print(f"    sendPhoto не спрацював: {info}")
        return ok

    def send_post(self, body, photo_url=None):
        """Спробувати з картинкою, а якщо не вийшло — текстом."""
        if photo_url and self.send_photo(photo_url, body):
            return True
        if photo_url:
            print("    надсилаю без картинки")
        return self.send_message(body)
