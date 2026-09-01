"""Читання RSS-стрічок, фільтрація новин і пошук картинок."""

import html
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# Браузерний User-Agent: без нього частина сайтів віддає 403.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20

# Службові хвости, які RSS-стрічки додають до описів.
JUNK_PATTERNS = [
    re.compile(r"The post\s+.*?\s+appeared first on\s+.*?$", re.I | re.S),
    re.compile(r"\bContinue reading\b.*$", re.I | re.S),
    re.compile(r"\bRead more\s+(at|on|here)\b.*$", re.I | re.S),
    re.compile(r"\bRead the full story\b.*$", re.I | re.S),
    re.compile(r"\bEDITORIAL TEAM\b.*$", re.I | re.S),
    re.compile(r"\bThis article first appeared\b.*$", re.I | re.S),
    re.compile(r"\bThe post\b\s*$", re.I),
    re.compile(r"\[\s*(…|\.\.\.)\s*\]\s*$"),
    re.compile(r"\(\s*Image credit.*?\)", re.I | re.S),
]

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)

BAD_IMAGE_HINTS = (
    "gravatar", "avatar", "logo", "icon", "spacer", "pixel",
    "feedburner", "doubleclick", "1x1", "blank.gif",
)


def strip_html(raw):
    """Прибрати HTML-теги і зайві пробіли."""
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return SPACE_RE.sub(" ", text).strip()


def clean_summary(raw):
    """Очистити опис новини від службових хвостів."""
    text = strip_html(raw)
    for pattern in JUNK_PATTERNS:
        text = pattern.sub("", text)
    text = SPACE_RE.sub(" ", text).strip()
    return text.strip(" -–—|")


def _entry_datetime(entry):
    """Дата публікації запису в UTC (або None)."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _looks_like_image(url):
    if not url or not url.startswith("http"):
        return False
    low = url.lower()
    return not any(hint in low for hint in BAD_IMAGE_HINTS)


def image_from_entry(entry):
    """Знайти картинку в самому RSS-записі."""
    for media in entry.get("media_content", []) or []:
        url = media.get("url")
        if _looks_like_image(url):
            return url

    for thumb in entry.get("media_thumbnail", []) or []:
        url = thumb.get("url")
        if _looks_like_image(url):
            return url

    for enc in entry.get("enclosures", []) or []:
        if "image" in (enc.get("type") or "") and _looks_like_image(enc.get("href")):
            return enc.get("href")
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and "image" in (link.get("type") or ""):
            if _looks_like_image(link.get("href")):
                return link.get("href")

    blocks = [entry.get("summary", "")]
    for content in entry.get("content", []) or []:
        blocks.append(content.get("value", ""))
    for block in blocks:
        for match in IMG_SRC_RE.findall(block or ""):
            url = html.unescape(match)
            if _looks_like_image(url):
                return url
    return None


def image_from_page(url):
    """Запасний варіант: дістати og:image зі сторінки статті."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"    не вдалося відкрити сторінку по картинку: {exc}")
        return None

    head = resp.text[:400000]
    for prop in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        pattern = re.compile(
            r"<meta[^>]+(?:property|name)=[\"']" + re.escape(prop) +
            r"[\"'][^>]*content=[\"']([^\"']+)[\"']", re.I)
        match = pattern.search(head)
        if not match:
            pattern = re.compile(
                r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"']" +
                re.escape(prop) + r"[\"']", re.I)
            match = pattern.search(head)
        if match:
            found = html.unescape(match.group(1)).strip()
            if _looks_like_image(found):
                return found
    return None


def find_image(entry, link):
    """Картинка з RSS, а якщо немає — зі сторінки статті."""
    return image_from_entry(entry) or (image_from_page(link) if link else None)


def _keyword_regex(word):
    word = word.strip().lower()
    if not word:
        return None
    return re.compile(r"(?<![a-z0-9])" + re.escape(word).replace(r"\ ", r"[\s\-]+") +
                      r"(?![a-z0-9])", re.I)


def compile_keywords(words):
    """Перетворити список слів на регулярки зі збігом по цілому слову."""
    out = []
    for word in words or []:
        rx = _keyword_regex(str(word))
        if rx is not None:
            out.append(rx)
    return out


def matches_any(text, regexes):
    return any(rx.search(text) for rx in regexes)


def fetch_entries(config):
    """Зібрати свіжі записи з усіх джерел."""
    max_age = int(config.get("max_age_days", 5))
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
    collected = []

    for source in config.get("sources", []) or []:
        name = source.get("name") or source.get("url", "")
        url = source.get("url")
        if not url:
            continue
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/rss+xml,*/*"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except requests.RequestException as exc:
            print(f"  [{name}] джерело недоступне: {exc}")
            continue

        taken = 0
        for entry in parsed.entries:
            link = (entry.get("link") or "").strip()
            title = strip_html(entry.get("title", ""))
            if not link or not title:
                continue
            published = _entry_datetime(entry)
            if published is not None and published < cutoff:
                continue
            collected.append({
                "source": name,
                "title": title,
                "summary": clean_summary(
                    entry.get("summary")
                    or (entry.get("content") or [{}])[0].get("value", "")
                ),
                "link": link,
                "published": published,
                "entry": entry,
            })
            taken += 1
        print(f"  [{name}] узято свіжих записів: {taken}")

    return collected


def filter_entries(entries, config, sent_links):
    """Відкинути вже надіслані, стоп-слова і те, що не підходить темам."""
    blocked = compile_keywords(config.get("blocked_keywords", []))

    topic_regexes = []
    for topic in config.get("topics", []) or []:
        if topic.get("enabled"):
            topic_regexes.extend(compile_keywords(topic.get("keywords", [])))
    if not topic_regexes:
        print("  усі теми вимкнені в config.yaml — новин не буде")
        return []

    kept = []
    seen_links = set()
    for item in entries:
        link = item["link"]
        if link in sent_links or link in seen_links:
            continue
        haystack = f"{item['title']} {item['summary']}"
        if matches_any(haystack, blocked):
            continue
        if not matches_any(haystack, topic_regexes):
            continue
        seen_links.add(link)
        kept.append(item)
    return kept


def interleave_by_source(items):
    """Перемішати по колу, щоб пости підряд були з різних сайтів."""
    buckets = {}
    order = []
    for item in items:
        key = item["source"]
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)

    for key in order:
        buckets[key].sort(
            key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    mixed = []
    while True:
        added = False
        for key in order:
            if buckets[key]:
                mixed.append(buckets[key].pop(0))
                added = True
        if not added:
            break
    return mixed
