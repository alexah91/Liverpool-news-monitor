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
# SETTINGS
# ============================================================

NORMAL_MAX_AGE = timedelta(minutes=10)
PRIORITY_PODCAST_MAX_AGE = timedelta(hours=24)

DB_FILE = "news.db"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
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

# Content that should normally never become a news alert.
BLOCKED_PHRASES = [
    "how to watch",
    "how to watch live",
    "where to watch",
    "watch live",
    "live stream",
    "live streaming",
    "tv channel",
    "kick-off time",
    "kickoff time",
    "what time is",
    "quiz",
    "take our quiz",
    "gallery",
    "click here",
    "download our app",
    "win tickets",
    "betting",
    "odds",
]

# URL patterns that are normally not real news articles.
BLOCKED_URL_PARTS = [
    "/live-blog/",
    "/liveblog/",
    "/live-blog",
    "/live/",
    "/live-updates",
    "/live_updates",
    "/commentary",
    "/videos/",
    "/video/",
    "/podcasts/",
    "/podcast/",
    "/quiz/",
    "/quizzes/",
    "/gallery/",
    "/search",
    "/login",
    "/register",
    "/signup",
    "/app",
    "/betting",
    "/bet/",
    "/advertise",
]

# Navigation / utility links.
BLOCKED_LINK_TEXT = [
    "home",
    "login",
    "sign in",
    "register",
    "subscribe",
    "newsletter",
    "contact",
    "about us",
    "privacy",
    "terms",
    "cookie",
    "fixtures",
    "results",
    "table",
    "squad",
    "tickets",
]


# ============================================================
# CONFIG
# ============================================================

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)


# ============================================================
# STATISTICS
# ============================================================

stats = {
    "scanned": 0,
    "sent": 0,
    "duplicates": 0,
    "old": 0,
    "no_date": 0,
    "not_liverpool": 0,
    "blocked": 0,
    "bad_type": 0,
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


def make_id(url):
    normalized = normalize_url(url)
    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def normalize_url(url):
    """
    Removes common tracking parameters so the same article
    does not appear as several different URLs.
    """

    if not url:
        return ""

    try:
        parsed = urlparse(url)

        clean_query = []

        for key, value in [
            part.split("=", 1) if "=" in part else (part, "")
            for part in parsed.query.split("&")
            if part
        ]:
            key_lower = key.lower()

            if key_lower.startswith("utm_"):
                continue

            if key_lower in {
                "fbclid",
                "gclid",
                "ref",
                "source",
            }:
                continue

            clean_query.append(
                f"{key}={value}"
                if value
                else key
            )

        query = "&".join(clean_query)

        result = parsed._replace(
            query=query,
            fragment="",
        )

        return result.geturl().rstrip("/")

    except Exception:
        return url.split("#")[0].rstrip("/")


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
            normalize_url(item["url"]),
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
    Attempts to parse common date formats.
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
# PAGE METADATA
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
            ]:
                dt = parse_datetime(
                    obj.get(key)
                )

                if dt:
                    return dt

    return None


def extract_date_from_meta(soup):
    wanted = {
        "article:published_time",
        "datepublished",
        "publishdate",
        "publication_date",
        "published_time",
        "date",
        "datepublished",
    }

    for meta in soup.find_all("meta"):
        key = (
            meta.get("property")
            or meta.get("name")
            or meta.get("itemprop")
            or ""
        ).lower().strip()

        if key not in wanted:
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
    dt = extract_date_from_json_ld(soup)

    if dt:
        return dt

    dt = extract_date_from_meta(soup)

    if dt:
        return dt

    dt = extract_date_from_time_tags(soup)

    if dt:
        return dt

    return None


def get_article_data(url):
    """
    Opens an article and extracts publication date
    and a public description/summary.
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

        published_dt = extract_page_date(
            soup
        )

        summary = ""

        # OpenGraph description.
        for key in [
            "og:description",
            "description",
        ]:
            meta = soup.find(
                "meta",
                attrs={
                    "property": key
                },
            )

            if not meta:
                meta = soup.find(
                    "meta",
                    attrs={
                        "name": key
                    },
                )

            if meta and meta.get(
                "content"
            ):
                summary = meta.get(
                    "content"
                ).strip()

                break

        # JSON-LD description fallback.
        if not summary:
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

                    if isinstance(
                        data.get("@graph"),
                        list,
                    ):
                        objects.extend(
                            data["@graph"]
                        )

                elif isinstance(data, list):
                    objects.extend(data)

                for obj in objects:
                    if not isinstance(
                        obj,
                        dict,
                    ):
                        continue

                    if obj.get(
                        "description"
                    ):
                        summary = str(
                            obj["description"]
                        ).strip()

                        break

                if summary:
                    break

        return {
            "published_dt": published_dt,
            "summary": summary,
        }

    except Exception as exc:
        print(
            f"Could not read article: {url}"
        )
        print(
            f"Reason: {exc}"
        )

        stats["errors"] += 1

        return {
            "published_dt": None,
            "summary": "",
        }


# ============================================================
# TEXT / FILTER HELPERS
# ============================================================

def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        (text or "").strip().lower(),
    )


def clean_text(text):
    text = BeautifulSoup(
        text or "",
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


def is_blocked(title, summary=""):
    text = normalize_text(
        f"{title} {summary}"
    )

    for blocked in BLOCKED_CONTENT:
        if blocked in text:
            return True

    for phrase in BLOCKED_PHRASES:
        if phrase in text:
            return True

    return False


def is_podcast(title, summary=""):
    text = normalize_text(
        f"{title} {summary}"
    )

    podcast_words = [
        "podcast",
        "podcasts",
        "episode",
        "ep.",
        "listen",
    ]

    return any(
        word in text
        for word in podcast_words
    )


def is_liverpool_article(
    title,
    summary="",
    source_type="",
):
    title_text = normalize_text(title)
    summary_text = normalize_text(summary)

    text = (
        f"{title_text} "
        f"{summary_text}"
    )

    # Dedicated Liverpool pages are allowed,
    # but bad content is still blocked elsewhere.
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
        keyword = normalize_text(
            keyword
        )

        if keyword and keyword in text:
            return True

    basic_keywords = [
        "liverpool",
        "liverpool fc",
        "lfc",
        "anfield",
        "reds",
    ]

    return any(
        keyword in text
        for keyword in basic_keywords
    )


def is_opponent_only_article(
    title,
    summary="",
):
    text = normalize_text(
        f"{title} {summary}"
    )

    # Examples of stories where Liverpool is
    # merely the opponent/context.
    opponent_patterns = [
        r"\bafter .* against liverpool\b",
        r"\bafter .* vs liverpool\b",
        r"\bafter .* v liverpool\b",
        r"\bagainst liverpool\b",
        r"\bvs liverpool\b",
        r"\bv liverpool\b",
        r"\bmiss(es)? the liverpool game\b",
        r"\bmiss(es)? liverpool tie\b",
        r"\bleft out of liverpool tie\b",
    ]

    liverpool_words = [
        "liverpool",
        "lfc",
        "anfield",
    ]

    if not any(
        word in text
        for word in liverpool_words
    ):
        return False

    # Do not reject genuine Liverpool stories.
    if any(
        phrase in text
        for phrase in [
            "liverpool sign",
            "liverpool agree",
            "liverpool deal",
            "liverpool injury",
            "liverpool team",
            "liverpool lineup",
            "liverpool line-up",
            "liverpool contract",
            "liverpool manager",
            "liverpool boss",
            "liverpool midfielder",
            "liverpool defender",
            "liverpool striker",
            "liverpool forward",
        ]
    ):
        return False

    return any(
        re.search(
            pattern,
            text,
        )
        for pattern in opponent_patterns
    )


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
        "sign for",
        "signs for",
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
        "interested in",
        "target",
        "shortlist",
        "move for",
        "join",
        "departure",
        "exit",
        "loan",
    ]

    return any(
        phrase in text
        for phrase in transfer_phrases
    )


def looks_like_video_only(
    title,
    summary="",
    url="",
):
    text = normalize_text(
        f"{title} {summary}"
    )

    url_text = normalize_text(url)

    video_words = [
        "watch:",
        "watch -",
        "watch ",
        "(video)",
        "video:",
    ]

    if any(
        word in text
        for word in video_words
    ):
        return True

    if "/video/" in url_text:
        return True

    if "/videos/" in url_text:
        return True

    return False


# ============================================================
# URL FILTERING
# ============================================================

def looks_like_article_url(url):
    if not url:
        return False

    lower = url.lower()

    for part in BLOCKED_URL_PARTS:
        if part in lower:
            return False

    return True


def is_navigation_link(title, url):
    title_text = normalize_text(title)

    if title_text in BLOCKED_LINK_TEXT:
        return True

    if len(title_text) < 25:
        return True

    if not url:
        return True

    return False


def absolute_url(
    base_url,
    href,
):
    if not href:
        return None

    if href.startswith(
        "http://"
    ):
        return href

    if href.startswith(
        "https://"
    ):
        return href

    if href.startswith("//"):
        return "https:" + href

    return urljoin(
        base_url,
        href,
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

    # Reject dates more than 5 minutes in the future.
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

def discord_color(item):
    if item["priority"] and item["is_transfer"]:
        return 15158332  # red

    if item["priority"]:
        return 3066993  # green

    if item["is_transfer"]:
        return 15158332  # red

    if item["is_podcast"]:
        return 10181046  # purple

    return 3447003  # blue


def send_to_discord(item):
    if not DISCORD_WEBHOOK:
        print(
            "ERROR: DISCORD_WEBHOOK is missing."
        )
        return False

    prefix = "📰"

    if item["priority"]:
        prefix = "🟢"

    if item["is_transfer"]:
        prefix += " 🚨"

    if item["is_podcast"]:
        prefix += " 🎙️"

    description = (
        item.get(
            "summary",
            "",
        )
        or "Ingen offentlig sammanfattning tillgänglig."
    )

    description = clean_text(
        description
    )[:900]

    published = item[
        "published_at"
    ].astimezone(
        timezone.utc
    )

    now = datetime.now(
        timezone.utc
    )

    age_seconds = max(
        0,
        int(
            (
                now - published
            ).total_seconds()
        ),
    )

    age_minutes = age_seconds // 60

    if age_minutes < 1:
        age_text = "just nu"
    elif age_minutes == 1:
        age_text = "1 min sedan"
    else:
        age_text = (
            f"{age_minutes} min sedan"
        )

    embed = {
        "title": (
            f"{prefix} "
            f"{item['title']}"
        ),
        "url": item["url"],
        "description": description,
        "color": discord_color(item),
        "fields": [
            {
                "name": "Källa",
                "value": item[
                    "source"
                ],
                "inline": True,
            },
            {
                "name": "Publicerad",
                "value": age_text,
                "inline": True,
            },
        ],
        "footer": {
            "text": (
                "Liverpool News Monitor"
            )
        },
    }

    if item["is_transfer"]:
        embed["fields"].append(
            {
                "name": "Kategori",
                "value": "🚨 Transfer",
                "inline": True,
            }
        )

    elif item["is_podcast"]:
        embed["fields"].append(
            {
                "name": "Kategori",
                "value": "🎙️ Podcast",
                "inline": True,
            }
        )

    else:
        embed["fields"].append(
            {
                "name": "Kategori",
                "value": "📰 Liverpool News",
                "inline": True,
            }
        )

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
            f"DISCORD SENT: "
            f"{item['title']}"
        )

        stats["sent"] += 1

        return True

    except Exception as exc:
        print(
            f"Discord error: {exc}"
        )

        stats["errors"] += 1

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
    stats["scanned"] += 1

    title = clean_text(title)
    summary = clean_text(summary)

    url = normalize_url(url)

    if not title or not url:
        return

    # --------------------------------------------------------
    # Hard content block
    # --------------------------------------------------------

    if is_blocked(
        title,
        summary,
    ):
        stats["blocked"] += 1

        print(
            f"BLOCKED: {title}"
        )

        return

    if looks_like_video_only(
        title,
        summary,
        url,
    ):
        stats["bad_type"] += 1

        print(
            f"VIDEO/OTHER SKIPPED: {title}"
        )

        return

    if not looks_like_article_url(
        url
    ):
        stats["bad_type"] += 1
        return

    # --------------------------------------------------------
    # Liverpool filter
    # --------------------------------------------------------

    if not is_liverpool_article(
        title,
        summary,
        source_type,
    ):
        stats["not_liverpool"] += 1

        print(
            f"NOT LIVERPOOL: {title}"
        )

        return

    # --------------------------------------------------------
    # Opponent-only filter
    # --------------------------------------------------------

    if is_opponent_only_article(
        title,
        summary,
    ):
        stats["opponent_only"] += 1

        print(
            f"OPPONENT ONLY: {title}"
        )

        return

    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    if not published_dt:
        print(
            f"No RSS date, checking article: "
            f"{title}"
        )

        data = get_article_data(
            url
        )

        published_dt = data[
            "published_dt"
        ]

        if not summary:
            summary = data[
                "summary"
            ]

    if not published_dt:
        stats["no_date"] += 1

        print(
            f"NO DATE: {source} | {title}"
        )

        return

    # --------------------------------------------------------
    # Podcast
    # --------------------------------------------------------

    podcast = is_podcast(
        title,
        summary,
    )

    if podcast:
        if (
            source
            not in PRIORITY_SOURCES
        ):
            stats[
                "podcast_blocked"
            ] += 1

            print(
                "PODCAST SKIPPED - "
                "not priority source: "
                f"{title}"
            )

            return

    # --------------------------------------------------------
    # Freshness
    # --------------------------------------------------------

    if not is_fresh(
        published_dt,
        title,
        source,
        summary,
    ):
        stats["old"] += 1

        now = datetime.now(
            timezone.utc
        )

        age = (
            now - published_dt
        ).total_seconds() / 60

        print(
            f"OLD: {title} "
            f"| age={age:.1f} min"
        )

        return

    # --------------------------------------------------------
    # Duplicate
    # --------------------------------------------------------

    if already_seen(
        conn,
        url,
    ):
        stats["duplicates"] += 1

        print(
            f"DUPLICATE: {title}"
        )

        return

    # --------------------------------------------------------
    # Build item
    # --------------------------------------------------------

    transfer = is_transfer_news(
        title,
        summary,
    )

    priority = (
        source in PRIORITY_SOURCES
    )

    item = {
        "title": title,
        "url": url,
        "source": source,
        "summary": summary,
        "published_at": published_dt,
        "is_transfer": transfer,
        "is_podcast": podcast,
        "priority": priority,
    }

    # --------------------------------------------------------
    # Log
    # --------------------------------------------------------

    now = datetime.now(
        timezone.utc
    )

    age_minutes = max(
        0,
        (
            now - published_dt
        ).total_seconds() / 60,
    )

    print("")
    print(
        "================================"
    )
    print(
        "FRESH ARTICLE FOUND"
    )
    print(
        "================================"
    )
    print(
        f"Source: {source}"
    )
    print(
        f"Title: {title}"
    )
    print(
        f"Published: "
        f"{published_dt.isoformat()}"
    )
    print(
        f"Age: "
        f"{age_minutes:.1f} minutes"
    )

    if priority:
        print(
            "Priority: YES"
        )

    if transfer:
        print(
            "Type: TRANSFER"
        )
    elif podcast:
        print(
            "Type: PRIORITY PODCAST"
        )
    else:
        print(
            "Type: NORMAL NEWS"
        )

    print(
        "Status: FRESH"
    )
    print(
        "================================"
    )
    print("")

    # --------------------------------------------------------
    # Discord
    # --------------------------------------------------------

    if send_to_discord(item):
        mark_seen(
            conn,
            item,
        )


# ============================================================
# RSS SCANNER
# ============================================================

def scan_rss(
    conn,
    feed,
):
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
            f"RSS ERROR: "
            f"{source} -> {exc}"
        )

        stats["errors"] += 1

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

def find_article_links(
    soup,
):
    """
    First look for actual <article> elements.
    This avoids many navigation links.
    """

    links = []

    articles = soup.find_all(
        "article"
    )

    for article in articles:
        headline = None

        for heading_tag in [
            "h1",
            "h2",
            "h3",
        ]:
            headline = article.find(
                heading_tag
            )

            if headline:
                break

        if headline:
            anchor = headline.find(
                "a",
                href=True,
            )

            if anchor:
                links.append(
                    (
                        anchor.get_text(
                            " ",
                            strip=True,
                        ),
                        anchor.get(
                            "href"
                        ),
                        article,
                    )
                )

                continue

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

            if not title:
                continue

            if len(title) < 25:
                continue

            links.append(
                (
                    title,
                    href,
                    article,
                )
            )

    # Fallback for sites without <article>.
    if not links:
        for heading in soup.find_all(
            ["h1", "h2", "h3"]
        ):
            anchor = heading.find(
                "a",
                href=True,
            )

            if not anchor:
                continue

            title = anchor.get_text(
                " ",
                strip=True,
            )

            href = anchor.get(
                "href"
            )

            if len(title) < 25:
                continue

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

        stats["errors"] += 1

        return

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    links = find_article_links(
        soup
    )

    seen_urls = set()

    for title, href, element in links:
        title = clean_text(title)

        if is_navigation_link(
            title,
            href,
        ):
            continue

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

        # Avoid the page linking to itself.
        if (
            article_url.rstrip("/")
            == page_url.rstrip("/")
        ):
            continue

        published_dt = None
        summary = ""

        # First try metadata inside the article/card.
        try:
            published_dt = (
                extract_page_date(
                    element
                )
            )
        except Exception:
            published_dt = None

        # If card does not contain date/summary,
        # inspect the actual article.
        if not published_dt:
            data = get_article_data(
                article_url
            )

            published_dt = data[
                "published_dt"
            ]

            summary = data[
                "summary"
            ]

        process_article(
            conn=conn,
            title=title,
            url=article_url,
            source=source_name,
            summary=summary,
            published_dt=published_dt,
            source_type=source_type,
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
        "Liverpool News Monitor 4.0"
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
    # Close database
    # --------------------------------------------------------

    conn.close()

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("")
    print(
        "================================"
    )
    print(
        "SCAN COMPLETE"
    )
    print(
        "================================"
    )

    print(
        f"Scanned:          "
        f"{stats['scanned']}"
    )

    print(
        f"Discord sent:     "
        f"{stats['sent']}"
    )

    print(
        f"Duplicates:       "
        f"{stats['duplicates']}"
    )

    print(
        f"Too old:          "
        f"{stats['old']}"
    )

    print(
        f"No date:          "
        f"{stats['no_date']}"
    )

    print(
        f"Not Liverpool:    "
        f"{stats['not_liverpool']}"
    )

    print(
        f"Blocked content:  "
        f"{stats['blocked']}"
    )

    print(
        f"Bad content type: "
        f"{stats['bad_type']}"
    )

    print(
        f"Opponent only:    "
        f"{stats['opponent_only']}"
    )

    print(
        f"Podcasts blocked:  "
        f"{stats['podcast_blocked']}"
    )

    print(
        f"Errors:           "
        f"{stats['errors']}"
    )

    print(
        "================================"
    )


if __name__ == "__main__":
    main()
