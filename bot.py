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
    return state


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
    return state


# ----------------------------------------------------------------- розклад

def parse_send_times(config):
    """Список часів розсилки як (година, хвилина), відсортований."""
    parsed = []
    for value in config.get("send_times", []) or []:
        text = str(value).strip()
        try:
            hour, minute = text.split(":")
            parsed.append((int(hour), int(minute)))
        except ValueError:
            print(f"УВАГА: час '{text}' у send_times записаний неправильно, пропускаю.")
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


# ----------------------------------------------------------------- новини

def collect_candidates(config, state):
    print("Читаю джерела…")
    entries = sources.fetch_entries(config)
    print(f"Усього свіжих записів: {len(entries)}")
    kept = sources.filter_entries(entries, config, set(state.get("sent", {})))
    print(f"Після фільтрів (стоп-слова, теми, повтори): {len(kept)}")
    return sources.interleave_by_source(kept)[:MAX_CANDIDATES]


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

def run(args):
    config = load_config(args.config)
    tz = get_timezone(config)
    now = datetime.now(tz)
    state = prune_state(load_state())

    print(f"Київський час зараз: {now:%Y-%m-%d %H:%M}")

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

    sent_count = 0
    for post in posts:
        if sender.send_post(post["body"], post["image"]):
            state["sent"][post["link"]] = datetime.now(timezone.utc).isoformat()
            sent_count += 1
            print(f"    надіслано: {post['title'][:70]}")
        else:
            print(f"    НЕ надіслано: {post['title'][:70]}")

    if slot_key and sent_count:
        state["last_slot"] = slot_key
    save_state(state)
    print(f"\nНадіслано новин: {sent_count} з {len(posts)}")
    return 0 if sent_count else 1


def main():
    parser = argparse.ArgumentParser(description="Бот добрих новин")
    parser.add_argument("--dry-run", action="store_true",
                        help="показати пости, нічого не надсилаючи")
    parser.add_argument("--test", action="store_true",
                        help="надіслати одну новину прямо зараз")
    parser.add_argument("--count", type=int, default=None,
                        help="скільки новин узяти для --dry-run або --test")
    parser.add_argument("--config", default=CONFIG_FILE, help="шлях до config.yaml")
    args = parser.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
