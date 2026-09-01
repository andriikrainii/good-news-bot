"""Спілкування з групою: меню, кнопки, голосування.

Бот не живе постійно — GitHub вмикає його час від часу. При кожному
вмиканні він забирає в Telegram усе, що назбиралося (команди й натискання
кнопок), відповідає на це і знову засинає.
"""

from datetime import datetime, timedelta, timezone

import settings
import sources

# Скільки днів пам'ятати голоси за конкретний пост.
POST_MEMORY_DAYS = 30

COMMANDS = [
    ("menu", "Налаштування бота"),
    ("temy", "Теми новин"),
    ("chastota", "Скільки новин на день"),
    ("stat", "Що подобається групі"),
]


# ----------------------------------------------------------------- пам'ять

def ensure_state(state):
    state.setdefault("update_offset", 0)
    state.setdefault("posts", {})
    state.setdefault("votes", {"topics": {}, "sources": {}})
    state["votes"].setdefault("topics", {})
    state["votes"].setdefault("sources", {})
    return state


def prune_posts(state):
    cutoff = datetime.now(timezone.utc) - timedelta(days=POST_MEMORY_DAYS)
    kept = {}
    for key, post in (state.get("posts") or {}).items():
        try:
            when = datetime.fromisoformat(post.get("at", ""))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            kept[key] = post
    state["posts"] = kept
    return state


def score(votes, name):
    entry = (votes or {}).get(name) or {}
    return int(entry.get("up", 0)) - int(entry.get("down", 0))


def topics_of(item, config):
    """Яким увімкненим темам відповідає новина."""
    haystack = f"{item.get('title', '')} {item.get('summary', '')}"
    matched = []
    for topic in config.get("topics", []) or []:
        if not topic.get("enabled"):
            continue
        regexes = sources.compile_keywords(topic.get("keywords", []))
        if sources.matches_any(haystack, regexes):
            matched.append(topic.get("name"))
    return matched


# ----------------------------------------------------------------- кнопки

def vote_keyboard(post):
    up = len(post.get("up", []))
    down = len(post.get("down", []))
    return [[
        {"text": f"👍 {up}" if up else "👍", "callback_data": "v:u"},
        {"text": f"👎 {down}" if down else "👎", "callback_data": "v:d"},
    ]]


def main_menu(config):
    times = ", ".join(str(t) for t in config.get("send_times", []) or [])
    enabled = [t.get("name") for t in config.get("topics", []) or [] if t.get("enabled")]
    text = (
        "<b>⚙️ Налаштування бота</b>\n\n"
        f"Зараз: <b>{config.get('posts_per_day', 0)}</b> новин на добу, "
        f"о {times}\n"
        f"Увімкнені теми: {', '.join(enabled) if enabled else '— жодної —'}\n\n"
        "Змінювати може будь-хто з групи."
    )
    keyboard = [
        [{"text": "📋 Теми новин", "callback_data": "m:topics"}],
        [{"text": "⏰ Скільки і коли", "callback_data": "m:freq"}],
        [{"text": "📊 Що подобається групі", "callback_data": "m:stats"}],
    ]
    return text, keyboard


def topics_menu(config, state):
    votes = (state.get("votes") or {}).get("topics", {})
    text = (
        "<b>📋 Теми новин</b>\n\n"
        "Натисніть на тему, щоб увімкнути або вимкнути її.\n"
        "✅ — новини на цю тему приходять, ❌ — ні."
    )
    keyboard = []
    for index, topic in enumerate(config.get("topics", []) or []):
        name = topic.get("name", "?")
        mark = "✅" if topic.get("enabled") else "❌"
        net = score(votes, name)
        tail = f"  ({net:+d})" if net else ""
        keyboard.append([{
            "text": f"{mark} {name}{tail}",
            "callback_data": f"t:{index}",
        }])
    keyboard.append([{"text": "⬅️ Назад", "callback_data": "m:main"}])
    return text, keyboard


def freq_menu(config):
    times = ", ".join(str(t) for t in config.get("send_times", []) or [])
    count = int(config.get("posts_per_day", 0) or 0)
    text = (
        "<b>⏰ Скільки і коли</b>\n\n"
        f"Зараз: <b>{count}</b> новин на добу, о {times}\n\n"
        "Новини діляться між часами розсилки якомога рівніше."
    )
    row = [{"text": ("• " if n == count else "") + str(n), "callback_data": f"n:{n}"}
           for n in settings.COUNT_CHOICES]
    schedule_row = []
    for key in sorted(settings.SCHEDULE_PRESETS):
        preset = settings.SCHEDULE_PRESETS[key]
        current = [str(t) for t in config.get("send_times", []) or []]
        mark = "• " if current == preset else ""
        word = "раз" if key == "1" else "рази"
        schedule_row.append({"text": f"{mark}{key} {word}", "callback_data": f"s:{key}"})
    keyboard = [
        row[:4], row[4:],
        [{"text": "— коли надсилати —", "callback_data": "m:freq"}],
        schedule_row,
        [{"text": "⬅️ Назад", "callback_data": "m:main"}],
    ]
    return text, keyboard


def stats_menu(config, state):
    votes = (state.get("votes") or {}).get("topics", {})
    lines = ["<b>📊 Що подобається групі</b>", ""]
    rows = []
    for topic in config.get("topics", []) or []:
        name = topic.get("name", "?")
        entry = votes.get(name) or {}
        up, down = int(entry.get("up", 0)), int(entry.get("down", 0))
        if up or down:
            rows.append((score(votes, name), name, up, down))
    if rows:
        rows.sort(reverse=True)
        for net, name, up, down in rows:
            lines.append(f"{name}: 👍 {up}  👎 {down}")
        lines.append("")
        lines.append("Теми, які подобаються більше, бот показує частіше.")
    else:
        lines.append("Поки ніхто не голосував.")
        lines.append("Тисніть 👍 або 👎 під новинами — і бот підлаштується.")
    keyboard = [[{"text": "⬅️ Назад", "callback_data": "m:main"}]]
    return "\n".join(lines), keyboard


def menu_for(kind, config, state):
    if kind == "topics":
        return topics_menu(config, state)
    if kind == "freq":
        return freq_menu(config)
    if kind == "stats":
        return stats_menu(config, state)
    return main_menu(config)


# ----------------------------------------------------------------- голоси

def recompute_votes(state):
    """Перерахувати підсумки голосів із самих постів.

    Рахуємо щоразу заново, а не додаємо до лічильників — так підсумок
    ніколи не розійдеться з дійсністю. Голоси за пости, старші за
    POST_MEMORY_DAYS, природно зникають: свіжі вподобання важливіші.
    """
    topics, sources_votes = {}, {}
    for post in (state.get("posts") or {}).values():
        up, down = len(post.get("up", [])), len(post.get("down", []))
        if not (up or down):
            continue
        for name in post.get("topics", []) or []:
            entry = topics.setdefault(name, {"up": 0, "down": 0})
            entry["up"] += up
            entry["down"] += down
        name = post.get("source")
        if name:
            entry = sources_votes.setdefault(name, {"up": 0, "down": 0})
            entry["up"] += up
            entry["down"] += down
    state["votes"] = {"topics": topics, "sources": sources_votes}
    return state


def preference_score(item, config, state):
    """Наскільки групі зайшла б така новина, судячи з голосів."""
    votes = state.get("votes") or {}
    total = sum(score(votes.get("topics", {}), name)
                for name in topics_of(item, config))
    return total + score(votes.get("sources", {}), item.get("source", ""))


def rank_candidates(items, config, state):
    """Спершу цікавіше для групи, але не три пости підряд з одного сайту."""
    ordered = sorted(
        items,
        key=lambda i: (
            preference_score(i, config, state),
            i["published"] or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return sources.interleave_by_source(ordered)


# ----------------------------------------------------------------- слухання

def _command(text):
    """Витягти команду з повідомлення: '/temy@bot розмова' -> 'temy'."""
    word = (text or "").strip().split()[0] if (text or "").strip() else ""
    if not word.startswith("/"):
        return None
    return word[1:].split("@")[0].lower()


COMMAND_MENUS = {
    "menu": "main", "start": "main", "help": "main",
    "temy": "topics", "topics": "topics",
    "chastota": "freq", "freq": "freq",
    "stat": "stats", "stats": "stats",
}


def handle_updates(sender, config, state, chat_id):
    """Розібрати все, що назбиралося. Повертає (config, чи_змінено_налаштування)."""
    updates = sender.get_updates(offset=state.get("update_offset", 0))
    if not updates:
        return config, False
    print(f"Нових подій із групи: {len(updates)}")

    config_changed = False
    menus_to_refresh = {}   # номер повідомлення -> який екран показати
    posts_to_refresh = set()
    chat_id = str(chat_id)

    for update in updates:
        state["update_offset"] = int(update.get("update_id", 0)) + 1

        message = update.get("message")
        if message:
            if str((message.get("chat") or {}).get("id")) != chat_id:
                continue
            kind = COMMAND_MENUS.get(_command(message.get("text")))
            if kind:
                text, keyboard = menu_for(kind, config, state)
                sender.send_message(text, keyboard)
                print(f"  показано меню: {kind}")
            continue

        callback = update.get("callback_query")
        if not callback:
            continue

        data = callback.get("data") or ""
        source_message = callback.get("message") or {}
        message_id = source_message.get("message_id")
        if str((source_message.get("chat") or {}).get("id")) != chat_id:
            sender.answer_callback(callback.get("id"), "Цей бот працює лише у своїй групі.")
            continue

        user = callback.get("from") or {}
        user_id = str(user.get("id"))
        who = user.get("first_name") or "Хтось"
        note = ""

        if data.startswith("v:"):
            post = (state.get("posts") or {}).get(str(message_id))
            if not post:
                note = "Ця новина вже застара для голосування."
            else:
                up = post.setdefault("up", [])
                down = post.setdefault("down", [])
                liked = data == "v:u"
                mine, other = (up, down) if liked else (down, up)
                if user_id in mine:
                    mine.remove(user_id)
                    note = "Голос скасовано."
                else:
                    mine.append(user_id)
                    if user_id in other:
                        other.remove(user_id)
                    note = "Дякую! Врахую 👍" if liked else "Зрозумів, таких менше 👎"
                posts_to_refresh.add(str(message_id))

        elif data.startswith("m:"):
            menus_to_refresh[message_id] = data[2:]

        elif data.startswith("t:"):
            try:
                index = int(data[2:])
            except ValueError:
                continue
            topics = config.get("topics", []) or []
            if 0 <= index < len(topics):
                new_value = not topics[index].get("enabled")
                ok, name = settings.set_topic_enabled(index, new_value)
                if ok:
                    config = settings.read_config()
                    config_changed = True
                    note = f"{name}: {'увімкнено' if new_value else 'вимкнено'}"
                    print(f"  {who} {note}")
                else:
                    note = "Не вдалося змінити, спробуйте ще раз."
            menus_to_refresh[message_id] = "topics"

        elif data.startswith("n:"):
            try:
                count = int(data[2:])
            except ValueError:
                continue
            if settings.set_posts_per_day(count):
                config = settings.read_config()
                config_changed = True
                note = f"Тепер {count} новин на добу"
                print(f"  {who}: {note}")
            menus_to_refresh[message_id] = "freq"

        elif data.startswith("s:"):
            preset = settings.SCHEDULE_PRESETS.get(data[2:])
            if preset and settings.set_send_times(preset):
                config = settings.read_config()
                config_changed = True
                note = "Новий розклад: " + ", ".join(preset)
                print(f"  {who}: {note}")
            menus_to_refresh[message_id] = "freq"

        sender.answer_callback(callback.get("id"), note)

    if posts_to_refresh:
        recompute_votes(state)
        for message_id in posts_to_refresh:
            post = state["posts"][message_id]
            sender.edit_keyboard(int(message_id), vote_keyboard(post))
        print(f"  оновлено голосів під постами: {len(posts_to_refresh)}")

    for message_id, kind in menus_to_refresh.items():
        text, keyboard = menu_for(kind, config, state)
        sender.edit_text(message_id, text, keyboard)

    return config, config_changed
