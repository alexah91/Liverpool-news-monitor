import os
import json
import sqlite3
import hashlib
import re
from datetime import datetime, timezone

import requests
import feedparser
from bs4 import BeautifulSoup


# ============================================================
# LOAD CONFIG
# ============================================================

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)


DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

if not DISCORD_WEBHOOK:
    print("ERROR: DISCORD_WEBHOOK is missing.")
    raise SystemExit(1)


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
# TEXT HELPERS
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = BeautifulSoup(str(text), "html.parser").get_text(" ")

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_url(url):
    if not url:
        return ""

    return url.strip()


# ============================================================
# LIVERPOOL FILTER
# ============================================================

def is_liverpool_article(title, summary=""):
    text = clean_text(
        f"{title} {summary}"
    ).lower()

    # Strong Liverpool-specific keywords
    for keyword in STRONG_KEYWORDS:
        if keyword in text:
            return True

    # General Liverpool keywords
    for keyword in KEYWORDS:
        if keyword in text:
            return True

    return False


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


def is_transfer_news(title, summary=""):
    text = clean_text(
        f"{title} {summary}"
    ).lower()

    return any(
        keyword in text
        for keyword in TRANSFER_KEYWORDS
    )


# ============================================================
# PRIORITY SOURCE
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
# MESSAGE FORMAT
# ============================================================

def build_discord_message(
    title,
    url,
    source,
    summary=""
):
    priority = is_priority_source(source)
    transfer = is_transfer_news(title, summary)

    if priority and transfer:
        prefix = "🟢 🚨"
    elif priority:
        prefix = "🟢"
    elif transfer:
        prefix = "🚨"
    else:
        prefix = "📰"

    message = (
        f"{prefix} **{source}**\n\n"
        f"**{title}**\n\n"
        f"🔗 {url}"
    )

    return message


# ============================================================
# DISCORD
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

    response = requests.post(
        DISCORD_WEBHOOK,
        json=payload,
        timeout=20
    )

    if response.status_code not in (200, 204):
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
# DATABASE HELPERS
# ============================================================

def article_id(title, url):
    raw = f"{title}|{url}".encode(
        "utf-8",
        errors="ignore"
    )

    return hashlib.sha256(raw).hexdigest()


def already_seen(article_hash):
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
            datetime.now(timezone.utc).isoformat()
        )
    )

    db.commit()


# ============================================================
# PROCESS ARTICLE
# ============================================================

def process_article(
    title,
    url,
    source,
    summary="",
    published=""
):
    title = clean_text(title)
    summary = clean_text(summary)
    url = normalize_url(url)

    if not title or not url:
        return

    # Only Liverpool content
    if not is_liverpool_article(
        title,
        summary
    ):
        print(
            "Filtered:",
            source,
            title
        )

        return

    article_hash = article_id(
        title,
        url
    )

    # Do not send duplicates
    if already_seen(article_hash):
        print(
            "Already seen:",
            title
        )

        return

    # Send first
    success = send_discord(
        title,
        url,
        source,
        summary
    )

    # Only save after successful Discord delivery
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
            feed = feedparser.parse(url)

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
                    ""
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
                    published
                )

        except Exception as e:
            print(
                f"RSS error ({name}): {e}"
            )


# ============================================================
# WEB PAGE SCANNER
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

    if not url:
        return

    print(
        f"Scanning web page: {name}"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; "
            "LiverpoolNewsMonitor/1.0)"
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

        links = soup.find_all(
            "a",
            href=True
        )

        seen_links = set()

        for link in links:

            href = link.get("href")

            title = clean_text(
                link.get_text(" ", strip=True)
            )

            if not href or not title:
                continue

            if href.startswith("/"):
                from urllib.parse import urljoin

                href = urljoin(
                    url,
                    href
                )

            if not href.startswith(
                "http"
            ):
                continue

            if href in seen_links:
                continue

            seen_links.add(href)

            if len(title) < 15:
                continue

            # Limit obvious navigation links
            if title.lower() in {
                "home",
                "login",
                "subscribe",
                "contact",
                "menu",
                "search",
            }:
                continue

            process_article(
                title,
                href,
                name
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
# TEST DISCORD
# ============================================================

def test_discord():
    message = (
        "🟢 **Liverpool News Monitor**\n\n"
        "Scanner is online and working."
    )

    response = requests.post(
        DISCORD_WEBHOOK,
        json={
            "content": message
        },
        timeout=20
    )

    if response.status_code in (
        200,
        204
    ):
        print(
            "Discord test successful."
        )

    else:
        print(
            "Discord test failed:",
            response.status_code,
            response.text
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "Liverpool News Monitor"
    )

    print(
        "================================"
    )

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

    # Test Discord
    test_discord()

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

    # X sources are registered,
    # but not scraped/bypassed.
    show_x_sources()

    print("")
    print(
        "Scan complete."
    )


if __name__ == "__main__":
    main()
