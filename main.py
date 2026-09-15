import os
import json
import sqlite3
import hashlib
from datetime import datetime

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

KEYWORDS = [
    keyword.lower()
    for keyword in config["keywords"]
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


def is_liverpool_article(title, summary):

    text = (
        f"{title} {summary}"
    ).lower()

    for keyword in KEYWORDS:

        if keyword in text:
            return True

    return False


# ==========================================
# DATABASE
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
# RSS
# ==========================================

def scan_feed(feed_config):

    print(
        f"Scanning: {feed_config['name']}"
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

        if not title or not url:
            continue

        # Kontrollera Liverpool
        if not is_liverpool_article(
            title,
            summary
        ):
            continue

        # Undvik dubletter
        if article_exists(url):
            continue

        article = {
            "title": title,
            "url": url,
            "summary": summary,
            "source": feed_config["name"],
            "published": published
        }

        print(
            f"NEW: {title}"
        )

        save_article(article)

        send_discord(article)


# ==========================================
# MAIN
# ==========================================

def main():

    print(
        "Liverpool News Monitor started"
    )

    for feed in config["rss_feeds"]:

        try:

            scan_feed(feed)

        except Exception as error:

            print(
                f"ERROR: {feed['name']}: {error}"
            )


if __name__ == "__main__":
    main()
