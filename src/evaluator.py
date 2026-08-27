#!/usr/bin/env python3
"""
Iris Library production evaluator: direct Gemma4:12B via Ollama.

This is a thin adapter around the deterministic research controller.
It preserves the existing deterministic research/search/fallback/validation/
archive controller and replaces only its Hermes model-evaluation hook.

It does not modify researcher.py, evidence.py, or the database itself.
"""

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import researcher as core
from config import OLLAMA_MODEL, OLLAMA_URL

MODEL = OLLAMA_MODEL
NUM_CTX = 16384
THINK = False
TEMPERATURE = 0.0
OLLAMA_TIMEOUT_SECONDS = 60
KEEP_ALIVE = "10m"
ADAPTER_VERSION = "direct-gemma-production-v3-bounded-fallback"
EXPECTED_RESEARCH_ONE_VERSION = "v7.7-camofox-timeout-status"

# Transient browser-discovery failures can occur when Google temporarily
# returns a SERP shell without usable external results. For unattended
# production, pause and retry the SAME book rather than returning a transient
# infrastructure ERROR to the batch database.
BROWSER_RETRY_DELAYS_SECONDS = (60, 180, 300, 600)

def _is_browser_infrastructure_error(result):
    if not isinstance(result, dict) or result.get("status") != "ERROR":
        return False
    text = " ".join(
        str(result.get(k) or "") for k in ("notes", "error", "last_error")
    ).casefold()
    markers = (
        "live browser discovery failed",
        "live browser search failed",
        "infrastructure failure",
        "no external result urls were extracted",
        "no_external_results",
    )
    return any(marker in text for marker in markers)

def _browser_retry_delay(retry_number):
    # retry_number is 1-based. After 10 minutes is reached, remain at a
    # 10-minute cadence until the browser/search path recovers or the process
    # is manually interrupted.
    index = min(max(retry_number, 1) - 1, len(BROWSER_RETRY_DELAYS_SECONDS) - 1)
    return BROWSER_RETRY_DELAYS_SECONDS[index]

# Same structured-output contract used by the successful replay benchmark.
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "author",
        "status",
        "is_series",
        "series_name",
        "series_number",
        "confidence",
        "searches_used",
        "pages_fetched",
        "sources",
        "conflicts",
        "notes",
        "bonus_metadata",
    ],
    "properties": {
        "title": {"type": "string"},
        "author": {"type": "string"},
        "status": {
            "type": "string",
            "enum": [
                "VERIFIED",
                "NOT_SERIES",
                "LIKELY_NOT_SERIES",
                "LIKELY_SERIES",
                "UNFOUND",
                "CONFLICT",
                "TIMED_OUT",
                "ERROR",
            ],
        },
        "is_series": {"type": ["boolean", "null"]},
        "series_name": {"type": ["string", "null"]},
        "series_number": {"type": ["string", "number", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "searches_used": {"type": "integer", "minimum": 0, "maximum": 2},
        "pages_fetched": {"type": "integer", "minimum": 0, "maximum": 4},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_id", "supports", "evidence"],
                "properties": {
                    "source_id": {"type": "string"},
                    "supports": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "conflicts": {
            "type": "array",
            "items": {"type": "string"},
        },
        "notes": {"type": "string"},
        "bonus_metadata": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "total_volumes_in_series",
                "series_status",
                "isbn_13",
                "isbn_10",
                "publisher",
                "original_publication_year",
                "alternate_series_names",
                "series_position_text",
            ],
            "properties": {
                "total_volumes_in_series": {"type": ["integer", "string", "null"]},
                "series_status": {
                    "type": ["string", "null"],
                    "enum": ["ONGOING", "COMPLETE", None],
                },
                "isbn_13": {"type": ["string", "null"]},
                "isbn_10": {"type": ["string", "null"]},
                "publisher": {"type": ["string", "null"]},
                "original_publication_year": {"type": ["integer", "string", "null"]},
                "alternate_series_names": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "series_position_text": {"type": ["string", "null"]},
            },
        },
    },
}


def compatibility_check():
    live_version = getattr(core, "RESEARCH_ONE_VERSION", None)
    if live_version != EXPECTED_RESEARCH_ONE_VERSION:
        raise RuntimeError(
            "Refusing to run direct-Gemma production adapter: "
            f"expected researcher.py {EXPECTED_RESEARCH_ONE_VERSION!r}, "
            f"found {live_version!r}. Inspect live files before proceeding."
        )

    # v7.7 should expose these deterministic hooks used by its research loop.
    required = [
        "research",
        "run_hermes",
        "normalize_schema",
        "RAW_DIR",
    ]
    missing = [name for name in required if not hasattr(core, name)]
    if missing:
        raise RuntimeError(
            "Refusing to run: researcher.py is missing expected hooks: "
            + ", ".join(missing)
        )



def ollama_check():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=10) as response:
            obj = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot reach Ollama at {OLLAMA_URL}: {exc!r}")

    names = {str(m.get("name") or "") for m in obj.get("models") or []}
    if MODEL not in names:
        raise RuntimeError(
            f"Required model {MODEL!r} is not installed in Ollama. "
            f"Installed names include: {sorted(names)[:20]}"
        )
    return True

def _write_raw(job_id, attempt_number, response_obj):
    core.RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = core.RAW_DIR / f"{job_id}_attempt{attempt_number}_direct_ollama.json"
    path.write_text(
        json.dumps(response_obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path.name


def run_direct_ollama(prompt, job_id, attempt_number):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": OUTPUT_SCHEMA,
        "think": THINK,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "num_ctx": NUM_CTX,
            "temperature": TEMPERATURE,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=OLLAMA_TIMEOUT_SECONDS,
        ) as response:
            response_obj = json.loads(response.read().decode("utf-8"))
    except (TimeoutError, socket.timeout):
        return None, (
            "Direct Ollama Gemma evidence evaluation timed out after "
            f"{OLLAMA_TIMEOUT_SECONDS} seconds on attempt {attempt_number}."
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return None, (
                "Direct Ollama Gemma evidence evaluation timed out after "
                f"{OLLAMA_TIMEOUT_SECONDS} seconds on attempt {attempt_number}."
            )
        return None, f"Direct Ollama request failed: {exc!r}"
    except Exception as exc:
        return None, f"Direct Ollama request failed: {exc!r}"

    raw_name = _write_raw(job_id, attempt_number, response_obj)

    try:
        content = ((response_obj.get("message") or {}).get("content") or "").strip()
        if not content:
            raise ValueError("Ollama returned no message.content")
        obj = core.normalize_schema(json.loads(content))
    except Exception as exc:
        return None, (
            "Could not parse direct Ollama research JSON on attempt "
            f"{attempt_number}: {exc}. Raw output: {raw_name}"
        )

    return obj, None


def research(title, author, year=None):
    compatibility_check()

    # The current controller performs module-global lookups of run_hermes()
    # and gather_live_evidence(). Swap only those hooks for the duration of
    # this single-threaded call. Primary browser infrastructure failures still
    # trigger resilient retry. A failure of the ONE targeted series fallback,
    # however, is bounded: the controller keeps its already-valid primary
    # provisional conclusion (LIKELY_*) rather than turning the book into an
    # infrastructure ERROR and retrying forever.
    original_runner = core.run_hermes
    original_gather = core.gather_live_evidence
    core.run_hermes = run_direct_ollama

    fallback_unavailable_count = 0

    def gather_with_bounded_fallback(*args, **kwargs):
        nonlocal fallback_unavailable_count
        evidence = original_gather(*args, **kwargs)
        is_fallback = kwargs.get("candidate_rank_mode") == "series_fallback"
        if (
            is_fallback
            and isinstance(evidence, dict)
            and evidence.get("status") in {"SEARCH_ERROR", "FETCH_ERROR"}
        ):
            fallback_unavailable_count += 1
            original_status = evidence.get("status")
            original_notes = evidence.get("notes")
            bounded = dict(evidence)
            # researcher.py treats a completed fallback with no readable
            # evidence as bounded and returns the primary provisional object.
            # Use a distinct non-error status so its SEARCH_ERROR/FETCH_ERROR
            # branch does not discard that primary conclusion.
            bounded["status"] = "FALLBACK_UNAVAILABLE"
            bounded["notes"] = (
                "Targeted fallback unavailable; preserving the valid primary "
                f"provisional conclusion. Original {original_status}: "
                f"{original_notes}"
            )
            print(
                f"FALLBACK_UNAVAILABLE title={title!r} "
                f"reason={original_notes!r}; preserving primary conclusion",
                flush=True,
            )
            return bounded
        return evidence

    core.gather_live_evidence = gather_with_bounded_fallback
    started = time.monotonic()
    browser_retries = 0
    try:
        while True:
            result = core.research(title, author, year)
            if not _is_browser_infrastructure_error(result):
                break

            browser_retries += 1
            delay = _browser_retry_delay(browser_retries)
            print(
                f"BROWSER_RETRY title={title!r} retry={browser_retries} "
                f"sleep={delay}s reason={result.get('notes')!r}",
                flush=True,
            )
            time.sleep(delay)
    finally:
        core.run_hermes = original_runner
        core.gather_live_evidence = original_gather

    result["evaluator"] = {
        "adapter": ADAPTER_VERSION,
        "model": MODEL,
        "ollama_url": OLLAMA_URL,
        "num_ctx": NUM_CTX,
        "think": THINK,
        "temperature": TEMPERATURE,
        "timeout_seconds": OLLAMA_TIMEOUT_SECONDS,
        "browser_infrastructure_retries": browser_retries,
        "fallback_unavailable_count": fallback_unavailable_count,
        "wall_seconds": round(time.monotonic() - started, 3),
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--year")
    args = parser.parse_args()

    compatibility_check()

    if args.check:
        ollama_check()
        print(
            json.dumps(
                {
                    "ok": True,
                    "adapter": ADAPTER_VERSION,
                    "research_one_version": getattr(core, "RESEARCH_ONE_VERSION", None),
                    "model": MODEL,
                    "evaluator_path": "direct_ollama_no_hermes",
                    "ollama_url": OLLAMA_URL,
                    "num_ctx": NUM_CTX,
                    "think": THINK,
                    "timeout_seconds": OLLAMA_TIMEOUT_SECONDS,
                },
                indent=2,
            )
        )
        return

    if not args.title or not args.author:
        parser.error("--title and --author are required unless --check is used")

    print(
        json.dumps(
            research(args.title, args.author, args.year),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
