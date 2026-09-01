"""Зв'язок із миттєвою частиною бота, що живе на Netlify.

Netlify відповідає на натискання кнопок одразу і тримає голоси.
GitHub раз на запуск обмінюється з ним: віддає знімок налаштувань
і нові пости, забирає голоси й зміни, які зробили в чаті.

Якщо Netlify не налаштований або не відповідає — бот працює як раніше,
просто без миттєвих кнопок. Новини від цього не страждають.
"""

import os

import requests

REQUEST_TIMEOUT = 20


def _clean(url):
    return (url or "").strip().rstrip("/")


class Hub:
    def __init__(self, url=None, secret=None):
        self.url = _clean(url if url is not None else os.environ.get("NETLIFY_URL", ""))
        self.secret = (secret if secret is not None
                       else os.environ.get("SYNC_SECRET", "")).strip()

    @property
    def configured(self):
        return bool(self.url and self.secret)

    @property
    def webhook_url(self):
        return f"{self.url}/telegram"

    def sync(self, config=None, new_posts=None, ack_pending=0):
        """Обмінятися станом. Повертає відповідь Netlify або None."""
        if not self.configured:
            return None
        payload = {}
        if config is not None:
            payload["config"] = config
        if new_posts:
            payload["new_posts"] = new_posts
        if ack_pending:
            payload["ack_pending"] = ack_pending
        try:
            resp = requests.post(
                f"{self.url}/sync",
                json=payload,
                headers={"Authorization": f"Bearer {self.secret}"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"  Netlify не відповів ({exc}). Працюю без миттєвих кнопок.")
            return None
        if resp.status_code != 200:
            print(f"  Netlify повернув {resp.status_code}. Працюю без миттєвих кнопок.")
            return None
        try:
            return resp.json()
        except ValueError:
            print("  Netlify відповів незрозуміло. Працюю без миттєвих кнопок.")
            return None


def config_snapshot(config):
    """Те, що потрібно Netlify, щоб намалювати меню."""
    return {
        "posts_per_day": int(config.get("posts_per_day", 0) or 0),
        "send_times": [str(t) for t in config.get("send_times", []) or []],
        "topics": [
            {"name": topic.get("name", "?"), "enabled": bool(topic.get("enabled"))}
            for topic in config.get("topics", []) or []
        ],
    }
