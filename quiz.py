"""Шкільні тести для родинного чату.

Питання лежать у папці questions/ — кожен предмет окремим файлом.
Бот бере звідти питання і надсилає у Telegram як вікторину: людина
тисне варіант, і Telegram сам одразу показує, вгадала вона чи ні,
та рахує, хто як відповів.

Нічого чекати не треба: бот надіслав питання і пішов спати далі.
"""

import hashlib
import os
import random

import yaml

QUESTIONS_DIR = "questions"

# Обмеження самого Telegram на вікторину.
QUESTION_LIMIT = 300
OPTION_LIMIT = 100
EXPLANATION_LIMIT = 200
MIN_OPTIONS = 2
MAX_OPTIONS = 10

# Рівні складності: як вони називаються в підписі до питання.
LEVELS = {
    1: "1–4 клас",
    2: "5–7 клас",
    3: "8–11 клас",
}

# Скільки питань пам'ятати, щоб не питати те саме двічі поспіль.
MEMORY = 400


# ----------------------------------------------------------------- налаштування

def quiz_config(config):
    """Розділ quiz з config.yaml із розумними значеннями за замовчуванням."""
    raw = (config or {}).get("quiz") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "per_day": int(raw.get("per_day", 0) or 0),
        "send_times": [str(t) for t in raw.get("send_times", []) or []],
        "levels": [int(v) for v in raw.get("levels", []) or [] if str(v).isdigit()],
        "subjects": raw.get("subjects", []) or [],
        "show_who_answers": bool(raw.get("show_who_answers", True)),
        "time_limit_seconds": int(raw.get("time_limit_seconds", 0) or 0),
    }


def _subject_switches(settings_quiz):
    """Назва предмета -> увімкнений чи ні (як записано в config.yaml)."""
    switches = {}
    for item in settings_quiz.get("subjects", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        if name:
            switches[name] = bool(item.get("enabled", True))
    return switches


# ----------------------------------------------------------------- читання питань

def question_id(text):
    """Незмінний номер питання — щоб пам'ятати, що його вже питали.

    Рахується з самого тексту, тому переставляння питань у файлі
    чи додавання нових пам'ять не збиває.
    """
    digest = hashlib.sha1(str(text).strip().lower().encode("utf-8"))
    return digest.hexdigest()[:10]


def check_question(item):
    """Що не так із питанням. Порожній список — усе гаразд."""
    problems = []
    if not isinstance(item, dict):
        return ["питання записане не як список полів"]

    text = str(item.get("q", "") or "").strip()
    if not text:
        problems.append("немає тексту питання (q)")
    elif len(text) > QUESTION_LIMIT:
        problems.append(f"питання довше за {QUESTION_LIMIT} символів")

    options = item.get("options")
    if not isinstance(options, list):
        problems.append("немає списку варіантів (options)")
        options = []
    else:
        if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            problems.append(f"варіантів має бути від {MIN_OPTIONS} до {MAX_OPTIONS}")
        for option in options:
            if not str(option or "").strip():
                problems.append("порожній варіант відповіді")
            elif len(str(option)) > OPTION_LIMIT:
                problems.append(f"варіант довший за {OPTION_LIMIT} символів")
        cleaned = [str(o).strip().lower() for o in options]
        if len(set(cleaned)) != len(cleaned):
            problems.append("два однакові варіанти відповіді")

    try:
        answer = int(item.get("answer", 0))
    except (TypeError, ValueError):
        answer = 0
    if not 1 <= answer <= len(options or []):
        problems.append("номер правильної відповіді (answer) поза списком варіантів")

    try:
        level = int(item.get("level", 1))
    except (TypeError, ValueError):
        level = 0
    if level not in LEVELS:
        problems.append(f"рівень (level) має бути одним із {sorted(LEVELS)}")

    why = str(item.get("why", "") or "")
    if len(why) > EXPLANATION_LIMIT:
        problems.append(f"пояснення (why) довше за {EXPLANATION_LIMIT} символів")

    return problems


def read_subject_file(path):
    """Прочитати один файл предмета. Повертає (назва, емодзі, список питань)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    name = str(data.get("name") or os.path.basename(path)).strip()
    emoji = str(data.get("emoji") or "🎓").strip()
    return name, emoji, data.get("questions") or []


def load_bank(config, directory=QUESTIONS_DIR, quiet=False):
    """Зібрати всі питання ввімкнених предметів. Повертає (питання, скарги)."""
    settings_quiz = quiz_config(config)
    switches = _subject_switches(settings_quiz)
    levels = set(settings_quiz["levels"]) or set(LEVELS)

    bank, problems = [], []
    if not os.path.isdir(directory):
        return [], [f"немає папки {directory} з питаннями"]

    for filename in sorted(os.listdir(directory)):
        if not filename.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(directory, filename)
        try:
            name, emoji, items = read_subject_file(path)
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"{filename}: файл не читається ({exc})")
            continue

        switch = switches.get(name.strip().lower())
        if switch is False:
            if not quiet:
                print(f"  {name}: вимкнено в config.yaml")
            continue
        if switch is None and switches and not quiet:
            print(f"  {name}: немає в списку subjects у config.yaml, вважаю ввімкненим")

        taken = 0
        for number, item in enumerate(items, 1):
            faults = check_question(item)
            if faults:
                problems.append(f"{filename}, питання {number}: " + "; ".join(faults))
                continue
            level = int(item.get("level", 1))
            if level not in levels:
                continue
            bank.append({
                "id": question_id(item["q"]),
                "subject": name,
                "emoji": emoji,
                "level": level,
                "question": str(item["q"]).strip(),
                "options": [str(o).strip() for o in item["options"]],
                "answer_index": int(item["answer"]) - 1,
                "why": str(item.get("why", "") or "").strip(),
            })
            taken += 1
        if not quiet:
            print(f"  {emoji} {name}: питань напоготові — {taken}")

    return bank, problems


# ----------------------------------------------------------------- вибір питань

def pick_questions(bank, state, count, rng=None):
    """Взяти потрібну кількість питань, не повторюючись і чергуючи предмети."""
    rng = rng or random.Random()
    count = max(0, int(count))
    if not bank or not count:
        return []

    asked = set(state.get("quiz_asked", []) or [])
    pool = [q for q in bank if q["id"] not in asked]
    if len(pool) < count:
        print("  усі питання вже були — починаю коло спочатку")
        state["quiz_asked"] = []
        pool = list(bank)

    by_subject = {}
    for question in pool:
        by_subject.setdefault(question["subject"], []).append(question)

    order = sorted(by_subject)
    last = state.get("quiz_last_subject", "")
    if last in order:
        cut = order.index(last) + 1
        order = order[cut:] + order[:cut]

    chosen = []
    while len(chosen) < count and any(by_subject.values()):
        for subject in order:
            if len(chosen) >= count:
                break
            items = by_subject.get(subject) or []
            if not items:
                continue
            question = rng.choice(items)
            items.remove(question)
            chosen.append(question)
    return chosen


def remember(state, questions):
    """Запам'ятати, що ці питання вже були."""
    asked = list(state.get("quiz_asked", []) or [])
    asked.extend(q["id"] for q in questions)
    state["quiz_asked"] = asked[-MEMORY:]
    if questions:
        state["quiz_last_subject"] = questions[-1]["subject"]
    return state


# ----------------------------------------------------------------- вигляд питання

def shuffle_options(question, rng=None):
    """Перемішати варіанти відповіді.

    У файлах правильну відповідь часто пишуть першою — так їх зручніше
    складати. Щоб у чаті не вгадували «тисну перший варіант», бот щоразу
    перемішує варіанти сам.
    """
    rng = rng or random.Random()
    correct = question["options"][question["answer_index"]]
    options = list(question["options"])
    rng.shuffle(options)
    mixed = dict(question)
    mixed["options"] = options
    mixed["answer_index"] = options.index(correct)
    return mixed


def poll_question(question):
    """Текст вікторини: підпис із предметом і класом, далі саме питання."""
    head = f"{question['emoji']} {question['subject']} · {LEVELS.get(question['level'], '')}"
    text = f"{head.strip(' ·')}\n\n{question['question']}"
    return text[:QUESTION_LIMIT]


def explanation(question):
    """Підказка, яку Telegram показує після відповіді."""
    return question["why"][:EXPLANATION_LIMIT]


def show(index, question):
    """Показати питання в логах — для перевірки, без надсилання."""
    print("\n" + "─" * 60)
    print(poll_question(question))
    for number, option in enumerate(question["options"], 1):
        mark = "✅" if number - 1 == question["answer_index"] else "  "
        print(f"  {mark} {number}. {option}")
    if question["why"]:
        print(f"  💡 {question['why']}")
