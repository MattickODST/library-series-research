#!/usr/bin/env python3
import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import BROWSER_CACHE_DB, CAMOFOX_URL

CACHE_DB = BROWSER_CACHE_DB

USER_ID = "iris-library"
MAX_GOOGLE_LINKS = 100
MAX_EXTERNAL_RESULTS = 10

# Intentionally conservative. The batch is sequential, and roughly
# <= 1 minute/book is acceptable. This is only the minimum spacing
# between LIVE Google searches; cached searches do not wait.
MIN_LIVE_SEARCH_INTERVAL_SECONDS = 7.0
SERP_SETTLE_SECONDS = 2.0
LINK_EXTRACTION_ATTEMPTS = 4
LINK_EXTRACTION_RETRY_SECONDS = 2.5

EXCLUDED_HOST_SUFFIXES = (
    "google.com",
    "googleusercontent.com",
    "gstatic.com",
    "googleadservices.com",
    "doubleclick.net",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CACHE_DB, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS search_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT NOT NULL,
            query TEXT NOT NULL,
            status TEXT NOT NULL,
            detail_json TEXT,
            attempted_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.commit()
    return db


def canonical_query(title, author):
    return " ".join(
        x.strip() for x in (str(title or ""), str(author or "")) if x.strip()
    )


def cache_key_for(query):
    return query.casefold().strip()


def _request_json(method, path, body=None, timeout=20):
    url = CAMOFOX_URL.rstrip("/") + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _record_attempt(db, key, query, status, detail):
    db.execute(
        """
        INSERT INTO search_attempts
            (cache_key, query, status, detail_json, attempted_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            key,
            query,
            status,
            json.dumps(detail, ensure_ascii=False),
            now_iso(),
        ),
    )
    db.commit()


def _cached_result(db, key):
    row = db.execute(
        "SELECT result_json FROM search_cache WHERE cache_key=?",
        (key,),
    ).fetchone()
    if not row:
        return None
    try:
        obj = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    obj["cache_hit"] = True
    return obj


def _wait_for_search_slot(db):
    row = db.execute(
        "SELECT value FROM meta WHERE key='last_live_search_epoch'"
    ).fetchone()
    if row:
        try:
            last = float(row[0])
        except (TypeError, ValueError):
            last = 0.0
        wait_for = MIN_LIVE_SEARCH_INTERVAL_SECONDS - (time.time() - last)
        if wait_for > 0:
            time.sleep(wait_for)

    # Reserve the slot before issuing the request so a crash still prevents
    # an immediate burst on restart.
    db.execute(
        """
        INSERT INTO meta(key, value)
        VALUES('last_live_search_epoch', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(time.time()),),
    )
    db.commit()


def _external_results(links):
    out = []
    seen = set()

    for item in links or []:
        if not isinstance(item, dict):
            continue

        raw_url = str(item.get("url") or "").strip()
        if not raw_url.startswith(("http://", "https://")):
            continue

        try:
            parsed = urllib.parse.urlsplit(raw_url)
        except ValueError:
            continue

        host = (parsed.hostname or "").casefold()
        if not host:
            continue

        if any(
            host == suffix or host.endswith("." + suffix)
            for suffix in EXCLUDED_HOST_SUFFIXES
        ):
            continue

        # Remove fragments only. Preserve query strings because some publisher
        # and catalog pages rely on them.
        clean_url = urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.query,
                "",
            )
        )

        dedupe_key = clean_url.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        out.append(
            {
                "url": clean_url,
                "text": str(item.get("text") or "").strip(),
            }
        )

        if len(out) >= MAX_EXTERNAL_RESULTS:
            break

    return out


def search_web(title, author, refresh=False):
    query = canonical_query(title, author)
    if not query:
        return {
            "status": "ERROR",
            "query": query,
            "cache_hit": False,
            "results": [],
            "notes": "Title/author query was empty.",
        }

    key = cache_key_for(query)
    db = connect()

    if not refresh:
        cached = _cached_result(db, key)
        if cached is not None:
            db.close()
            return cached

    try:
        health = _request_json("GET", "/health", timeout=10)
        if not health.get("ok"):
            detail = {"health": health}
            _record_attempt(db, key, query, "BACKEND_ERROR", detail)
            db.close()
            return {
                "status": "BACKEND_ERROR",
                "query": query,
                "cache_hit": False,
                "results": [],
                "notes": "Camofox server health check failed.",
                "diagnostics": detail,
            }

        _wait_for_search_slot(db)

        session_key = "book-" + uuid.uuid4().hex[:12]
        created = _request_json(
            "POST",
            "/tabs",
            {
                "userId": USER_ID,
                "sessionKey": session_key,
                "url": "https://www.google.com",
            },
            timeout=20,
        )
        tab_id = created.get("tabId")
        if not tab_id:
            raise RuntimeError("Camofox did not return tabId")

        try:
            nav = _request_json(
                "POST",
                f"/tabs/{urllib.parse.quote(str(tab_id))}/navigate",
                {
                    "userId": USER_ID,
                    "macro": "@google_search",
                    "query": query,
                },
                timeout=25,
            )
            if not nav.get("ok"):
                raise RuntimeError(
                    "Google navigation did not return ok=true: "
                    + repr(nav)
                )

            # Google can render its navigation/filter links before the
            # organic result anchors appear.  Do not issue a new search just
            # because the first DOM snapshot is early: re-read links from the
            # SAME SERP a few times.
            results = []
            link_snapshots = []
            links_obj = {"links": []}

            for extraction_attempt in range(
                1, LINK_EXTRACTION_ATTEMPTS + 1
            ):
                if extraction_attempt == 1:
                    time.sleep(SERP_SETTLE_SECONDS)
                else:
                    time.sleep(LINK_EXTRACTION_RETRY_SECONDS)

                links_obj = _request_json(
                    "GET",
                    (
                        f"/tabs/{urllib.parse.quote(str(tab_id))}/links"
                        f"?userId={urllib.parse.quote(USER_ID)}"
                        f"&limit={MAX_GOOGLE_LINKS}"
                    ),
                    timeout=25,
                )
                raw_links = links_obj.get("links") or []
                results = _external_results(raw_links)
                link_snapshots.append(
                    {
                        "attempt": extraction_attempt,
                        "total_links": len(raw_links),
                        "external_results": len(results),
                    }
                )
                if results:
                    break

        finally:
            try:
                _request_json(
                    "DELETE",
                    (
                        f"/tabs/{urllib.parse.quote(str(tab_id))}"
                        f"?userId={urllib.parse.quote(USER_ID)}"
                    ),
                    timeout=10,
                )
            except Exception:
                pass

        if not results:
            detail = {
                "navigation": nav,
                "link_count": len(links_obj.get("links") or []),
                "link_snapshots": link_snapshots,
            }
            _record_attempt(db, key, query, "NO_EXTERNAL_RESULTS", detail)
            db.close()
            return {
                "status": "NO_EXTERNAL_RESULTS",
                "query": query,
                "cache_hit": False,
                "results": [],
                "notes": (
                    "Google loaded, but no external result URLs were extracted. "
                    "This is not a book-level UNFOUND conclusion."
                ),
                "diagnostics": detail,
            }

        result = {
            "status": "OK",
            "query": query,
            "cache_hit": False,
            "searched_at": now_iso(),
            "results": results,
            "notes": (
                f"Live Google search via Camofox returned "
                f"{len(results)} unique external URLs."
            ),
        }

        encoded = json.dumps(result, ensure_ascii=False)
        db.execute(
            """
            INSERT INTO search_cache
                (cache_key, query, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                query=excluded.query,
                result_json=excluded.result_json,
                updated_at=excluded.updated_at
            """,
            (key, query, encoded, now_iso(), now_iso()),
        )
        db.commit()
        _record_attempt(
            db,
            key,
            query,
            "OK",
            {"result_count": len(results)},
        )
        db.close()
        return result

    except Exception as exc:
        detail = {"error": repr(exc)}
        try:
            _record_attempt(db, key, query, "BACKEND_ERROR", detail)
        finally:
            db.close()

        return {
            "status": "BACKEND_ERROR",
            "query": query,
            "cache_hit": False,
            "results": [],
            "notes": (
                "Live browser search failed. This is infrastructure failure, "
                "not evidence that the book is unfound."
            ),
            "diagnostics": detail,
        }


def stats():
    db = connect()
    cache_count = db.execute(
        "SELECT COUNT(*) FROM search_cache"
    ).fetchone()[0]
    attempt_count = db.execute(
        "SELECT COUNT(*) FROM search_attempts"
    ).fetchone()[0]
    status_rows = db.execute(
        """
        SELECT status, COUNT(*)
        FROM search_attempts
        GROUP BY status
        ORDER BY status
        """
    ).fetchall()
    db.close()
    return {
        "cache_db": str(CACHE_DB),
        "cached_searches": cache_count,
        "attempts": attempt_count,
        "attempt_statuses": dict(status_rows),
        "minimum_live_search_interval_seconds": (
            MIN_LIVE_SEARCH_INTERVAL_SECONDS
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search")
    p_search.add_argument("--title", required=True)
    p_search.add_argument("--author", required=True)
    p_search.add_argument("--refresh", action="store_true")

    sub.add_parser("stats")

    args = parser.parse_args()

    if args.command == "search":
        result = search_web(
            args.title,
            args.author,
            refresh=args.refresh,
        )
    else:
        result = stats()

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
