"""Надсилання постів у Telegram."""

import html
import json

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


# Щоб дуже довгий заголовок не з'їв увесь ліміт підпису.
TITLE_LIMIT = 200


def build_post(title, text, link, source_name, footer):
    """Зібрати HTML-текст поста."""
    title = _trim_to_words(title or "", TITLE_LIMIT)
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

    @staticmethod
    def _message_id(info):
        try:
            return info["result"]["message_id"]
        except (TypeError, KeyError):
            return None

    def call_raw(self, method, payload):
        """Виклик будь-якого методу Telegram — для налаштування."""
        return self._call(method, payload)

    def send_message(self, text, keyboard=None, chat_id=None):
        """Надіслати текст. Повертає номер повідомлення або None."""
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": _fit(text, MESSAGE_LIMIT),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        ok, info = self._call("sendMessage", payload)
        if not ok:
            print(f"    sendMessage не спрацював: {info}")
            return None
        return self._message_id(info)

    def send_photo(self, photo_url, caption, keyboard=None):
        """Надіслати картинку з підписом. Повертає номер повідомлення або None."""
        payload = {
            "chat_id": self.chat_id,
            "photo": photo_url,
            "caption": _fit(caption, CAPTION_LIMIT),
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        ok, info = self._call("sendPhoto", payload)
        if not ok:
            print(f"    sendPhoto не спрацював: {info}")
            return None
        return self._message_id(info)

    def send_post(self, body, photo_url=None, keyboard=None):
        """Спробувати з картинкою, а якщо не вийшло — текстом."""
        if photo_url:
            message_id = self.send_photo(photo_url, body, keyboard)
            if message_id:
                return message_id
            print("    надсилаю без картинки")
        return self.send_message(body, keyboard)

    # ---------- слухання: команди й натискання кнопок ----------

    def get_updates(self, offset=0, timeout=0):
        """Забрати все, що назбиралося з минулого разу."""
        payload = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset:
            payload["offset"] = offset
        ok, info = self._call("getUpdates", payload)
        if not ok:
            print(f"    getUpdates не спрацював: {info}")
            return []
        return info.get("result", [])

    def answer_callback(self, callback_id, text=""):
        """Прибрати «годинник» на кнопці у того, хто натиснув."""
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        # Якщо натискання давнє, Telegram скаже «query is too old» — це не біда.
        self._call("answerCallbackQuery", payload)

    def edit_keyboard(self, message_id, keyboard, chat_id=None):
        """Оновити кнопки під уже надісланим повідомленням."""
        ok, info = self._call("editMessageReplyMarkup", {
            "chat_id": chat_id or self.chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": keyboard}),
        })
        if not ok and "not modified" not in str(info):
            print(f"    editMessageReplyMarkup: {info}")
        return ok

    def edit_text(self, message_id, text, keyboard=None, chat_id=None):
        """Переписати текст меню й кнопки під ним."""
        payload = {
            "chat_id": chat_id or self.chat_id,
            "message_id": message_id,
            "text": _fit(text, MESSAGE_LIMIT),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        ok, info = self._call("editMessageText", payload)
        if not ok and "not modified" not in str(info):
            print(f"    editMessageText: {info}")
        return ok

    def set_commands(self, commands):
        """Показати список команд у меню Telegram (кнопка «/» біля поля вводу)."""
        self._call("setMyCommands", {
            "commands": json.dumps(
                [{"command": c, "description": d} for c, d in commands]
            ),
            "scope": json.dumps({"type": "all_group_chats"}),
        })
