"""Акуратне редагування config.yaml.

Бот міняє в config.yaml лише потрібне значення, рядок у рядок,
не переписуючи файл цілком. Так усі пояснення українською,
порядок тем і форматування залишаються на місці — файл і далі
можна читати й правити руками.
"""

import re

import yaml

CONFIG_FILE = "config.yaml"

# Скільки новин на добу можна вибрати кнопками.
COUNT_CHOICES = [1, 2, 3, 4, 5, 7, 10]

# Готові розклади: скільки разів на день і о котрій.
SCHEDULE_PRESETS = {
    "1": ["10:00"],
    "2": ["09:00", "19:00"],
    "3": ["09:00", "14:00", "20:00"],
    "4": ["08:00", "12:00", "16:00", "20:00"],
}


def read_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().split("\n")


def _write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _save_if_valid(path, lines, check):
    """Записати файл, тільки якщо він лишився правильним YAML.

    check(config) має повернути True, якщо зміна справді відбулася.
    Інакше файл повертається у попередній стан.
    """
    backup = _read_lines(path)
    _write_lines(path, lines)
    try:
        config = read_config(path)
    except yaml.YAMLError as exc:
        _write_lines(path, backup)
        print(f"    зміна скасована, файл став би зламаним: {exc}")
        return False
    if not check(config):
        _write_lines(path, backup)
        print("    зміна скасована: значення не застосувалося")
        return False
    return True


def _topic_name_lines(lines):
    """Номери рядків із назвами тем, у тому ж порядку, що й у файлі."""
    found = []
    inside = False
    for index, line in enumerate(lines):
        if re.match(r"^topics:\s*$", line):
            inside = True
            continue
        if inside:
            if re.match(r"^[A-Za-z_]", line):  # почався інший розділ
                break
            match = re.match(r'^\s*-\s*name:\s*"?(.+?)"?\s*$', line)
            if match:
                found.append((index, match.group(1)))
    return found


def topic_names(path=CONFIG_FILE):
    return [name for _, name in _topic_name_lines(_read_lines(path))]


def set_topic_enabled(index, enabled, path=CONFIG_FILE):
    """Увімкнути або вимкнути тему за її номером у списку."""
    lines = _read_lines(path)
    entries = _topic_name_lines(lines)
    if not 0 <= index < len(entries):
        return False, None

    start, name = entries[index]
    limit = entries[index + 1][0] if index + 1 < len(entries) else len(lines)
    value = "true" if enabled else "false"

    for pos in range(start, limit):
        match = re.match(r"^(\s*enabled:\s*)(true|false)\s*$", lines[pos], re.I)
        if match:
            lines[pos] = f"{match.group(1)}{value}"
            ok = _save_if_valid(
                path, lines,
                lambda cfg: bool(cfg["topics"][index].get("enabled")) is bool(enabled),
            )
            return ok, name
    return False, name


def set_posts_per_day(count, path=CONFIG_FILE):
    """Змінити кількість новин на добу."""
    count = int(count)
    if not 1 <= count <= 50:
        return False
    lines = _read_lines(path)
    for pos, line in enumerate(lines):
        if re.match(r"^posts_per_day:\s*\d+\s*$", line):
            lines[pos] = f"posts_per_day: {count}"
            return _save_if_valid(
                path, lines, lambda cfg: int(cfg.get("posts_per_day", 0)) == count
            )
    return False


def set_send_times(times, path=CONFIG_FILE):
    """Замінити список часів розсилки, не чіпаючи коментарі навколо."""
    times = list(times)
    if not times:
        return False
    for value in times:
        match = re.match(r"^(\d{2}):(\d{2})$", str(value))
        if not match:
            return False
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return False

    lines = _read_lines(path)
    start = None
    for pos, line in enumerate(lines):
        if re.match(r"^send_times:\s*$", line):
            start = pos
            break
    if start is None:
        return False

    end = start + 1
    while end < len(lines) and re.match(r'^\s*-\s*"?\d{2}:\d{2}"?\s*$', lines[end]):
        end += 1
    if end == start + 1:
        return False

    block = [f'  - "{value}"' for value in times]
    lines = lines[:start + 1] + block + lines[end:]
    return _save_if_valid(
        path, lines, lambda cfg: [str(t) for t in cfg.get("send_times", [])] == times
    )
