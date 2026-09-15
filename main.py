import os
import json
import sqlite3
import hashlib
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

if not DISCORD_WEBHOOK:
    print("ERROR: DISCORD_WEBHOOK is missing.")
    raise SystemExit(1)


# ============================================================
# KEYWORDS
# ============================================================

KEYWORDS = [
    x.lower()
    for x in config.get("keywords", [])
]

STRONG_KEYWORDS = [
    x.lower()
    for x in config.get("strong_keywords", [])
]


# ============================================================
# PRIORITY SOURCES
# ============================================================

PRIORITY_SOURCES = {
    "David Ornstein",
    "James Pearce",
    "Ian Doyle",
    "Alex Crook",
    "Fabrizio Romano",
    "Ben Jacobs",
}


# ============================================================
# BLOCKED CONTENT
# ============================================================

BLOCKED_CONTENT = {
    "blood red liverpool podcast",
}


# ============================================================
# FRESHNESS
# ============================================================

NORMAL_MAX_AGE = timedelta(minutes=10)
PRIORITY_PODCAST_MAX_AGE = timedelta(hours=24)


# ============================================================
# DATABASE
# ============================================================

DB_FILE = "news.db"

db = sqlite3.connect(DB_FILE)

db.execute("""
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    title TEXT,
    url TEXT,
    source TEXT,
    published TEXT,
    created_at TEXT
)
""")

db.commit()


# ============================================================
# HELPERS
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = BeautifulSoup(
        str(text),
        "html.parser"
    ).get_text(" ")

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def normalize_url(url):
    if not url:
        return ""

    return url.strip()


def now_utc():
    return datetime.now(timezone.utc)


# ============================================================
# DATE PARSING
# ============================================================

def parse_datetime(value):
    """
    Attempts to convert common RSS/web date formats
    into a timezone-aware UTC datetime.
    """

    if not value:
        return None

    if isinstance(value, datetime):
        dt = value

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(timezone.utc)

    value = str(value).strip()

    if not value:
        return None

    # feedparser date handling
    try:
        parsed = feedparser._parse_date(value)

        if parsed:
            return datetime(
                parsed.tm_year,
                parsed.tm_mon,
                parsed.tm_mday,
                parsed.tm_hour,
                parsed.tm_min,
                parsed.tm_sec,
                tzinfo=timezone.utc
            )
    except Exception:
        pass

    # ISO 8601
    try:
        normalized = value.replace(
            "Z",
            "+00:00"
        )

        dt = datetime.fromisoformat(
            normalized
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(timezone.utc)

    except Exception:
        pass

    return None


# ============================================================
# FRESHNESS FILTER
# ============================================================

def is_fresh(
    published,
    source,
    title,
    summary=""
):
    """
    Normal news:
        maximum 10 minutes old.

    Podcasts from priority sources:
        maximum 24 hours old.

    If we cannot determine publication time,
    we DO NOT send the item.
    """

    published_dt = parse_datetime(
        published
    )

    if not published_dt:
        print(
            "No valid publication time:",
            source,
            title
        )

        return False

    age = now_utc() - published_dt

    # Future timestamps caused by clock differences
    if age < timedelta(0):
        age = timedelta(0)

    podcast = is_podcast(
        title,
        summary
    )

    priority = is_priority_source(
        source
    )

    if podcast and priority:
        if age <= PRIORITY_PODCAST_MAX_AGE:
            return True

        print(
            "Old priority podcast:",
            source,
            title
        )

        return False

    if age <= NORMAL_MAX_AGE:
        return True

    print(
        "Old article:",
        source,
        title,
        "age:",
        age
    )

    return False


# ============================================================
# CONTENT TYPE
# ============================================================

def is_podcast(
    title,
    summary=""
):
    text = clean_text(
        f"{title} {summary}"
    ).lower()

    podcast_words = [
        "podcast",
        "audio",
        "listen",
        "episode",
        "ep."
    ]

    return any(
        word in text
        for word in podcast_words
    )


# ============================================================
# PRIORITY
# ============================================================

def is_priority_source(source_name):
    if not source_name:
        return False

    source_lower = source_name.lower()

    for priority in PRIORITY_SOURCES:
        if priority.lower() in source_lower:
            return True

    return False


# ============================================================
# BLOCKED CONTENT
# ============================================================

def is_blocked_content(
    title,
    summary="",
    url=""
):
    text = clean_text(
        f"{title} {summary} {url}"
    ).lower()

    for blocked in BLOCKED_CONTENT:
        if blocked in text:
            return True

    return False


# ============================================================
# LIVERPOOL RELEVANCE
# ============================================================

def is_liverpool_article(
    title,
    summary="",
    source_type=""
):
    text = clean_text(
        f"{title} {summary}"
    ).lower()

    # Strong Liverpool terms
    for keyword in STRONG_KEYWORDS:
        if keyword in text:
            return True

    # General Liverpool terms
    for keyword in KEYWORDS:
        if keyword in text:
            return True

    # Liverpool-specific source pages
    if source_type in {
        "liverpool_page",
        "liverpool_official"
    }:
        return True

    return False


# ============================================================
# AVOID OBVIOUS OPPONENT-ONLY STORIES
# ============================================================

def is_opponent_only_story(
    title,
    summary=""
):
    text = clean_text(
        f"{title} {summary}"
    ).lower()

    opponent_patterns = [
        "liverpool tie",
        "liverpool clash",
        "liverpool game",
        "liverpool match",
        "liverpool fixture",
        "vs liverpool",
        "v liverpool",
        "against liverpool",
    ]

    # These are not automatically rejected.
    # We only reject them when the article strongly
    # appears to be about somebody else.
    other_subject_patterns = [
        "posts crying emoji",
        "left out of",
        "reacts to",
        "reacted to",
        "speaks about liverpool",
    ]

    has_opponent_pattern = any(
        pattern in text
        for pattern in opponent_patterns
    )

    has_other_subject = any(
        pattern in text
        for pattern in other_subject_patterns
    )

    return (
        has_opponent_pattern
        and has_other_subject
    )


# ============================================================
# TRANSFER DETECTION
# ============================================================

TRANSFER_KEYWORDS = [
    "transfer",
    "transfers",
    "sign",
    "signing",
    "signed",
    "deal",
    "agreement",
    "bid",
    "offer",
    "interest",
    "interested",
    "target",
    "targets",
    "negotiation",
    "negotiations",
    "talks",
    "contract",
    "contract extension",
    "medical",
    "here we go",
    "move",
    "loan",
    "swap",
    "release clause",
]


def is_transfer_news(
    title,
    summary=""
):
    text = clean_text(
        f"{title} {summary}"
    ).lower()

    return any(
        keyword in text
        for keyword in TRANSFER_KEYWORDS
    )


# ============================================================
# ARTICLE ID
# ============================================================

def article_id(
    title,
    url
):
    raw = (
        f"{title}|{url}"
        .encode(
            "utf-8",
            errors="ignore"
        )
    )

    return hashlib.sha256(
        raw
    ).hexdigest()


# ============================================================
# DATABASE
# ============================================================

def already_seen(
    article_hash
):
    cursor = db.execute(
        "SELECT 1 FROM articles WHERE id = ?",
        (article_hash,)
    )

    return cursor.fetchone() is not None


def save_article(
    article_hash,
    title,
    url,
    source,
    published=""
):
    db.execute(
        """
        INSERT OR IGNORE INTO articles
        (id, title, url, source, published, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            article_hash,
            title,
            url,
            source,
            published,
            now_utc().isoformat()
        )
    )

    db.commit()


# ============================================================
# DISCORD MESSAGE
# ============================================================

def build_discord_message(
    title,
    url,
    source,
    summary=""
):
    priority = is_priority_source(
        source
    )

    transfer = is_transfer_news(
        title,
        summary
    )

    podcast = is_podcast(
        title,
        summary
    )

    if priority and transfer:
        prefix = "🟢 🚨"

    elif priority and podcast:
        prefix = "🟢 🎙️"

    elif priority:
        prefix = "🟢"

    elif transfer:
        prefix = "🚨"

    elif podcast:
        prefix = "🎙️"

    else:
        prefix = "📰"

    return (
        f"{prefix} **{source}**\n\n"
        f"**{title}**\n\n"
        f"🔗 {url}"
    )


# ============================================================
# SEND DISCORD
# ============================================================

def send_discord(
    title,
    url,
    source,
    summary=""
):
    message = build_discord_message(
        title,
        url,
        source,
        summary
    )

    payload = {
        "content": message
    }

    try:
        response = requests.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=20
        )

    except Exception as e:
        print(
            "Discord request failed:",
            e
        )

        return False

    if response.status_code not in (
        200,
        204
    ):
        print(
            "Discord error:",
            response.status_code,
            response.text
        )

        return False

    print(
        "Discord sent:",
        source,
        title
    )

    return True


# ============================================================
# PROCESS ARTICLE
# ============================================================

def process_article(
    title,
    url,
    source,
    summary="",
    published="",
    source_type=""
):
    title = clean_text(title)
    summary = clean_text(summary)
    url = normalize_url(url)

    if not title or not url:
        return

    # Block explicitly unwanted content
    if is_blocked_content(
        title,
        summary,
        url
    ):
        print(
            "Blocked:",
            source,
            title
        )

        return

    # Liverpool relevance
    if not is_liverpool_article(
        title,
        summary,
        source_type
    ):
        print(
            "Not Liverpool:",
            source,
            title
        )

        return

    # Avoid obvious opponent-only stories
    if is_opponent_only_story(
        title,
        summary
    ):
        print(
            "Opponent-only story:",
            source,
            title
        )

        return

    # Freshness
    if not is_fresh(
        published,
        source,
        title,
        summary
    ):
        return

    article_hash = article_id(
        title,
        url
    )

    # Duplicate protection
    if already_seen(
        article_hash
    ):
        print(
            "Already seen:",
            title
        )

        return

    # Send
    success = send_discord(
        title,
        url,
        source,
        summary
    )

    # Save only after successful send
    if success:
        save_article(
            article_hash,
            title,
            url,
            source,
            published
        )


# ============================================================
# RSS SCANNER
# ============================================================

def scan_rss():
    feeds = config.get(
        "rss_feeds",
        []
    )

    for feed_config in feeds:

        name = feed_config.get(
            "name",
            "RSS"
        )

        url = feed_config.get(
            "url"
        )

        if not url:
            continue

        print(
            f"Scanning RSS: {name}"
        )

        try:
            feed = feedparser.parse(
                url
            )

            for entry in feed.entries:

                title = entry.get(
                    "title",
                    ""
                )

                link = entry.get(
                    "link",
                    ""
                )

                summary = entry.get(
                    "summary",
                    entry.get(
                        "description",
                        ""
                    )
                )

                published = entry.get(
                    "published",
                    entry.get(
                        "updated",
                        ""
                    )
                )

                process_article(
                    title,
                    link,
                    name,
                    summary,
                    published,
                    "rss"
                )

        except Exception as e:
            print(
                f"RSS error ({name}): {e}"
            )


# ============================================================
# WEB DATE EXTRACTION
# ============================================================

def extract_published_date(
    element
):
    """
    Attempts to find publication date from
    HTML metadata and time tags.
    """

    # <time datetime="...">
    time_tag = element.find(
        "time"
    )

    if time_tag:

        value = (
            time_tag.get("datetime")
            or time_tag.get_text(
                " ",
                strip=True
            )
        )

        dt = parse_datetime(
            value
        )

        if dt:
            return dt

    # Common metadata
    meta_names = [
        "article:published_time",
        "og:published_time",
        "datePublished",
        "date",
        "pubdate",
        "publish-date",
        "published_time",
    ]

    for meta_name in meta_names:

        meta = element.find(
            "meta",
            attrs={
                "property": meta_name
            }
        )

        if not meta:
            meta = element.find(
                "meta",
                attrs={
                    "name": meta_name
                }
            )

        if meta:

            value = meta.get(
                "content"
            )

            dt = parse_datetime(
                value
            )

            if dt:
                return dt

    return None


# ============================================================
# WEB ARTICLE CANDIDATES
# ============================================================

def get_article_candidates(
    soup,
    base_url
):
    candidates = []

    # Prefer actual article elements
    article_elements = soup.find_all(
        "article"
    )

    # If page has no <article> tags,
    # use headings as fallback.
    if not article_elements:

        article_elements = []

        for heading in soup.find_all(
            ["h1", "h2", "h3"]
        ):
            article_elements.append(
                heading.parent
            )

    seen = set()

    for element in article_elements:

        if not element:
            continue

        # Find links inside article/card
        links = element.find_all(
            "a",
            href=True
        )

        for link in links:

            title = clean_text(
                link.get_text(
                    " ",
                    strip=True
                )
            )

            href = link.get(
                "href"
            )

            if not title or not href:
                continue

            if len(title) < 20:
                continue

            href = urljoin(
                base_url,
                href
            )

            if not href.startswith(
                "http"
            ):
                continue

            key = (
                title.lower(),
                href
            )

            if key in seen:
                continue

            seen.add(key)

            published_dt = (
                extract_published_date(
                    element
                )
            )

            candidates.append(
                (
                    title,
                    href,
                    published_dt
                )
            )

    return candidates


# ============================================================
# WEB SCANNER
# ============================================================

def scan_web_page(
    source_config
):
    name = source_config.get(
        "name",
        "Web"
    )

    url = source_config.get(
        "url"
    )

    source_type = source_config.get(
        "type",
        "web"
    )

    if not url:
        return

    print(
        f"Scanning web page: {name}"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; "
            "LiverpoolNewsMonitor/2.0)"
        )
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=20
        )

        if response.status_code != 200:

            print(
                f"Web error ({name}): "
                f"{response.status_code}"
            )

            return

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        candidates = get_article_candidates(
            soup,
            url
        )

        for (
            title,
            href,
            published_dt
        ) in candidates:

            if not published_dt:

                print(
                    "Skipping web item without date:",
                    name,
                    title
                )

                continue

            process_article(
                title,
                href,
                name,
                "",
                published_dt,
                source_type
            )

    except Exception as e:

        print(
            f"Web error ({name}): {e}"
        )


# ============================================================
# X SOURCES
# ============================================================

def show_x_sources():

    x_sources = config.get(
        "x_sources",
        []
    )

    print("")
    print(
        "X sources registered:"
    )

    for source in x_sources:

        print(
            f"  - {source.get('name')}: "
            f"{source.get('url')}"
        )

    print(
        "X scanning requires a legitimate "
        "X API/public-data method."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "Liverpool News Monitor 2.0"
    )

    print(
        "================================"
    )

    print(
        "Freshness:"
    )

    print(
        "  📰 Normal news: 10 minutes"
    )

    print(
        "  🎙️ Priority podcasts: 24 hours"
    )

    print("")

    print(
        "Priority sources:"
    )

    for source in sorted(
        PRIORITY_SOURCES
    ):
        print(
            f"  🟢 {source}"
        )

    print("")

    # RSS
    scan_rss()

    print("")

    # Web sources
    for source_config in config.get(
        "web_sources",
        []
    ):

        scan_web_page(
            source_config
        )

    print("")

    # X sources
    show_x_sources()

    print("")
    print(
        "Scan complete."
    )


if __name__ == "__main__":
    main()
