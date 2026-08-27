#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

from browser_search import search_web
from config import FETCH_PYTHON, FETCH_SCRIPT

MAX_CANDIDATE_URLS = 8
MAX_FETCHED_PAGES = 4
FETCH_TIMEOUT_SECONDS = 14
TOTAL_FETCH_BUDGET_SECONDS = 38
MIN_SECONDS_BETWEEN_FETCHES = 1.5
MAX_CHARS_PER_PAGE_FOR_MODEL = 4200
MIN_READABLE_CHARS = 250

SERIES_SIGNAL_WEIGHTS = {
    "series": 12,
    "series order": 14,
    "reading order": 14,
    "book": 7,
    "books": 6,
    "book #": 10,
    "book no": 10,
    "volume": 8,
    "vol.": 8,
    "bibliography": 9,
    "sequence": 9,
    "installment": 8,
    "mystery series": 10,
}


def _domain_key(url):
    """Return a coarse domain key for diversity, without external libraries."""
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host

    # Handle a few common country-code second-level patterns generically.
    if (
        len(parts) >= 3
        and parts[-2] in {"co", "com", "org", "net", "ac"}
        and len(parts[-1]) == 2
    ):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _series_candidate_score(candidate, target_title=None):
    """Rank already-returned Google results for likely series-order evidence."""
    url = str(candidate.get("url") or "")
    text = str(candidate.get("text") or "")
    haystack = f"{text} {url}".casefold()

    score = 0

    # On a targeted fallback, an exact-title result is much more useful than
    # a generic author/bibliography result. A title followed by a named
    # parenthetical such as "(Frank Clevenger)" is especially valuable as a
    # series-identity lead. This only affects ranking; it is never accepted as
    # final evidence by itself.
    target_title = str(target_title or "").strip()
    if target_title:
        title_cf = target_title.casefold()
        text_cf = text.casefold()
        decoded_url_cf = urllib.parse.unquote(url).casefold()

        if title_cf in text_cf:
            score += 45
        if title_cf in decoded_url_cf:
            score += 18

        if title_cf in text_cf:
            pos = text_cf.find(title_cf)
            area = text[pos : pos + len(target_title) + 140]
            if re.search(r"\([^()]{3,80}\)", area):
                score += 35

    for signal, weight in SERIES_SIGNAL_WEIGHTS.items():
        if signal in haystack:
            score += weight

    # Extra weight for explicit numeric/ordering patterns in snippets/URLs.
    if re.search(r"(?:book|volume|vol\.?)\s*(?:#|no\.?|number)?\s*\d+", haystack):
        score += 16
    if re.search(r"#\s*\d+", haystack):
        score += 12
    if re.search(r"\b\d+(?:st|nd|rd|th)\s+(?:book|novel|installment)\b", haystack):
        score += 14

    # Weak retail/listing signals are still useful, but should not crowd out
    # diverse bibliography/author/publisher results during fallback.
    host = _domain_key(url)
    if host in {"ebay.com"}:
        score -= 5

    return score


def _rank_series_candidates(candidates, target_title=None):
    """
    Re-rank only a targeted fallback result set and prefer domain diversity.
    This does not alter the normal primary-search ordering.
    """
    indexed = list(enumerate(candidates or []))
    indexed.sort(
        key=lambda pair: (
            -_series_candidate_score(
                pair[1],
                target_title=target_title,
            ),
            pair[0],  # preserve Google rank for equal scores
        )
    )

    ranked = [item for _, item in indexed]

    # First pass: one result per coarse domain.
    diverse = []
    deferred = []
    seen_domains = set()

    for candidate in ranked:
        domain = _domain_key(candidate.get("url"))
        if domain and domain not in seen_domains:
            seen_domains.add(domain)
            diverse.append(candidate)
        else:
            deferred.append(candidate)

    # Keep duplicates only after every available distinct domain had a chance.
    return diverse + deferred


def _safe_name(index, url):
    host = urllib.parse.urlsplit(url).hostname or "page"
    host = re.sub(r"[^a-zA-Z0-9._-]+", "-", host).strip("-")
    return f"{index:02d}-{host[:60]}.txt"


def _parse_fetch_json(stdout):
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


MODEL_SIGNAL_TERMS = (
    "series",
    "book ",
    "book #",
    "volume",
    "vol.",
    "reading order",
    "bibliography",
    "standalone",
    "stand-alone",
    "isbn",
    "publisher",
    "published",
    "publication",
)


def _focused_model_text(text, title=None, author=None):
    """
    Keep full fetched text on disk, but send Gemma a compact deterministic
    evidence digest made from title/author/series/metadata neighborhoods.
    No semantic judgment happens here.
    """
    text = str(text or "").replace("\x00", "")
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()

    if len(text) <= MAX_CHARS_PER_PAGE_FOR_MODEL:
        return text

    lower = text.casefold()
    needles = []

    for value in (title, author):
        value = str(value or "").strip()
        if value:
            needles.append(value.casefold())

    needles.extend(MODEL_SIGNAL_TERMS)

    centers = set()
    for needle in needles:
        start = 0
        hits = 0
        while hits < 5:
            pos = lower.find(needle, start)
            if pos < 0:
                break
            centers.add(pos)
            start = pos + max(1, len(needle))
            hits += 1

    # Always retain some page-title/header context.
    windows = [(0, min(len(text), 900))]
    for center in sorted(centers):
        windows.append(
            (
                max(0, center - 450),
                min(len(text), center + 850),
            )
        )

    # Merge overlapping windows.
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 80:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    pieces = []
    used = 0
    for start, end in merged:
        piece = text[start:end].strip()
        if not piece:
            continue

        remaining = MAX_CHARS_PER_PAGE_FOR_MODEL - used
        if remaining <= 0:
            break

        if len(piece) > remaining:
            piece = piece[:remaining]

        pieces.append(piece)
        used += len(piece) + 30

    if not pieces:
        return text[:MAX_CHARS_PER_PAGE_FOR_MODEL]

    return "\n\n[...]\n\n".join(pieces)[:MAX_CHARS_PER_PAGE_FOR_MODEL]


def gather_live_evidence(
    title,
    author,
    job_tmp_dir,
    max_candidate_urls=MAX_CANDIDATE_URLS,
    max_fetched_pages=MAX_FETCHED_PAGES,
    total_fetch_budget_seconds=TOTAL_FETCH_BUDGET_SECONDS,
    candidate_rank_mode=None,
    ranking_title=None,
):
    job_tmp_dir = Path(job_tmp_dir)
    job_tmp_dir.mkdir(parents=True, exist_ok=True)

    overall_started = time.monotonic()
    search_started = time.monotonic()
    search = search_web(title, author)
    search_seconds = time.monotonic() - search_started

    if search.get("status") != "OK":
        return {
            "status": "SEARCH_ERROR",
            "query": search.get("query"),
            "search_cache_hit": bool(search.get("cache_hit")),
            "search": search,
            "candidates": [],
            "fetched_pages": [],
            "fetch_attempts": [],
            "notes": (
                "Live browser discovery failed. This is infrastructure "
                "failure, not evidence that the book is unfound."
            ),
            "timings": {
                "search_seconds": round(search_seconds, 3),
                "fetch_seconds": 0.0,
                "evidence_total_seconds": round(
                    time.monotonic() - overall_started, 3
                ),
            },
        }

    all_candidates = list(search.get("results") or [])
    if candidate_rank_mode == "series_fallback":
        all_candidates = _rank_series_candidates(
            all_candidates,
            target_title=ranking_title or title,
        )

    candidates = all_candidates[:max_candidate_urls]
    fetched_pages = []
    attempts = []
    started = time.monotonic()
    fetch_started = time.monotonic()
    last_fetch_started = 0.0

    for index, candidate in enumerate(candidates, start=1):
        if len(fetched_pages) >= max_fetched_pages:
            break

        elapsed = time.monotonic() - started
        if elapsed >= total_fetch_budget_seconds:
            break

        url = str(candidate.get("url") or "").strip()
        if not url:
            continue

        since_last = time.monotonic() - last_fetch_started
        if last_fetch_started and since_last < MIN_SECONDS_BETWEEN_FETCHES:
            time.sleep(MIN_SECONDS_BETWEEN_FETCHES - since_last)

        output = job_tmp_dir / _safe_name(index, url)
        cmd = [
            FETCH_PYTHON,
            FETCH_SCRIPT,
            url,
            str(output),
        ]

        last_fetch_started = time.monotonic()
        remaining = max(
            1.0,
            total_fetch_budget_seconds - (time.monotonic() - started),
        )
        timeout = min(FETCH_TIMEOUT_SECONDS, remaining)

        try:
            run = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            fetch_obj = _parse_fetch_json(run.stdout)
            attempt = {
                "url": url,
                "search_text": str(candidate.get("text") or ""),
                "returncode": run.returncode,
                "fetch_result": fetch_obj,
            }

            if (
                run.returncode == 0
                and fetch_obj
                and fetch_obj.get("ok") is True
                and output.exists()
            ):
                raw_text = output.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                readable = raw_text.strip()

                if len(readable) >= MIN_READABLE_CHARS:
                    fetched_pages.append(
                        {
                            "url": url,
                            "search_text": str(
                                candidate.get("text") or ""
                            ),
                            "chars": len(readable),
                            "text": _focused_model_text(
                                readable,
                                title=title,
                                author=author,
                            ),
                            "local_path": str(output),
                        }
                    )
                    attempt["status"] = "FETCHED"
                else:
                    attempt["status"] = "TOO_THIN"
                    attempt["chars"] = len(readable)
            else:
                attempt["status"] = "FETCH_FAILED"
                attempt["stderr_tail"] = (run.stderr or "")[-1000:]

            attempts.append(attempt)

        except subprocess.TimeoutExpired:
            attempts.append(
                {
                    "url": url,
                    "search_text": str(candidate.get("text") or ""),
                    "status": "TIMEOUT",
                    "timeout_seconds": timeout,
                }
            )
        except Exception as exc:
            attempts.append(
                {
                    "url": url,
                    "search_text": str(candidate.get("text") or ""),
                    "status": "ERROR",
                    "error": repr(exc),
                }
            )

    if not fetched_pages:
        return {
            "status": "FETCH_ERROR",
            "query": search.get("query"),
            "search_cache_hit": bool(search.get("cache_hit")),
            "search": {
                "searched_at": search.get("searched_at"),
                "notes": search.get("notes"),
            },
            "candidates": candidates,
            "fetched_pages": [],
            "fetch_attempts": attempts,
            "notes": (
                "Live Google discovery returned candidate URLs, but no "
                "candidate page was successfully fetched into readable text "
                "within the bounded fetch budget. This is treated as an "
                "infrastructure/evidence acquisition error, not UNFOUND."
            ),
            "timings": {
                "search_seconds": round(search_seconds, 3),
                "fetch_seconds": round(
                    time.monotonic() - fetch_started, 3
                ),
                "evidence_total_seconds": round(
                    time.monotonic() - overall_started, 3
                ),
            },
        }

    return {
        "status": "OK",
        "query": search.get("query"),
        "search_cache_hit": bool(search.get("cache_hit")),
        "search": {
            "searched_at": search.get("searched_at"),
            "notes": search.get("notes"),
        },
        "candidates": candidates,
        "fetched_pages": fetched_pages,
        "fetch_attempts": attempts,
        "notes": (
            f"Live Google discovery supplied {len(candidates)} candidate "
            f"URLs; {len(fetched_pages)} readable pages were fetched."
        ),
        "timings": {
            "search_seconds": round(search_seconds, 3),
            "fetch_seconds": round(
                time.monotonic() - fetch_started, 3
            ),
            "evidence_total_seconds": round(
                time.monotonic() - overall_started, 3
            ),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--job-dir", required=True)
    args = parser.parse_args()

    result = gather_live_evidence(
        args.title,
        args.author,
        args.job_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
