import os
import json
import sqlite3
import hashlib
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup


# ============================================================
# LIVERPOOL NEWS MONITOR 5.0
# ============================================================

NORMAL_MAX_AGE = timedelta(minutes=10)
PRIORITY_PODCAST_MAX_AGE = timedelta(hours=24)

DB_FILE = "news.db"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


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

BLOCKED_CONTENT = [
    "blood red liverpool podcast",
    "blood red podcast",
    "blood red",
    "anfield sector",
]


# ============================================================
# BAD URL PARTS
# ============================================================

BAD_URL_PARTS = [
    "/live-blog/",
    "/live/",
    "/liveblog/",
    "live-updates",
    "/videos/",
    "/video/",
    "/gallery/",
    "/galleries/",
    "/quiz",
    "/quizzes",
    "/betting/",
    "/bet/",
    "/casino/",
    "/advertise/",
    "/advertising/",
    "/login",
    "/register",
    "/signup",
    "/app/",
    "/podcasts/",
]


# ============================================================
# BAD TITLE PHRASES
# ============================================================

BAD_TITLE_PHRASES = [
    "how to watch",
    "how to watch live",
    "tv channel",
    "kick-off time",
    "kick off time",
    "what time is",
    "where to watch",
    "live stream",
    "commentary stream",
    "live updates",
    "live coverage",
    "live score",
    "matchday programme",
    "quiz",
    "gallery",
    "photos",
    "pictures",
    "video",
    "watch:",
    "highlights",
    "betting tips",
    "odds",
    "casino",
    "giveaway",
    "newsletter",
]


# ============================================================
# CONFIG
# ============================================================

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)


# ============================================================
# STATISTICS
# ============================================================

STATS = {
    "scanned": 0,
    "sent": 0,
    "duplicates": 0,
    "too_old": 0,
    "no_date": 0,
    "not_liverpool": 0,
    "blocked": 0,
    "bad_content": 0,
    "opponent_only": 0,
    "podcast_blocked": 0,
    "errors": 0,
}


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


def normalize_url(url):
    if not url:
        return ""

    url = url.strip()

    parsed = urlparse(url)

    clean = parsed._replace(
        query="",
        fragment="",
    )

    return clean.geturl().rstrip("/")


def make_id(url):
    return hashlib.sha256(
        normalize_url(url).encode("utf-8")
    ).hexdigest()


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
# TEXT HELPERS
# ============================================================

def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        (text or "").strip().lower(),
    )


def clean_text(text):
    if not text:
        return ""

    text = BeautifulSoup(
        text,
        "html.parser",
    ).get_text(
        " ",
        strip=True,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


# ============================================================
# DATE PARSING
# ============================================================

def parse_datetime(value):
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

    try:
        iso_value = value.replace(
            "Z",
            "+00:00",
        )

        dt = datetime.fromisoformat(
            iso_value
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(
            timezone.utc
        )

    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(
            value
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(
            timezone.utc
        )

    except Exception:
        pass

    return None


def parse_feed_entry_date(entry):
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

    for field in [
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ]:
        value = entry.get(field)

        if value:
            try:
                return datetime(
                    value.tm_year,
                    value.tm_mon,
                    value.tm_mday,
                    value.tm_hour,
                    value.tm_min,
                    value.tm_sec,
                    tzinfo=timezone.utc,
                )

            except Exception:
                pass

    return None


# ============================================================
# WEB PAGE DATE EXTRACTION
# ============================================================

def extract_date_from_json_ld(soup):
    scripts = soup.find_all(
        "script",
        attrs={
            "type": "application/ld+json"
        },
    )

    for script in scripts:
        raw = (
            script.string
            or script.get_text(
                strip=True
            )
        )

        if not raw:
            continue

        try:
            data = json.loads(raw)

        except Exception:
            continue

        objects = []

        if isinstance(data, dict):
            objects.append(data)

            graph = data.get("@graph")

            if isinstance(graph, list):
                objects.extend(graph)

        elif isinstance(data, list):
            objects.extend(data)

        for obj in objects:
            if not isinstance(obj, dict):
                continue

            for key in [
                "datePublished",
                "dateCreated",
                "dateModified",
            ]:
                dt = parse_datetime(
                    obj.get(key)
                )

                if dt:
                    return dt

    return None


def extract_date_from_meta(soup):
    possible_names = {
        "article:published_time",
        "datepublished",
        "publishdate",
        "publication_date",
        "published_time",
        "date",
        "dc.date",
        "dc.date.issued",
        "parsely-pub-date",
    }

    for meta in soup.find_all("meta"):
        key = (
            meta.get("property")
            or meta.get("name")
            or meta.get("itemprop")
            or ""
        ).lower().strip()

        if key not in possible_names:
            continue

        value = meta.get("content")

        dt = parse_datetime(value)

        if dt:
            return dt

    return None


def extract_date_from_time_tags(soup):
    for tag in soup.find_all("time"):
        value = (
            tag.get("datetime")
            or tag.get_text(
                " ",
                strip=True,
            )
        )

        dt = parse_datetime(value)

        if dt:
            return dt

    return None


def extract_page_date(soup):
    dt = extract_date_from_json_ld(
        soup
    )

    if dt:
        return dt

    dt = extract_date_from_meta(
        soup
    )

    if dt:
        return dt

    dt = extract_date_from_time_tags(
        soup
    )

    if dt:
        return dt

    return None


def get_article_date(url):
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

        return extract_page_date(
            soup
        )

    except Exception as exc:
        print(
            f"Date error: {url} -> {exc}"
        )

        STATS["errors"] += 1

        return None


# ============================================================
# CONTENT TYPE
# ============================================================

def is_podcast(title, summary=""):
    text = normalize_text(
        f"{title} {summary}"
    )

    words = [
        "podcast",
        "podcasts",
        "episode",
        "ep.",
        "audio",
    ]

    return any(
        word in text
        for word in words
    )


def is_blocked(title, summary="", url=""):
    text = normalize_text(
        f"{title} {summary} {url}"
    )

    for blocked in BLOCKED_CONTENT:
        if blocked in text:
            return True

    return False


def is_bad_content(title, url):
    title_text = normalize_text(
        title
    )

    url_text = normalize_text(
        url
    )

    for phrase in BAD_TITLE_PHRASES:
        if phrase in title_text:
            return True

    for part in BAD_URL_PARTS:
        if part in url_text:
            return True

    return False


# ============================================================
# LIVERPOOL FILTER
# ============================================================

def is_liverpool_article(
    title,
    summary="",
    source_type="",
):
    title_text = normalize_text(
        title
    )

    summary_text = normalize_text(
        summary
    )

    text = (
        f"{title_text} {summary_text}"
    )

    # Dedicated Liverpool pages
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

    keywords = [
        "liverpool",
        "liverpool fc",
        "lfc",
        "anfield",
        "reds",
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


# ============================================================
# OPPONENT FILTER
# ============================================================

def is_opponent_only_article(
    title,
    summary="",
):
    title_text = normalize_text(
        title
    )

    summary_text = normalize_text(
        summary
    )

    text = (
        f"{title_text} {summary_text}"
    )

    opponent_phrases = [
        "against liverpool",
        "vs liverpool",
        "v liverpool",
        "liverpool tie",
        "liverpool clash",
        "liverpool fixture",
        "liverpool return",
    ]

    opponent_focus = [
        "crying",
        "emoji",
        "ruled out",
        "left out",
        "misses out",
        "injury blow",
        "injured",
        "return",
    ]

    if any(
        phrase in text
        for phrase in opponent_phrases
    ):
        if any(
            word in text
            for word in opponent_focus
        ):
            return True

    return False


# ============================================================
# TRANSFER FILTER
# ============================================================

def is_transfer_news(
    title,
    summary="",
):
    text = normalize_text(
        f"{title} {summary}"
    )

    transfer_phrases = [
        "transfer",
        "transfers",
        "signing",
        "sign ",
        "signs ",
        "signed ",
        "bid",
        "offer",
        "deal",
        "agreement",
        "agreed",
        "release clause",
        "interest",
        "target",
        "shortlist",
        "move for",
        "join",
        "joins",
        "contract",
        "departure",
        "exit",
    ]

    return any(
        phrase in text
        for phrase in transfer_phrases
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

    now = datetime.now(
        timezone.utc
    )

    age = now - published_dt

    # Ignore impossible future dates
    if age < timedelta(
        minutes=-5
    ):
        return False

    if age < timedelta(0):
        age = timedelta(0)

    podcast = is_podcast(
        title,
        summary,
    )

    if podcast:
        if source in PRIORITY_SOURCES:
            return (
                age
                <= PRIORITY_PODCAST_MAX_AGE
            )

        return False

    return age <= NORMAL_MAX_AGE


# ============================================================
# DISCORD
# ============================================================

def send_to_discord(item):
    if not DISCORD_WEBHOOK:
        print(
            "ERROR: DISCORD_WEBHOOK missing."
        )

        return False

    priority = (
        item["source"]
        in PRIORITY_SOURCES
    )

    transfer = item["is_transfer"]

    podcast = item["is_podcast"]

    if priority:
        prefix = "🟢"

    else:
        prefix = "📰"

    if transfer:
        prefix += " 🚨"

    if podcast:
        prefix += " 🎙️"

    description = (
        item.get("summary")
        or "Ingen offentlig sammanfattning tillgänglig."
    )

    description = description[:1000]

    if transfer and priority:
        category = "🚨 PRIORITY TRANSFER"

    elif transfer:
        category = "🚨 TRANSFER"

    elif podcast:
        category = "🎙️ PODCAST"

    elif priority:
        category = "🟢 PRIORITY"

    else:
        category = "📰 LIVERPOOL NEWS"

    embed = {
        "title": f"{prefix} {item['title']}",
        "url": item["url"],
        "description": description,
        "fields": [
            {
                "name": "Kategori",
                "value": category,
                "inline": True,
            },
            {
                "name": "Källa",
                "value": item["source"],
                "inline": True,
            },
        ],
        "footer": {
            "text": "Liverpool News Monitor 5.0"
        },
        "timestamp": item[
            "published_at"
        ].isoformat(),
    }

    payload = {
        "embeds": [
            embed
        ]
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

        STATS["sent"] += 1

        return True

    except Exception as exc:
        print(
            f"Discord error: {exc}"
        )

        STATS["errors"] += 1

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
    STATS["scanned"] += 1

    title = clean_text(title)

    summary = clean_text(summary)

    url = normalize_url(url)

    if not title or not url:
        return

    # --------------------------------------------------------
    # BLOCKED CONTENT
    # --------------------------------------------------------

    if is_blocked(
        title,
        summary,
        url,
    ):
        STATS["blocked"] += 1

        print(
            f"BLOCKED: {title}"
        )

        return

    # --------------------------------------------------------
    # BAD CONTENT
    # --------------------------------------------------------

    if is_bad_content(
        title,
        url,
    ):
        STATS["bad_content"] += 1

        print(
            f"BAD CONTENT: {title}"
        )

        return

    # --------------------------------------------------------
    # LIVERPOOL FILTER
    # --------------------------------------------------------

    if not is_liverpool_article(
        title,
        summary,
        source_type,
    ):
        STATS["not_liverpool"] += 1

        print(
            f"NOT LIVERPOOL: {title}"
        )

        return

    # --------------------------------------------------------
    # OPPONENT FILTER
    # --------------------------------------------------------

    if is_opponent_only_article(
        title,
        summary,
    ):
        STATS["opponent_only"] += 1

        print(
            f"OPPONENT ONLY: {title}"
        )

        return

    # --------------------------------------------------------
    # DATE
    # --------------------------------------------------------

    if not published_dt:
        published_dt = get_article_date(
            url
        )

    if not published_dt:
        STATS["no_date"] += 1

        print(
            f"NO DATE: {title}"
        )

        return

    # --------------------------------------------------------
    # PODCAST
    # --------------------------------------------------------

    podcast = is_podcast(
        title,
        summary,
    )

    if (
        podcast
        and source
        not in PRIORITY_SOURCES
    ):
        STATS[
            "podcast_blocked"
        ] += 1

        print(
            f"PODCAST BLOCKED: {title}"
        )

        return

    # --------------------------------------------------------
    # FRESHNESS
    # --------------------------------------------------------

    if not is_fresh(
        published_dt,
        title,
        source,
        summary,
    ):
        STATS["too_old"] += 1

        age = (
            datetime.now(timezone.utc)
            - published_dt
        ).total_seconds() / 60

        print(
            f"OLD: {title} | age={age:.1f} min"
        )

        return

    # --------------------------------------------------------
    # DUPLICATE
    # --------------------------------------------------------

    if already_seen(
        conn,
        url,
    ):
        STATS["duplicates"] += 1

        print(
            f"DUPLICATE: {title}"
        )

        return

    # --------------------------------------------------------
    # ITEM
    # --------------------------------------------------------

    transfer = is_transfer_news(
        title,
        summary,
    )

    item = {
        "title": title,
        "url": url,
        "source": source,
        "summary": summary,
        "published_at": published_dt,
        "is_transfer": transfer,
        "is_podcast": podcast,
    }

    # --------------------------------------------------------
    # LOG
    # --------------------------------------------------------

    print("")
    print(
        "================================"
    )
    print("FRESH ARTICLE")
    print(
        "================================"
    )
    print(f"Source: {source}")
    print(f"Title: {title}")
    print(
        f"Published: "
        f"{published_dt.isoformat()}"
    )

    if transfer:
        print("Type: TRANSFER")

    elif podcast:
        print("Type: PRIORITY PODCAST")

    elif source in PRIORITY_SOURCES:
        print("Type: PRIORITY NEWS")

    else:
        print("Type: NORMAL NEWS")

    print("Status: FRESH")

    print(
        "================================"
    )
    print("")

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

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
        parsed = feedparser.parse(
            url
        )

    except Exception as exc:
        print(
            f"RSS ERROR: {source} -> {exc}"
        )

        STATS["errors"] += 1

        return

    if getattr(
        parsed,
        "bozo",
        False,
    ):
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
            entry.get(
                "summary"
            )
            or entry.get(
                "description"
            )
            or ""
        )

        published_dt = (
            parse_feed_entry_date(
                entry
            )
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

def absolute_url(
    base_url,
    href,
):
    if not href:
        return None

    return urljoin(
        base_url,
        href,
    )


def looks_like_article_url(
    url
):
    if not url:
        return False

    lower = url.lower()

    for part in BAD_URL_PARTS:
        if part in lower:
            return False

    return True


def find_article_links(
    soup,
):
    links = []

    # --------------------------------------------------------
    # FIRST: article elements
    # --------------------------------------------------------

    articles = soup.find_all(
        "article"
    )

    for article in articles:
        heading = article.find(
            [
                "h1",
                "h2",
                "h3",
                "h4",
            ]
        )

        if heading:
            a = heading.find(
                "a",
                href=True,
            )

            if a:
                title = a.get_text(
                    " ",
                    strip=True,
                )

                href = a.get(
                    "href"
                )

                if title and href:
                    links.append(
                        (
                            title,
                            href,
                            article,
                        )
                    )

                    continue

        # fallback inside article
        for a in article.find_all(
            "a",
            href=True,
        ):
            title = a.get_text(
                " ",
                strip=True,
            )

            href = a.get(
                "href"
            )

            if (
                title
                and href
                and len(title) >= 25
            ):
                links.append(
                    (
                        title,
                        href,
                        article,
                    )
                )

                break

    # --------------------------------------------------------
    # SECOND: headings
    # --------------------------------------------------------

    if not links:
        for heading in soup.find_all(
            [
                "h1",
                "h2",
                "h3",
            ]
        ):
            a = heading.find(
                "a",
                href=True,
            )

            if not a:
                continue

            title = a.get_text(
                " ",
                strip=True,
            )

            href = a.get(
                "href"
            )

            if (
                title
                and href
                and len(title) >= 25
            ):
                links.append(
                    (
                        title,
                        href,
                        heading,
                    )
                )

    return links


def scan_web_page(
    conn,
    source,
):
    source_name = source[
        "name"
    ]

    page_url = source[
        "url"
    ]

    source_type = source.get(
        "type",
        "",
    )

    print("")
    print(
        f"Scanning web page: "
        f"{source_name}"
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
            f"WEB ERROR: "
            f"{source_name} -> {exc}"
        )

        STATS["errors"] += 1

        return

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    links = find_article_links(
        soup
    )

    seen_urls = set()

    for (
        title,
        href,
        element,
    ) in links:

        article_url = absolute_url(
            page_url,
            href,
        )

        if not article_url:
            continue

        article_url = normalize_url(
            article_url
        )

        if article_url in seen_urls:
            continue

        seen_urls.add(
            article_url
        )

        if not looks_like_article_url(
            article_url
        ):
            continue

        if (
            article_url.rstrip("/")
            == page_url.rstrip("/")
        ):
            continue

        published_dt = None

        # ----------------------------------------------------
        # CARD DATE
        # ----------------------------------------------------

        try:
            time_tag = (
                element.find(
                    "time"
                )
            )

            if time_tag:
                published_dt = (
                    parse_datetime(
                        time_tag.get(
                            "datetime"
                        )
                        or time_tag.get_text(
                            " ",
                            strip=True,
                        )
                    )
                )

        except Exception:
            pass

        # ----------------------------------------------------
        # ARTICLE DATE
        # ----------------------------------------------------

        if not published_dt:
            published_dt = (
                get_article_date(
                    article_url
                )
            )

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
# SUMMARY
# ============================================================

def print_summary():
    print("")
    print(
        "================================"
    )
    print("SCAN COMPLETE")
    print(
        "================================"
    )

    print(
        f"Scanned:             "
        f"{STATS['scanned']}"
    )

    print(
        f"Discord sent:        "
        f"{STATS['sent']}"
    )

    print(
        f"Duplicates:          "
        f"{STATS['duplicates']}"
    )

    print(
        f"Too old:             "
        f"{STATS['too_old']}"
    )

    print(
        f"No date:             "
        f"{STATS['no_date']}"
    )

    print(
        f"Not Liverpool:       "
        f"{STATS['not_liverpool']}"
    )

    print(
        f"Blocked content:     "
        f"{STATS['blocked']}"
    )

    print(
        f"Bad content type:    "
        f"{STATS['bad_content']}"
    )

    print(
        f"Opponent only:       "
        f"{STATS['opponent_only']}"
    )

    print(
        f"Podcasts blocked:    "
        f"{STATS['podcast_blocked']}"
    )

    print(
        f"Errors:              "
        f"{STATS['errors']}"
    )

    print(
        "================================"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("")
    print(
        "================================"
    )
    print(
        "Liverpool News Monitor 5.0"
    )
    print(
        "================================"
    )

    print(
        "📰 Normal news: 10 minutes"
    )

    print(
        "🚨 Transfer news: 10 minutes"
    )

    print(
        "🎙️ Priority podcasts: 24 hours"
    )

    print(
        "❌ X scanning: DISABLED"
    )

    print("")

    print(
        "Priority sources:"
    )

    for source in sorted(
        PRIORITY_SOURCES
    ):
        print(
            f"🟢 {source}"
        )

    print("")

    conn = init_db()

    # --------------------------------------------------------
    # RSS
    # --------------------------------------------------------

    for feed in config.get(
        "rss_feeds",
        [],
    ):
        scan_rss(
            conn,
            feed,
        )

    # --------------------------------------------------------
    # WEB
    # --------------------------------------------------------

    for source in config.get(
        "web_sources",
        [],
    ):
        scan_web_page(
            conn,
            source,
        )

    # --------------------------------------------------------
    # X IS INTENTIONALLY DISABLED
    # --------------------------------------------------------

    print("")
    print(
        "X scanning is disabled."
    )

    print(
        "No X API, Patreon or Anfield Sector is used."
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print_summary()

    conn.close()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
