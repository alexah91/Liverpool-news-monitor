import os
import json
import sqlite3
from datetime import datetime
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup


# ==========================================
# CONFIG
# ==========================================

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]


# ==========================================
# DATABASE
# ==========================================

db = sqlite3.connect("news.db")

db.execute("""
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    title TEXT,
    source TEXT,
    published TEXT,
    created_at TEXT
)
""")

db.commit()


# ==========================================
# LIVERPOOL FILTER
# ==========================================

STRONG_KEYWORDS = [
    keyword.lower()
    for keyword in config.get("strong_keywords", [])
]


def clean_text(text):
    if not text:
        return ""

    soup = BeautifulSoup(
        text,
        "html.parser"
    )

    return soup.get_text(
        " ",
        strip=True
    )


def is_liverpool_article(title, summary=""):

    text = clean_text(
        f"{title} {summary}"
    ).lower()

    # Viktigt:
    # "reds" används INTE längre.
    # Artikeln måste ha en tydlig Liverpool-signal.

    for keyword in STRONG_KEYWORDS:

        if keyword in text:
            return True

    return False


# ==========================================
# DATABASE FUNCTIONS
# ==========================================

def article_exists(url):

    cursor = db.execute(
        """
        SELECT id
        FROM articles
        WHERE url = ?
        """,
        (url,)
    )

    return cursor.fetchone() is not None


def save_article(article):

    db.execute(
        """
        INSERT OR IGNORE INTO articles
        (
            url,
            title,
            source,
            published,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            article["url"],
            article["title"],
            article["source"],
            article["published"],
            datetime.utcnow().isoformat()
        )
    )

    db.commit()


# ==========================================
# DISCORD
# ==========================================

def send_discord(article):

    embed = {
        "title": article["title"],
        "url": article["url"],
        "description": article["summary"][:1000],
        "color": 0xC8102E,

        "fields": [
            {
                "name": "Källa",
                "value": article["source"],
                "inline": True
            },
            {
                "name": "Publicerad",
                "value": article["published"] or "Okänd",
                "inline": True
            }
        ],

        "footer": {
            "text": "Liverpool News Monitor"
        }
    }

    payload = {
        "content": "🔴 **NY LIVERPOOL-NYHET**",
        "embeds": [embed]
    }

    response = requests.post(
        DISCORD_WEBHOOK,
        json=payload,
        timeout=15
    )

    response.raise_for_status()


# ==========================================
# SEND ARTICLE
# ==========================================

def process_article(
    title,
    url,
    summary,
    source,
    published=""
):

    if not title or not url:
        return

    if not url.startswith(
        ("http://", "https://")
    ):
        return

    if article_exists(url):
        return

    article = {
        "title": title.strip(),
        "url": url.strip(),
        "summary": clean_text(summary),
        "source": source,
        "published": published
    }

    print(
        f"CHECKING: {title}"
    )

    if not is_liverpool_article(
        article["title"],
        article["summary"]
    ):
        print(
            f"IGNORED - not Liverpool: {title}"
        )
        return

    print(
        f"NEW LIVERPOOL ARTICLE: {title}"
    )

    # Discord först.
    # Spara endast om Discord lyckas.
    send_discord(article)

    save_article(article)


# ==========================================
# RSS
# ==========================================

def scan_feed(feed_config):

    print(
        f"Scanning RSS: {feed_config['name']}"
    )

    feed = feedparser.parse(
        feed_config["url"]
    )

    for entry in feed.entries:

        title = entry.get(
            "title",
            ""
        ).strip()

        url = entry.get(
            "link",
            ""
        ).strip()

        summary = clean_text(
            entry.get(
                "summary",
                ""
            )
        )

        published = entry.get(
            "published",
            ""
        )

        process_article(
            title,
            url,
            summary,
            feed_config["name"],
            published
        )


# ==========================================
# WEB PAGE
# ==========================================

def get_page(url):

    headers = {
        "User-Agent":
        "Mozilla/5.0 Liverpool-News-Monitor/1.0"
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=20
    )

    response.raise_for_status()

    return response.text


# ==========================================
# OFFICIAL / LIVERPOOL PAGES
# ==========================================

def scan_liverpool_page(source):

    print(
        f"Scanning Liverpool page: {source['name']}"
    )

    html = get_page(
        source["url"]
    )

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    found = set()

    for link in soup.find_all("a"):

        title = link.get_text(
            " ",
            strip=True
        )

        href = link.get("href")

        if not title or not href:
            continue

        url = urljoin(
            source["url"],
            href
        )

        if url in found:
            continue

        found.add(url)

        # Endast riktiga webbadresser
        if not url.startswith(
            ("http://", "https://")
        ):
            continue

        # Liverpool FC
        if source["type"] == "liverpool_official":

            if "liverpoolfc.com" not in url:
                continue

            if "/news/" not in url:
                continue

            # Officiella Liverpool-sidan är redan
            # Liverpool-specifik.
            process_article(
                title,
                url,
                title,
                source["name"]
            )

        # Övriga Liverpool-sidor
        else:

            if not is_liverpool_article(
                title,
                ""
            ):
                continue

            process_article(
                title,
                url,
                title,
                source["name"]
            )


# ==========================================
# AUTHOR PAGES
# ==========================================

def scan_author_page(source):

    print(
        f"Scanning author: {source['name']}"
    )

    html = get_page(
        source["url"]
    )

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    found = set()

    for link in soup.find_all("a"):

        title = link.get_text(
            " ",
            strip=True
        )

        href = link.get("href")

        if not title or not href:
            continue

        url = urljoin(
            source["url"],
            href
        )

        if url in found:
            continue

        found.add(url)

        if not url.startswith(
            ("http://", "https://")
        ):
            continue

        # Endast artiklar som ser ut som
        # riktiga artikellänkar.
        if not any(
            part in url.lower()
            for part in [
                "/football/",
                "/sport/",
                "/liverpool/",
                "/article/",
                "/news/"
            ]
        ):
            continue

        # Mycket viktigt:
        # Författaren i sig räcker INTE.
        # Själva artikeln måste ha Liverpool-signal.
        if not is_liverpool_article(
            title,
            ""
        ):
            continue

        process_article(
            title,
            url,
            title,
            source["name"]
        )


# ==========================================
# MAIN
# ==========================================

def main():

    print(
        "================================"
    )

    print(
        "Liverpool News Monitor started"
    )

    print(
        "Strict Liverpool filtering: ON"
    )

    print(
        "================================"
    )

    # --------------------------------------
    # RSS SOURCES
    # --------------------------------------

    for feed in config.get(
        "rss_feeds",
        []
    ):

        try:

            scan_feed(feed)

        except Exception as error:

            print(
                f"ERROR RSS {feed['name']}: {error}"
            )


    # --------------------------------------
    # WEB SOURCES
    # --------------------------------------

    for source in config.get(
        "web_sources",
        []
    ):

        try:

            if source["type"] == "author_page":

                scan_author_page(
                    source
                )

            else:

                scan_liverpool_page(
                    source
                )

        except Exception as error:

            print(
                f"ERROR WEB {source['name']}: {error}"
            )


    # --------------------------------------
    # X SOURCES
    # --------------------------------------

    print(
        "X sources registered:"
    )

    for source in config.get(
        "x_sources",
        []
    ):

        print(
            f"- {source['name']}: {source['url']}"
        )

    print(
        "X monitoring will be added separately."
    )


if __name__ == "__main__":
    main()
