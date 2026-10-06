import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.x.com/2"
SEARCH_URL = f"{API_BASE}/tweets/search/recent"
REPOST_URL = f"{API_BASE}/users/{{user_id}}/retweets"

BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
USER_ACCESS_TOKEN = os.getenv("X_USER_ACCESS_TOKEN", "").strip()
USER_ID = os.getenv("X_USER_ID", "").strip()
CONFIG_FILE = os.getenv("BOT_CONFIG", "config.json")
DB_FILE = os.getenv("BOT_DB", "bot.sqlite3")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("xbot")


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)

    defaults = {
        "keywords": [], "exclude_words": [], "exclude_users": [],
        "language": "", "interval_minutes": 10, "max_posts_per_cycle": 3,
        "min_likes": 0, "min_retweets": 0, "post_max_age_minutes": 15,
        "dry_run": True,
    }
    for key, value in defaults.items():
        cfg.setdefault(key, value)

    if not cfg["keywords"]:
        raise ValueError("config.json muss mindestens ein Suchwort enthalten.")
    if int(cfg["max_posts_per_cycle"]) < 1:
        raise ValueError("max_posts_per_cycle muss mindestens 1 sein.")
    return cfg


def db_init():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        """CREATE TABLE IF NOT EXISTS processed_posts (
            post_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            text TEXT
        )"""
    )
    con.commit()
    return con


def was_processed(con, post_id):
    return con.execute(
        "SELECT 1 FROM processed_posts WHERE post_id = ?", (post_id,)
    ).fetchone() is not None


def mark_processed(con, post_id, action, text):
    con.execute(
        """INSERT OR REPLACE INTO processed_posts
           (post_id, action, processed_at, text)
           VALUES (?, ?, ?, ?)""",
        (post_id, action, datetime.now(timezone.utc).isoformat(), text[:2000]),
    )
    con.commit()


def build_query(cfg):
    terms = [str(x).strip() for x in cfg["keywords"] if str(x).strip()]
    query = "(" + " OR ".join(terms) + ") -is:retweet -is:reply"

    if cfg.get("language"):
        query += f" lang:{cfg['language']}"

    for username in cfg.get("exclude_users", []):
        username = str(username).strip().lstrip("@")
        if username:
            query += f" -from:{username}"

    return query


def api_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "X-Auto-Repost-Bot/1.1",
    }


def search_posts(cfg):
    if not BEARER_TOKEN:
        raise RuntimeError("X_BEARER_TOKEN fehlt in .env")

    params = {
        "query": build_query(cfg),
        "max_results": min(max(int(cfg["max_posts_per_cycle"]) * 5, 10), 100),
        "tweet.fields": "id,text,author_id,created_at,public_metrics",
    }

    response = requests.get(
        SEARCH_URL, headers=api_headers(BEARER_TOKEN), params=params, timeout=30
    )

    if response.status_code == 429:
        raise RuntimeError("X API Rate Limit erreicht (429).")
    response.raise_for_status()

    payload = response.json()
    if "errors" in payload and not payload.get("data"):
        raise RuntimeError(f"X API Fehler: {payload['errors']}")
    return payload.get("data", [])


def post_is_eligible(post, cfg):
    text = post.get("text", "")
    lowered = text.casefold()

    if USER_ID and post.get("author_id") == USER_ID:
        return False

    if any(str(word).casefold() in lowered for word in cfg.get("exclude_words", [])):
        return False

    metrics = post.get("public_metrics") or {}
    if int(metrics.get("like_count", 0)) < int(cfg.get("min_likes", 0)):
        return False
    if int(metrics.get("retweet_count", 0)) < int(cfg.get("min_retweets", 0)):
        return False

    created = post.get("created_at")
    if created:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - created_at).total_seconds() / 60
        if age < 0 or age > float(cfg.get("post_max_age_minutes", 15)):
            return False

    return True


def repost(post_id):
    if not USER_ACCESS_TOKEN:
        raise RuntimeError("X_USER_ACCESS_TOKEN fehlt in .env")
    if not USER_ID:
        raise RuntimeError("X_USER_ID fehlt in .env")

    response = requests.post(
        REPOST_URL.format(user_id=USER_ID),
        headers=api_headers(USER_ACCESS_TOKEN),
        json={"tweet_id": str(post_id)},
        timeout=30,
    )

    if response.status_code == 429:
        raise RuntimeError("X API Rate Limit beim Repost erreicht (429).")
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"X verweigert den Repost ({response.status_code}). "
            "Prüfe User Access Token und Schreibrechte der App."
        )
    response.raise_for_status()

    data = response.json().get("data", {})
    if data.get("retweeted") is not True:
        raise RuntimeError(f"Repost wurde von X nicht bestätigt: {response.text[:500]}")


def run_cycle(con, cfg):
    posts = search_posts(cfg)
    candidates = [
        post for post in posts
        if not was_processed(con, str(post["id"])) and post_is_eligible(post, cfg)
    ]

    candidates.sort(
        key=lambda post: (post.get("created_at", ""), str(post["id"])),
        reverse=True,
    )

    for post in candidates[: int(cfg["max_posts_per_cycle"])]:
        post_id = str(post["id"])
        text = post.get("text", "").replace("\n", " ")

        if cfg.get("dry_run", True):
            log.info("[DRY RUN] Würde %s repostieren: %s", post_id, text[:180])
            continue

        try:
            repost(post_id)
        except Exception as exc:
            log.error("Repost %s fehlgeschlagen: %s", post_id, exc)
            continue

        mark_processed(con, post_id, "reposted", text)
        log.info("Repost erfolgreich: %s", post_id)


def main():
    cfg = load_config()
    con = db_init()

    log.info("Bot gestartet.")
    log.info("Suche: %s", build_query(cfg))
    log.info("Dry Run: %s", cfg["dry_run"])

    while True:
        try:
            run_cycle(con, cfg)
        except KeyboardInterrupt:
            log.info("Bot beendet.")
            break
        except Exception as exc:
            log.exception("Zyklus fehlgeschlagen: %s", exc)

        time.sleep(max(int(cfg["interval_minutes"]) * 60, 60))


if __name__ == "__main__":
    main()
