import os
import json
import sqlite3
import hashlib
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser
import requests
from bs4 import BeautifulSoup


# ============================================================
# SETTINGS
# ============================================================

NORMAL_MAX_AGE = timedelta(minutes=10)
PRIORITY_PODCAST_MAX_AGE = timedelta(hours=24)

DB_FILE = "news.db"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    )
}

PRIORITY_SOURCES = {
    "David Ornstein",
    "James Pearce",
    "Ian Doyle",
    "Alex Crook",
    "Fabrizio Romano",
    "Ben Jacobs",
}

BLOCKED_CONTENT = [
    "blood red liverpool podcast",
    "blood red podcast",
]


# ============================================================
# CONFIG
# ============================================================

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_items (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            source TEXT,
            published_at TEXT,
            created_at TEXT
        )
        """
    )

    conn.commit()
    return conn


def make_id(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def already_seen(conn, url):
    item_id = make_id(url)

    row = conn.execute(
        "SELECT 1 FROM seen_items WHERE id = ?",
        (item_id,),
    ).fetchone()

    return row is not None


def mark_seen(conn, item):
    item_id = make_id(item["url"])

    conn.execute(
        """
        INSERT OR IGNORE INTO seen_items
        (id, url, title, source, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            item["url"],
            item["title"],
            item["source"],
            item["published_at"].isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()


# ============================================================
# TIME PARSING
# ============================================================

def parse_datetime(value):
    """
    Attempts to parse many common date formats.
    Returns timezone-aware UTC datetime or None.
    """

    if not value:
        return None

    if isinstance(value, datetime):
        dt = value

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    value = str(value).strip()

    if not value:
        return None

    # ISO format
    try:
        iso_value = value.replace("Z", "+00:00")

        dt = datetime.fromisoformat(iso_value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        pass

    # RFC / RSS style dates
    try:
        dt = parsedate_to_datetime(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        pass

    return None


def parse_feed_entry_date(entry):
    """
    RSS/Atom date extraction.
    """

    candidates = [
        entry.get("published"),
        entry.get("updated"),
        entry.get("created"),
        entry.get("date"),
    ]

    for value in candidates:
        dt = parse_datetime(value)

        if dt:
            return dt

    # feedparser *_parsed fields
    for field in [
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ]:
        value = entry.get(field)

        if value:
            try:
                dt = datetime(
                    value.tm_year,
                    value.tm_mon,
                    value.tm_mday,
                    value.tm_hour,
                    value.tm_min,
                    value.tm_sec,
                    tzinfo=timezone.utc,
                )

                return dt

            except Exception:
                pass

    return None


# ============================================================
# WEB PAGE DATE EXTRACTION
# ============================================================

def extract_date_from_json_ld(soup):
    """
    Looks for datePublished in JSON-LD.
    """

    scripts = soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    )

    for script in scripts:
        raw = script.string or script.get_text(strip=True)

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        objects = []

        if isinstance(data, dict):
            objects.append(data)

            if "@graph" in data and isinstance(data["@graph"], list):
                objects.extend(data["@graph"])

        elif isinstance(data, list):
            objects.extend(data)

        for obj in objects:
            if not isinstance(obj, dict):
                continue

            for key in [
                "datePublished",
                "dateCreated",
            ]:
                dt = parse_datetime(obj.get(key))

                if dt:
                    return dt

    return None


def extract_date_from_meta(soup):
    """
    Looks for common publication-date meta tags.
    """

    meta_names = [
        "article:published_time",
        "datePublished",
        "datepublished",
        "publishdate",
        "publication_date",
        "published_time",
        "date",
    ]

    for meta in soup.find_all("meta"):
        key = (
            meta.get("property")
            or meta.get("name")
            or meta.get("itemprop")
            or ""
        ).lower()

        if key in meta_names:
            value = meta.get("content")

            dt = parse_datetime(value)

            if dt:
                return dt

    return None


def extract_date_from_time_tags(soup):
    """
    Looks for <time datetime="...">.
    """

    for tag in soup.find_all("time"):
        value = tag.get("datetime")

        if value:
            dt = parse_datetime(value)

            if dt:
                return dt

    return None


def extract_date_from_visible_text(soup):
    """
    Last-resort extraction for pages displaying dates such as:

    15 Sep 2026
    15 September 2026
    Sep 15, 2026
    """

    text = soup.get_text(" ", strip=True)

    patterns = [
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if not match:
            continue

        value = match.group(0)

        formats = [
            "%d %b %Y",
            "%d %B %Y",
            "%b %d, %Y",
            "%B %d, %Y",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(value, fmt)
                return dt.replace(tzinfo=timezone.utc)

            except Exception:
                pass

    return None


def extract_page_date(soup):
    """
    Uses the most reliable sources first.
    """

    dt = extract_date_from_json_ld(soup)

    if dt:
        return dt

    dt = extract_date_from_meta(soup)

    if dt:
        return dt

    dt = extract_date_from_time_tags(soup)

    if dt:
        return dt

    dt = extract_date_from_visible_text(soup)

    if dt:
        return dt

    return None


def get_article_date(url):
    """
    Opens the actual article and attempts to find its
    publication date.
    """

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=15,
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        return extract_page_date(soup)

    except Exception as exc:
        print(f"Could not read article date: {url}")
        print(f"Reason: {exc}")
        return None


# ============================================================
# CONTENT FILTERING
# ============================================================

def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        (text or "").strip().lower(),
    )


def is_blocked(title, summary=""):
    text = normalize_text(
        f"{title} {summary}"
    )

    for blocked in BLOCKED_CONTENT:
        if blocked in text:
            return True

    return False


def is_podcast(title, summary=""):
    text = normalize_text(
        f"{title} {summary}"
    )

    podcast_words = [
        "podcast",
        "podcasts",
        "listen",
        "audio",
        "episode",
        "ep.",
    ]

    return any(word in text for word in podcast_words)


def is_liverpool_article(
    title,
    summary="",
    source_type="",
):
    """
    Strict Liverpool relevance filter.
    """

    title_text = normalize_text(title)
    summary_text = normalize_text(summary)

    text = f"{title_text} {summary_text}"

    # Pages dedicated specifically to Liverpool
    if source_type in {
        "liverpool_page",
        "liverpool_official",
    }:
        return True

    strong_keywords = config.get(
        "strong_keywords",
        [],
    )

    for keyword in strong_keywords:
        if normalize_text(keyword) in text:
            return True

    # Liverpool references
    basic_keywords = [
        "liverpool",
        "lfc",
        "anfield",
        "reds",
    ]

    return any(
        keyword in text
        for keyword in basic_keywords
    )


def is_opponent_only_article(title, summary=""):
    """
    Rejects stories where Liverpool is merely the opponent/context
    and the story is actually about another club/player.

    Example:
    "Richarlison posts crying emoji after being left out of Liverpool tie"
    """

    title_text = normalize_text(title)
    summary_text = normalize_text(summary)

    text = f"{title_text} {summary_text}"

    opponent_context = [
        "left out of liverpool tie",
        "miss liverpool tie",
        "misses liverpool tie",
        "against liverpool",
        "vs liverpool",
        "v liverpool",
        "liverpool opponent",
        "liverpool opponents",
    ]

    player_focus_words = [
        "posts crying",
        "crying emoji",
        "injury blow",
        "ruled out",
        "left out",
        "misses out",
    ]

    if any(
        phrase in text
        for phrase in opponent_context
    ):
        if any(
            phrase in text
            for phrase in player_focus_words
        ):
            return True

    return False


def is_transfer_news(title, summary=""):
    text = normalize_text(
        f"{title} {summary}"
    )

    transfer_words = [
        "transfer",
        "transfers",
        "signing",
        "sign",
        "bid",
        "offer",
        "deal",
        "agreement",
        "agreed",
        "swap",
        "contract",
        "release clause",
        "interest",
        "target",
        "shortlist",
        "move for",
        "join",
        "departure",
    ]

    return any(
        word in text
        for word in transfer_words
    )


# ============================================================
# FRESHNESS
# ============================================================

def is_fresh(
    published_dt,
    title,
    source,
    summary="",
):
    if not published_dt:
        return False

    now = datetime.now(timezone.utc)

    age = now - published_dt

    # Allow a small amount of clock skew into the future.
    if age < timedelta(minutes=-5):
        return False

    if age < timedelta(0):
        age = timedelta(0)

    podcast = is_podcast(
        title,
        summary,
    )

    if podcast:
        if source in PRIORITY_SOURCES:
            return age <= PRIORITY_PODCAST_MAX_AGE

        return False

    return age <= NORMAL_MAX_AGE


# ============================================================
# DISCORD
# ============================================================

def send_to_discord(item):
    if not DISCORD_WEBHOOK:
        print("ERROR: DISCORD_WEBHOOK is missing.")
        return False

    priority = item["source"] in PRIORITY_SOURCES
    transfer = item["is_transfer"]

    if priority:
        prefix = "🟢"
    else:
        prefix = "📰"

    if transfer:
        prefix += " 🚨"

    if is_podcast(
        item["title"],
        item.get("summary", ""),
    ):
        prefix += " 🎙️"

    embed = {
        "title": f"{prefix} {item['title']}",
        "url": item["url"],
        "description": item.get(
            "summary",
            ""
        )[:1000],
        "footer": {
            "text": item["source"]
        },
    }

    payload = {
        "embeds": [embed]
    }

    try:
        response = requests.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=15,
        )

        response.raise_for_status()

        print(
            f"DISCORD SENT: {item['title']}"
        )

        return True

    except Exception as exc:
        print(
            f"Discord error: {exc}"
        )

        return False


# ============================================================
# PROCESS ARTICLE
# ============================================================

def process_article(
    conn,
    title,
    url,
    source,
    summary="",
    published_dt=None,
    source_type="",
):
    title = BeautifulSoup(
        title or "",
        "html.parser",
    ).get_text(" ", strip=True)

    summary = BeautifulSoup(
        summary or "",
        "html.parser",
    ).get_text(" ", strip=True)

    title = re.sub(r"\s+", " ", title).strip()
    summary = re.sub(r"\s+", " ", summary).strip()

    if not title or not url:
        return

    if is_blocked(title, summary):
        print(
            f"BLOCKED: {title}"
        )
        return

    if not is_liverpool_article(
        title,
        summary,
        source_type,
    ):
        print(
            f"Not Liverpool enough: {title}"
        )
        return

    if is_opponent_only_article(
        title,
        summary,
    ):
        print(
            f"Opponent-only article skipped: {title}"
        )
        return

    # If RSS did not provide a publication time,
    # open the actual article.
    if not published_dt:
        print(
            f"No RSS date, checking article page: {title}"
        )

        published_dt = get_article_date(url)

    if not published_dt:
        print(
            f"No valid publication time: {source} {title}"
        )
        return

    now = datetime.now(timezone.utc)

    age = now - published_dt

    if age < timedelta(0):
        age = timedelta(0)

    age_minutes = age.total_seconds() / 60

    podcast = is_podcast(
        title,
        summary,
    )

    if podcast and source not in PRIORITY_SOURCES:
        print(
            f"Podcast skipped - source not priority: {title}"
        )
        return

    if not is_fresh(
        published_dt,
        title,
        source,
        summary,
    ):
        print(
            f"OLD: {title} | age={age_minutes:.1f} min"
        )
        return

    print("")
    print("================================")
    print("FRESH ARTICLE FOUND")
    print("================================")
    print(f"Source: {source}")
    print(f"Title: {title}")
    print(f"Published: {published_dt.isoformat()}")
    print(f"Age: {age_minutes:.1f} minutes")

    if podcast:
        print("Type: PRIORITY PODCAST")
    elif is_transfer_news(title, summary):
        print("Type: TRANSFER")
    else:
        print("Type: NORMAL NEWS")

    print("Status: FRESH")
    print("================================")
    print("")

    if already_seen(conn, url):
        print(
            f"Already sent previously: {title}"
        )
        return

    item = {
        "title": title,
        "url": url,
        "source": source,
        "summary": summary,
        "published_at": published_dt,
        "is_transfer": is_transfer_news(
            title,
            summary,
        ),
    }

    if send_to_discord(item):
        mark_seen(
            conn,
            item,
        )


# ============================================================
# RSS SCANNER
# ============================================================

def scan_rss(conn, feed):
    source = feed["name"]
    url = feed["url"]

    print("")
    print(
        f"Scanning RSS: {source}"
    )

    try:
        parsed = feedparser.parse(url)

    except Exception as exc:
        print(
            f"RSS error: {source} -> {exc}"
        )
        return

    if getattr(parsed, "bozo", False):
        print(
            f"RSS warning: {source}"
        )

    for entry in parsed.entries:
        title = entry.get(
            "title",
            "",
        )

        link = entry.get(
            "link",
            "",
        )

        summary = (
            entry.get("summary")
            or entry.get("description")
            or ""
        )

        published_dt = parse_feed_entry_date(
            entry
        )

        process_article(
            conn=conn,
            title=title,
            url=link,
            source=source,
            summary=summary,
            published_dt=published_dt,
            source_type="rss",
        )


# ============================================================
# WEB SCANNER
# ============================================================

def looks_like_article_url(url):
    if not url:
        return False

    bad_parts = [
        "/podcasts",
        "/podcast",
        "/videos",
        "/video",
        "/live",
        "/search",
        "/login",
        "/register",
        "/app",
        "/bet",
        "/advertise",
    ]

    lower = url.lower()

    if any(
        part in lower
        for part in bad_parts
    ):
        return False

    return True


def find_article_links(soup, base_url):
    """
    Prefer actual <article> elements.
    """

    links = []

    articles = soup.find_all("article")

    for article in articles:
        for a in article.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            href = a.get("href")

            if not title or not href:
                continue

            links.append(
                (title, href, article)
            )

    # If the page does not use article elements,
    # inspect likely content links.
    if not links:
        for a in soup.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            href = a.get("href")

            if len(title) < 25:
                continue

            links.append(
                (title, href, a)
            )

    return links


def absolute_url(base_url, href):
    if not href:
        return None

    if href.startswith("http://"):
        return href

    if href.startswith("https://"):
        return href

    if href.startswith("//"):
        return "https:" + href

    from urllib.parse import urljoin

    return urljoin(
        base_url,
        href,
    )


def scan_web_page(conn, source):
    source_name = source["name"]
    page_url = source["url"]
    source_type = source.get(
        "type",
        "",
    )

    print("")
    print(
        f"Scanning web page: {source_name}"
    )

    try:
        response = requests.get(
            page_url,
            headers=HEADERS,
            timeout=20,
        )

        response.raise_for_status()

    except Exception as exc:
        print(
            f"Web error: {source_name} -> {exc}"
        )
        return

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    links = find_article_links(
        soup,
        page_url,
    )

    seen_urls = set()

    for title, href, element in links:
        article_url = absolute_url(
            page_url,
            href,
        )

        if not article_url:
            continue

        if article_url in seen_urls:
            continue

        seen_urls.add(article_url)

        if not looks_like_article_url(
            article_url
        ):
            continue

        # Skip obvious navigation links.
        if article_url.rstrip("/") == page_url.rstrip("/"):
            continue

        # Do not use the page's own publication date here.
        # We want the individual article date.
        published_dt = None

        # Some article cards contain their own <time>.
        try:
            time_tag = element.find("time")

            if time_tag:
                published_dt = parse_datetime(
                    time_tag.get("datetime")
                    or time_tag.get_text(
                        " ",
                        strip=True,
                    )
                )

        except Exception:
            pass

        process_article(
            conn=conn,
            title=title,
            url=article_url,
            source=source_name,
            summary="",
            published_dt=published_dt,
            source_type=source_type,
        )


# ============================================================
# MAIN
# ============================================================

def main():
    print("")
    print("================================")
    print("Liverpool News Monitor 3.0")
    print("================================")
    print("Freshness:")
    print("📰 Normal news: 10 minutes")
    print("🎙️ Priority podcasts: 24 hours")
    print("")
    print("Priority sources:")

    for source in sorted(PRIORITY_SOURCES):
        print(f"🟢 {source}")

    print("")

    conn = init_db()

    # RSS
    for feed in config.get(
        "rss_feeds",
        [],
    ):
        scan_rss(
            conn,
            feed,
        )

    # Web pages
    for source in config.get(
        "web_sources",
        [],
    ):
        scan_web_page(
            conn,
            source,
        )

    # X sources are registered only.
    # Actual X scanning requires a legitimate API/public-data method.
    print("")
    print("X sources registered:")

    for source in config.get(
        "x_sources",
        [],
    ):
        print(
            f"- {source['name']}: {source['url']}"
        )

    print(
        "X scanning requires a legitimate X API/public-data method."
    )

    conn.close()

    print("")
    print("================================")
    print("SCAN COMPLETE")
    print("================================")


if __name__ == "__main__":
    main()
