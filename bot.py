#!/usr/bin/env python3
"""Бот добрих новин: збирає новини, перекладає українською і шле в Telegram.

Запуск:
    python bot.py --dry-run --count 3   показати пости, нічого не надсилаючи
    python bot.py --test                надіслати одну новину зараз
    python bot.py                       звичайний запуск за розкладом
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

import hub as hub_module
import interact
import settings
import sources
import telegram_api
from translator import Translator

CONFIG_FILE = "config.yaml"
STATE_FILE = "state.json"

# Наскільки пізно ще можна відпрацювати слот (GitHub часто запускає із затримкою).
SLOT_GRACE_MINUTES = 100
# Скільки днів пам'ятати надіслані посилання.
LINK_MEMORY_DAYS = 90
# Скільки новин максимум перебрати, шукаючи потрібну кількість добрих.
MAX_CANDIDATES = 60


# ----------------------------------------------------------------- config

def load_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not config.get("sources"):
        raise SystemExit("У config.yaml немає жодного джерела (sources).")
    return config


def get_timezone(config):
    name = config.get("timezone") or "Europe/Kyiv"
    try:
        return ZoneInfo(name)
    except Exception:
        print(f"УВАГА: часовий пояс '{name}' не знайдено, беру UTC.")
        return timezone.utc


# ----------------------------------------------------------------- state

def load_state(path=STATE_FILE):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    state.setdefault("sent", {})
    state.setdefault("last_slot", "")
    return interact.ensure_state(state)


def save_state(state, path=STATE_FILE):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def prune_state(state):
    """Забути посилання, старші за 90 днів."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LINK_MEMORY_DAYS)
    fresh = {}
    for link, stamp in (state.get("sent") or {}).items():
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            fresh[link] = stamp
    state["sent"] = fresh
    return interact.prune_posts(state)


# ----------------------------------------------------------------- розклад

def parse_send_times(config):
    """Список часів розсилки як (година, хвилина), відсортований."""
    parsed = []
    for value in config.get("send_times", []) or []:
        text = str(value).strip()
        try:
            hour, minute = (int(part) for part in text.split(":"))
        except ValueError:
            print(f"УВАГА: час '{text}' у send_times записаний неправильно, пропускаю.")
            continue
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            print(f"УВАГА: час '{text}' у send_times не існує, пропускаю.")
            continue
        parsed.append((hour, minute))
    parsed.sort()
    return parsed


def posts_for_slots(total, slot_count):
    """Поділити новини між слотами якомога рівніше: 5 на 3 -> [2, 2, 1]."""
    if slot_count <= 0:
        return []
    total = max(0, int(total))
    base, extra = divmod(total, slot_count)
    return [base + (1 if i < extra else 0) for i in range(slot_count)]


def current_slot(config, now):
    """Який слот зараз треба відпрацювати: (ключ, скільки новин) або (None, 0)."""
    times = parse_send_times(config)
    if not times:
        print("У config.yaml не задано жодного часу розсилки (send_times).")
        return None, 0

    counts = posts_for_slots(config.get("posts_per_day", 0), len(times))
    best = None

    for day_shift in (0, -1):  # -1 щоб не загубити слот, який був перед північчю
        day = (now + timedelta(days=day_shift)).date()
        for index, (hour, minute) in enumerate(times):
            slot_at = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo)
            slot_at = slot_at.replace(hour=hour, minute=minute)
            delay = (now - slot_at).total_seconds() / 60
            if 0 <= delay <= SLOT_GRACE_MINUTES:
                if best is None or slot_at > best[0]:
                    best = (slot_at, f"{slot_at:%Y-%m-%d %H:%M}", counts[index])

    if best is None:
        return None, 0
    return best[1], best[2]


def apply_from_chat(config, state, hub, sent_posts=None):
    """Обмінятися з Netlify: віддати знімок і пости, забрати голоси й зміни.

    Повертає оновлений config: у чаті могли перемкнути тему чи змінити
    кількість новин, і це треба акуратно вписати в config.yaml.
    """
    if not hub.configured:
        return config

    answer = hub.sync(
        config=hub_module.config_snapshot(config),
        new_posts=sent_posts or None,
        ack_pending=int(state.get("applied_pending", 0)),
    )
    if answer is None:
        return config

    votes = answer.get("votes")
    if votes:
        state["votes"] = votes

    applied = int(state.get("applied_pending", 0))
    for change in sorted(answer.get("pending", []), key=lambda c: c.get("id", 0)):
        kind = change.get("kind")
        done = False
        if kind == "topic":
            done, name = settings.set_topic_enabled(
                int(change.get("index", -1)), bool(change.get("value"))
            )
            if done:
                print(f"  з чату: {name} "
                      f"{'увімкнено' if change.get('value') else 'вимкнено'}")
        elif kind == "count":
            done = settings.set_posts_per_day(int(change.get("value", 0)))
            if done:
                print(f"  з чату: тепер {change.get('value')} новин на добу")
        elif kind == "schedule":
            preset = settings.SCHEDULE_PRESETS.get(str(change.get("key")))
            done = bool(preset) and settings.set_send_times(preset)
            if done:
                print(f"  з чату: новий розклад {', '.join(preset)}")
        if done:
            config = settings.read_config()
        applied = max(applied, int(change.get("id", 0)))

    state["applied_pending"] = applied
    return config


# ----------------------------------------------------------------- новини

def collect_candidates(config, state):
    print("Читаю джерела…")
    entries = sources.fetch_entries(config)
    print(f"Усього свіжих записів: {len(entries)}")
    kept = sources.filter_entries(entries, config, set(state.get("sent", {})))
    print(f"Після фільтрів (стоп-слова, теми, повтори): {len(kept)}")
    ranked = interact.rank_candidates(kept, config, state)
    if any((state.get("votes") or {}).get("topics", {}).values()):
        print("Порядок підібрано з урахуванням голосів групи.")
    return ranked[:MAX_CANDIDATES]


def prepare_posts(candidates, translator_obj, config, wanted, with_images=True):
    """Перекласти й зібрати потрібну кількість готових постів."""
    footer = config.get("footer", "")
    ready = []
    for item in candidates:
        if len(ready) >= wanted:
            break
        print(f"  → {item['title'][:80]}")
        result = translator_obj.process(item)
        if not result.get("ok"):
            print(f"    пропускаю: {result.get('reason', 'не підходить')}")
            continue
        image = sources.find_image(item["entry"], item["link"]) if with_images else None
        body = telegram_api.build_post(
            result["title"], result["text"], item["link"], item["source"], footer
        )
        ready.append({
            "link": item["link"],
            "source": item["source"],
            "topics": interact.topics_of(item, config),
            "image": image,
            "body": body,
            "title": result["title"],
            "text": result["text"],
        })
        print(f"    готово {'(з картинкою)' if image else '(без картинки)'}")
    return ready


def show_post(index, post):
    print("\n" + "─" * 60)
    print(f"ПОСТ {index}   джерело: {post['source']}")
    print(f"картинка: {post['image'] or 'немає'}")
    print("─" * 60)
    print(f"{post['title']}\n\n{post['text']}\n\n🔗 {post['link']}")


# ----------------------------------------------------------------- запуск

def listen(args):
    """Забрати з Telegram команди й натискання кнопок і відповісти на них."""
    config = load_config(args.config)
    hub = hub_module.Hub()
    if hub.configured:
        print("Кнопки обслуговує Netlify — опитування Telegram не потрібне.")
        return 0
    state = prune_state(load_state())
    sender = telegram_api.TelegramSender(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    if not sender.configured:
        print("ПОМИЛКА: немає TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")
        return 1

    if not state.get("commands_registered"):
        sender.set_commands(interact.COMMANDS)
        state["commands_registered"] = True

    config, config_changed = interact.handle_updates(
        sender, config, state, sender.chat_id
    )
    save_state(state)
    if config_changed:
        print("Налаштування в config.yaml оновлено на прохання групи.")
    return 0


def setup_webhook(_args):
    """Сказати Telegram, щоб слав натискання кнопок на Netlify."""
    hub = hub_module.Hub()
    sender = telegram_api.TelegramSender(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    if not sender.configured:
        print("ПОМИЛКА: немає TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")
        return 1
    if not hub.configured:
        print("Netlify не налаштований (потрібні NETLIFY_URL і SYNC_SECRET).")
        print("Прибираю вебхук — бот повернеться до опитування раз на кілька хвилин.")
        ok, info = sender.call_raw("deleteWebhook", {"drop_pending_updates": "false"})
        print("готово" if ok else f"не вдалося: {info}")
        return 0 if ok else 1

    ok, info = sender.call_raw("setWebhook", {
        "url": hub.webhook_url,
        "secret_token": os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
        "allowed_updates": '["message","callback_query"]',
        "drop_pending_updates": "true",
    })
    if not ok:
        print(f"Не вдалося підключити вебхук: {info}")
        return 1
    print(f"Вебхук підключено: {hub.webhook_url}")

    sender.set_commands(interact.COMMANDS)
    ok, info = sender.call_raw("getWebhookInfo", {})
    if ok:
        result = info.get("result", {})
        print(f"  адреса: {result.get('url')}")
        print(f"  черга необроблених: {result.get('pending_update_count')}")
        if result.get("last_error_message"):
            print(f"  остання помилка: {result.get('last_error_message')}")

    config = load_config()
    state = prune_state(load_state())
    answer = hub.sync(config=hub_module.config_snapshot(config))
    if answer is None:
        print("УВАГА: Netlify не відповів на перевірку зв'язку.")
        return 1
    print(f"Netlify на зв'язку, постів у пам'яті: {answer.get('posts_count', 0)}")
    save_state(state)
    return 0


def run(args):
    config = load_config(args.config)
    tz = get_timezone(config)
    now = datetime.now(tz)
    state = prune_state(load_state())
    hub = hub_module.Hub()

    print(f"Київський час зараз: {now:%Y-%m-%d %H:%M}")
    if hub.configured:
        config = apply_from_chat(config, state, hub)

    if args.dry_run:
        wanted = args.count or 3
        slot_key = None
    elif args.test:
        wanted = args.count or 1
        slot_key = None
        print("Тестовий запуск: надішлю одну новину просто зараз.")
    else:
        slot_key, wanted = current_slot(config, now)
        if not slot_key:
            print("Зараз не час розсилки. Нічого не роблю.")
            return 0
        if state.get("last_slot") == slot_key:
            print(f"Слот {slot_key} уже відпрацьовано. Нічого не роблю.")
            return 0
        if wanted <= 0:
            print(f"На слот {slot_key} припадає 0 новин. Позначаю слот і виходжу.")
            state["last_slot"] = slot_key
            save_state(state)
            return 0
        print(f"Слот {slot_key}: треба надіслати новин — {wanted}")

    sender = telegram_api.TelegramSender(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    if not args.dry_run and not sender.configured:
        print("ПОМИЛКА: немає TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")
        print("Додайте їх у секрети репозиторію на GitHub.")
        return 1

    candidates = collect_candidates(config, state)
    if not candidates:
        print("Підхожих новин не знайшлося. Спробую наступного разу.")
        return 0

    translator_obj = Translator(config)
    print(f"Переклад: {translator_obj.provider}")
    print("Готую пости…")
    posts = prepare_posts(candidates, translator_obj, config, wanted)

    if not posts:
        print("Жодна новина не пройшла перевірку. Спробую наступного разу.")
        return 0

    if args.dry_run:
        for index, post in enumerate(posts, 1):
            show_post(index, post)
        print("\n" + "─" * 60)
        print(f"Це був пробний показ ({len(posts)} шт.), нічого не надіслано.")
        return 0

    run_started = datetime.now(timezone.utc).isoformat()
    sent_count = 0
    for post in posts:
        entry = {
            "link": post["link"],
            "source": post["source"],
            "topics": post["topics"],
            "title": post["title"],
            "up": [],
            "down": [],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        message_id = sender.send_post(
            post["body"], post["image"], interact.vote_keyboard(entry)
        )
        if message_id:
            state["sent"][post["link"]] = entry["at"]
            state["posts"][str(message_id)] = entry
            sent_count += 1
            print(f"    надіслано: {post['title'][:70]}")
        else:
            print(f"    НЕ надіслано: {post['title'][:70]}")

    if slot_key and sent_count:
        state["last_slot"] = slot_key

    if hub.configured and sent_count:
        fresh = {mid: post for mid, post in state["posts"].items()
                 if post.get("at", "") >= run_started}
        apply_from_chat(config, state, hub, sent_posts=fresh)

    save_state(state)
    print(f"\nНадіслано новин: {sent_count} з {len(posts)}")
    return 0 if sent_count else 1


def main():
    parser = argparse.ArgumentParser(description="Бот добрих новин")
    parser.add_argument("--dry-run", action="store_true",
                        help="показати пости, нічого не надсилаючи")
    parser.add_argument("--test", action="store_true",
                        help="надіслати одну новину прямо зараз")
    parser.add_argument("--listen", action="store_true",
                        help="відповісти на команди й кнопки з групи")
    parser.add_argument("--setup-webhook", action="store_true",
                        help="підключити миттєві кнопки через Netlify")
    parser.add_argument("--count", type=int, default=None,
                        help="скільки новин узяти для --dry-run або --test")
    parser.add_argument("--config", default=CONFIG_FILE, help="шлях до config.yaml")
    args = parser.parse_args()
    try:
        if args.setup_webhook:
            return setup_webhook(args)
        return listen(args) if args.listen else run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
