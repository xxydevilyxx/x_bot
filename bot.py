import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("xbot")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    cfg.setdefault("keywords", [])
    cfg.setdefault("exclude_words", [])
    cfg.setdefault("exclude_users", [])
    cfg.setdefault("language", "")
    cfg.setdefault("interval_minutes", 10)
    cfg.setdefault("max_posts_per_cycle", 3)
    cfg.setdefault("min_likes", 0)
    cfg.setdefault("min_retweets", 0)
    cfg.setdefault("post_max_age_minutes", 15)
    cfg.setdefault("dry_run", False)

    if not cfg["keywords"]:
        raise ValueError("config.json muss mindestens ein Suchwort enthalten.")
    return cfg


def db_init():
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS processed_posts (
            post_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            text TEXT
        )
    """)
    con.commit()
    return con


def was_processed(con, post_id):
    row = con.execute(
        "SELECT 1 FROM processed_posts WHERE post_id = ?", (post_id,)
    ).fetchone()
    return row is not None


def mark_processed(con, post_id, action, text):
    con.execute(
        """INSERT OR REPLACE INTO processed_posts
           (post_id, action, processed_at, text)
           VALUES (?, ?, ?, ?)""",
        (post_id, action, datetime.now(timezone.utc).isoformat(), text[:2000]),
    )
    con.commit()


def build_query(cfg):
    # Exact phrases can be supplied by putting quotes into config.json.
    terms = []
    for word in cfg["keywords"]:
        word = str(word).strip()
        if word:
            terms.append(word)

    query = "(" + " OR ".join(terms) + ")"
    query += " -is:retweet"
    query += " -is:reply"

    if cfg.get("language"):
        query += f" lang:{cfg['language']}"

    for user in cfg.get("exclude_users", []):
        user = str(user).strip().lstrip("@")
        if user:
            query += f" -from:{user}"

    return query


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "X-Auto-Repost-Bot/1.0",
    }


def search_posts(cfg):
    if not BEARER_TOKEN:
        raise RuntimeError("X_BEARER_TOKEN fehlt in .env")

    params = {
        "query": build_query(cfg),
        "max_results": min(max(int(cfg["max_posts_per_cycle"]) * 3, 10), 100),
        "tweet.fields": "id,text,author_id,created_at,public_metrics",
        "expansions": "author_id",
        "user.fields": "id,username,protected",
    }

    r = requests.get(
        SEARCH_URL,
        headers=headers(BEARER_TOKEN),
        params=params,
        timeout=30,
    )

    if r.status_code == 429:
        raise RuntimeError("X API Rate Limit (429). Warte bis zum nächsten Zyklus.")
    r.raise_for_status()

    return r.json().get("data", [])


def post_is_eligible(post, cfg):
    text = post.get("text", "")
    low = text.casefold()

    for word in cfg.get("exclude_words", []):
        if str(word).casefold() in low:
            return False

    if USER_ID and post.get("author_id") == USER_ID:
        return False

    metrics = post.get("public_metrics") or {}
    if int(metrics.get("like_count", 0)) < int(cfg.get("min_likes", 0)):
        return False
    if int(metrics.get("retweet_count", 0)) < int(cfg.get("min_retweets", 0)):
        return False

    created = post.get("created_at")
    if created:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - created_dt).total_seconds() / 60
        if age_minutes > float(cfg.get("post_max_age_minutes", 15)):
            return False

    return True


def repost(post_id):
    if not USER_ACCESS_TOKEN:
        raise RuntimeError("X_USER_ACCESS_TOKEN fehlt in .env")
    if not USER_ID:
        raise RuntimeError("X_USER_ID fehlt in .env")

    url = REPOST_URL.format(user_id=quote(USER_ID))
    r = requests.post(
        url,
        headers=headers(USER_ACCESS_TOKEN),
        json={"tweet_id": str(post_id)},
        timeout=30,
    )

    if r.status_code == 429:
        raise RuntimeError("X API Rate Limit (429) beim Repost.")
    if r.status_code == 403:
        # This is commonly returned when the app/user is not allowed to write.
        raise RuntimeError(f"X verweigert den Repost (403): {r.text[:500]}")
    r.raise_for_status()

    data = r.json().get("data", {})
    if data.get("retweeted") is False:
        raise RuntimeError(f"X meldet keinen erfolgreichen Repost: {r.text[:500]}")


def run_cycle(con, cfg):
    posts = search_posts(cfg)
    candidates = []

    for post in posts:
        pid = str(post["id"])
        if was_processed(con, pid):
            continue
        if not post_is_eligible(post, cfg):
            continue
        candidates.append(post)

    candidates.sort(
        key=lambda p: (p.get("created_at") or "", p["id"]),
        reverse=True,
    )
    candidates = candidates[: int(cfg["max_posts_per_cycle"])]

    if not candidates:
        log.info("Keine neuen passenden Posts gefunden.")
        return

    for post in candidates:
        pid = str(post["id"])
        text = post.get("text", "").replace("\n", " ")
        if cfg.get("dry_run", False):
            log.info("[DRY RUN] Würde Post %s repostieren: %s", pid, text[:180])
            mark_processed(con, pid, "dry_run", text)
            continue

        try:
            repost(pid)
            mark_processed(con, pid, "reposted", text)
            log.info("Repost erfolgreich: %s", pid)
        except Exception as exc:
            log.error("Repost %s fehlgeschlagen: %s", pid, exc)
            # Do not mark failed posts as processed so a later cycle can retry.


def main():
    cfg = load_config()
    con = db_init()

    log.info("Bot gestartet.")
    log.info("Suche: %s", build_query(cfg))
    log.info("Intervall: %s Minuten", cfg["interval_minutes"])

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
