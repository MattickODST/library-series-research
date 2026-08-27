import argparse
import json
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from evidence import gather_live_evidence
from normalize import normalize
from config import (
    EVIDENCE_CACHE_ROOT,
    HERMES_EXECUTABLE,
    RAW_DIR,
    TMP_ROOT,
)

HERMES = HERMES_EXECUTABLE

ALLOWED_STATUS = {
    "VERIFIED",
    "NOT_SERIES",
    "LIKELY_NOT_SERIES",
    "LIKELY_SERIES",
    "UNFOUND",
    "CONFLICT",
    "TIMED_OUT",
    "ERROR",
}

MODEL_RESULT_FIELDS = {
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
}

BONUS_METADATA_FIELDS = {
    "total_volumes_in_series",
    "series_status",
    "isbn_13",
    "isbn_10",
    "publisher",
    "original_publication_year",
    "alternate_series_names",
    "series_position_text",
}

EXPLICIT_STANDALONE_RE = re.compile(
    r"\bstand[ -]?alone\b|"
    r"\bnot\s+(?:part\s+of|in)\s+(?:a|the)\s+series\b|"
    r"\bnon-series\b",
    re.IGNORECASE,
)

MAX_MODEL_ATTEMPTS = 2
HERMES_TIMEOUT_SECONDS = 60
RESEARCH_ONE_VERSION = "v7.7-camofox-timeout-status"


def extract_result(text):
    decoder = json.JSONDecoder()
    found = []

    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue

        if isinstance(obj, dict) and "status" in obj and "title" in obj:
            found.append(obj)

    if not found:
        raise ValueError("No research JSON object found in Hermes output")

    return found[-1]


def _normalize_supports(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[,;]", value) if x.strip()]
    text = str(value).strip()
    return [text] if text else []


def _normalize_confidence(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            if text.endswith("%"):
                return float(text[:-1].strip()) / 100.0
            return float(text)
        except ValueError:
            return value
    return value


def _normalize_int_counter(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return value


def normalize_schema(obj):
    # Keep only contract fields. Python adds controller metadata later.
    obj = {
        key: value
        for key, value in obj.items()
        if key in MODEL_RESULT_FIELDS
    }

    status = obj.get("status")
    if isinstance(status, str):
        obj["status"] = status.strip().upper()

    obj["confidence"] = _normalize_confidence(obj.get("confidence"))
    obj["searches_used"] = _normalize_int_counter(obj.get("searches_used"))
    obj["pages_fetched"] = _normalize_int_counter(obj.get("pages_fetched"))

    is_series = obj.get("is_series")
    if isinstance(is_series, str):
        lowered = is_series.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            obj["is_series"] = True
        elif lowered in {"false", "no", "n", "0"}:
            obj["is_series"] = False
        elif lowered in {"null", "none", "unknown", ""}:
            obj["is_series"] = None

    sources = obj.get("sources")
    normalized_sources = []
    if sources is None:
        sources = []

    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            normalized_sources.append(
                {
                    "source_id": str(
                        source.get("source_id") or ""
                    ).strip().upper(),
                    "supports": _normalize_supports(
                        source.get("supports")
                    ),
                    "evidence": str(
                        source.get("evidence") or ""
                    ).strip(),
                }
            )
    obj["sources"] = normalized_sources

    bonus = obj.get("bonus_metadata")
    if not isinstance(bonus, dict):
        bonus = {}

    clean_bonus = {}
    for key in BONUS_METADATA_FIELDS:
        value = bonus.get(key)
        if key == "alternate_series_names":
            if isinstance(value, list):
                clean_bonus[key] = [
                    str(x).strip()
                    for x in value
                    if str(x).strip()
                ]
            elif value:
                clean_bonus[key] = [str(value).strip()]
            else:
                clean_bonus[key] = []
            continue

        if isinstance(value, str):
            value = value.strip()
            if value.casefold() in {
                "",
                "unknown",
                "none",
                "null",
                "n/a",
                "not found",
            }:
                value = None

        clean_bonus[key] = value

    series_status = clean_bonus.get("series_status")
    if isinstance(series_status, str):
        series_status = series_status.strip().upper()
        if series_status not in {"ONGOING", "COMPLETE"}:
            series_status = None
    clean_bonus["series_status"] = series_status
    obj["bonus_metadata"] = clean_bonus

    series_name = str(obj.get("series_name") or "").strip()
    if (
        obj.get("status") == "VERIFIED"
        and series_name.casefold()
        in {
            "standalone",
            "standalone novel",
            "standalone novels",
            "standalones",
        }
    ):
        obj["status"] = "NOT_SERIES"
        obj["is_series"] = False
        obj["series_name"] = None
        obj["series_number"] = None
        existing = str(obj.get("notes") or "").strip()
        correction = (
            "Deterministic correction: a Standalone/Standalone Novels "
            "bibliography category is not a book series."
        )
        obj["notes"] = (
            existing + " " + correction if existing else correction
        )

    if obj.get("status") in {"UNFOUND", "ERROR"}:
        obj["confidence"] = 0.0
    elif (
        obj.get("status") == "CONFLICT"
        and isinstance(obj.get("confidence"), (int, float))
        and not isinstance(obj.get("confidence"), bool)
    ):
        obj["confidence"] = min(float(obj["confidence"]), 0.5)

    if obj.get("conflicts") is None:
        obj["conflicts"] = []
    elif not isinstance(obj.get("conflicts"), list):
        obj["conflicts"] = [str(obj["conflicts"])]

    return obj


def evidence_source_map(evidence):
    return {
        f"S{index}": page
        for index, page in enumerate(
            evidence.get("fetched_pages") or [],
            start=1,
        )
    }


def enforce_not_series_semantics(obj, source_map):
    if obj.get("status") != "NOT_SERIES":
        return obj

    explicit_support_found = False
    for source in obj.get("sources") or []:
        source_id = str(source.get("source_id") or "").strip().upper()
        page = source_map.get(source_id)
        if not page:
            continue

        model_evidence = str(source.get("evidence") or "")
        page_text = str(page.get("text") or "")

        # Require the model's cited evidence AND the fetched source text to
        # contain explicit standalone/non-series language. This prevents
        # "I saw no series information" from becoming NOT_SERIES.
        if (
            EXPLICIT_STANDALONE_RE.search(model_evidence)
            and EXPLICIT_STANDALONE_RE.search(page_text)
        ):
            explicit_support_found = True
            break

    if explicit_support_found:
        return obj

    existing = str(obj.get("notes") or "").strip()
    correction = (
        "Deterministic correction: absence of series evidence is not "
        "evidence that a book is standalone. No selected fetched source "
        "explicitly stated standalone/non-series status."
    )
    obj["status"] = "LIKELY_NOT_SERIES"
    obj["is_series"] = False
    obj["series_name"] = None
    obj["series_number"] = None
    # Preserve the model's evidence-based confidence and cited sources.
    # The status itself records that explicit standalone proof was not met.
    obj["notes"] = existing + " " + correction if existing else correction
    return obj


def resolve_source_ids(obj, source_map):
    resolved = []
    for source in obj.get("sources") or []:
        source_id = str(source.get("source_id") or "").strip().upper()
        page = source_map.get(source_id)
        if not page:
            continue
        resolved.append(
            {
                "url": str(page.get("url") or "").strip(),
                "supports": source.get("supports") or [],
                "evidence": str(source.get("evidence") or "").strip(),
            }
        )
    obj["sources"] = resolved
    return obj


def validate(obj, allowed_source_ids):
    errors = []

    status = obj.get("status")
    if status not in ALLOWED_STATUS:
        errors.append("invalid status")

    confidence = obj.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        errors.append("confidence must be numeric from 0.0 to 1.0")

    searches = obj.get("searches_used")
    pages = obj.get("pages_fetched")

    if (
        not isinstance(searches, int)
        or isinstance(searches, bool)
        or not 0 <= searches <= 2
    ):
        errors.append("searches_used must be an integer from 0 to 2")

    if (
        not isinstance(pages, int)
        or isinstance(pages, bool)
        or not 0 <= pages <= 4
    ):
        errors.append("pages_fetched must be an integer from 0 to 4")

    sources = obj.get("sources")
    if not isinstance(sources, list):
        errors.append("sources must be a list")
        sources = []

    for source in sources:
        if not isinstance(source, dict):
            errors.append("every source must be an object")
            continue

        source_id = str(
            source.get("source_id") or ""
        ).strip().upper()

        if not source_id:
            errors.append("every source requires a source_id")
        elif source_id not in allowed_source_ids:
            errors.append(
                "every source_id must match supplied fetched evidence"
            )

        if not isinstance(source.get("supports"), list):
            errors.append("source supports must be a list")

    if status in {
        "VERIFIED",
        "NOT_SERIES",
        "LIKELY_NOT_SERIES",
        "LIKELY_SERIES",
    }:
        if not isinstance(pages, int) or pages < 1:
            errors.append(
                "evidence-based statuses require at least one fetched page"
            )
        if not sources:
            errors.append(
                "evidence-based statuses require at least one evidence source"
            )

    if status == "VERIFIED":
        if obj.get("is_series") is not True:
            errors.append("VERIFIED requires is_series=true")
        if not str(obj.get("series_name") or "").strip():
            errors.append("VERIFIED requires series_name")
        if (
            obj.get("series_number") is None
            or str(obj.get("series_number")).strip() == ""
        ):
            errors.append("VERIFIED requires series_number")

    if status == "NOT_SERIES" and obj.get("is_series") is not False:
        errors.append("NOT_SERIES requires is_series=false")

    if (
        status == "LIKELY_NOT_SERIES"
        and obj.get("is_series") is not False
    ):
        errors.append("LIKELY_NOT_SERIES requires is_series=false")

    if status == "LIKELY_NOT_SERIES":
        if str(obj.get("series_name") or "").strip():
            errors.append("LIKELY_NOT_SERIES requires series_name=null")
        if (
            obj.get("series_number") is not None
            and str(obj.get("series_number")).strip() != ""
        ):
            errors.append("LIKELY_NOT_SERIES requires series_number=null")

    if status == "LIKELY_SERIES":
        if obj.get("is_series") is not True:
            errors.append("LIKELY_SERIES requires is_series=true")
        if not str(obj.get("series_name") or "").strip():
            errors.append("LIKELY_SERIES requires a candidate series_name")

    if status == "UNFOUND":
        if obj.get("is_series") is not None:
            errors.append("UNFOUND requires is_series=null")
        if str(obj.get("series_name") or "").strip():
            errors.append("UNFOUND requires series_name=null")
        if (
            obj.get("series_number") is not None
            and str(obj.get("series_number")).strip() != ""
        ):
            errors.append("UNFOUND requires series_number=null")

    if status == "TIMED_OUT":
        errors.append(
            "TIMED_OUT is controller-owned and must never be returned by the model"
        )

    return errors


def timed_out_result(title, author, notes, searches=0, pages=0):
    """
    Controller-owned terminal status for an evaluator timeout.

    A timed-out model response is not treated as a usable conclusion, even
    at low confidence, because it may be incomplete. The live search/fetch
    evidence is still preserved by _finish() and the evidence archive for
    later replay without needing to trust partial model output.
    """
    return {
        "title": title,
        "author": author,
        "status": "TIMED_OUT",
        "is_series": None,
        "series_name": None,
        "series_number": None,
        "confidence": 0.0,
        "searches_used": searches,
        "pages_fetched": pages,
        "sources": [],
        "conflicts": [],
        "notes": notes,
        "bonus_metadata": {
            "total_volumes_in_series": None,
            "series_status": None,
            "isbn_13": None,
            "isbn_10": None,
            "publisher": None,
            "original_publication_year": None,
            "alternate_series_names": [],
            "series_position_text": None,
        },
    }


def error_result(title, author, notes, searches=0, pages=0):
    return {
        "title": title,
        "author": author,
        "status": "ERROR",
        "is_series": None,
        "series_name": None,
        "series_number": None,
        "confidence": 0.0,
        "searches_used": searches,
        "pages_fetched": pages,
        "sources": [],
        "conflicts": [],
        "notes": notes,
        "bonus_metadata": {
            "total_volumes_in_series": None,
            "series_status": None,
            "isbn_13": None,
            "isbn_10": None,
            "publisher": None,
            "original_publication_year": None,
            "alternate_series_names": [],
            "series_position_text": None,
        },
    }


def build_prompt(
    normalized,
    evidence,
    retry_errors=None,
    previous_obj=None,
):
    clean_input = {
        "title": normalized.get("search_title"),
        "author": normalized.get("search_author"),
        "publication_year": normalized.get("publication_year"),
    }

    source_map = evidence_source_map(evidence)
    evidence_for_model = []
    for source_id, page in source_map.items():
        evidence_for_model.append(
            {
                "source_id": source_id,
                "search_result_text": page.get("search_text"),
                "page_text": page.get("text"),
            }
        )

    lines = [
        (
            "Evaluate whether this book belongs to a named book series and, "
            "if so, determine its sequence number."
        ),
        (
            "The web discovery and page fetching were already performed LIVE "
            "by deterministic Python. Do NOT call web_search, browser tools, "
            "terminal tools, or fetch tools. Use only the supplied fetched "
            "page evidence below."
        ),
        (
            "Search-result snippets are context only. VERIFIED and "
            "NOT_SERIES conclusions must be supported by fetched page text."
        ),
        (
            "NOT_SERIES has a strict meaning: a fetched page must explicitly "
            "state or clearly identify the target as standalone/non-series. "
            "The mere absence of series information is NOT enough for "
            "NOT_SERIES."
        ),
        (
            "Use LIKELY_NOT_SERIES when the fetched evidence points toward a "
            "standalone/non-series book but lacks the explicit standalone "
            "proof required for NOT_SERIES. Preserve useful cited evidence "
            "and give an evidence-based confidence instead of zeroing it."
        ),
        (
            "Use LIKELY_SERIES when fetched evidence supports a named series "
            "but the sequence number or other verification needed for "
            "VERIFIED is incomplete or uncertain. Preserve the candidate "
            "series_name, any supportable candidate series_number, cited "
            "sources, and evidence-based confidence."
        ),
        (
            "Use UNFOUND only when the supplied evidence is genuinely too "
            "weak, irrelevant, or contradictory to support even a directional "
            "LIKELY_SERIES or LIKELY_NOT_SERIES conclusion. UNFOUND should "
            "have is_series=null, series_name=null, series_number=null, and "
            "confidence=0."
        ),
        (
            "A publisher/author page explicitly calling the target Book N, "
            "Nth book, part of SERIES, or standalone is strong evidence."
        ),
        (
            "When evidence supports both a broad franchise/umbrella and a "
            "more specific named subseries or trilogy that explicitly numbers "
            "the target book, prefer the MOST SPECIFIC explicitly supported "
            "numbered series. Do not substitute a broader umbrella merely "
            "because it is also true, and do not invent a subseries."
        ),
        (
            "Do not infer a series number from the raw catalog title, edition "
            "number, publication year, or general ordering unless a fetched "
            "page supports that sequence."
        ),
        (
            "An omnibus/collection containing several books is not itself "
            "series book N unless a fetched source explicitly numbers that "
            "omnibus in the series."
        ),
        (
            "Sources MUST use the supplied source_id such as S1 or S2. Do "
            "not reproduce, rewrite, shorten, or invent URLs."
        ),
        "",
        "BONUS METADATA RULE:",
        (
            "bonus_metadata is VERY OPTIONAL. Populate a bonus field only "
            "when it is already plainly and explicitly present in the fetched "
            "page text supplied below. Do not infer it, calculate it, count "
            "a bibliography, request more research, or weaken/delay the "
            "primary series conclusion to fill it. Null/[] is the preferred "
            "answer when a bonus value is not immediately obvious."
        ),
        (
            "Allowed bonus fields: total_volumes_in_series, series_status "
            "(ONGOING or COMPLETE only when explicit), isbn_13, isbn_10, "
            "publisher, original_publication_year, alternate_series_names, "
            "series_position_text."
        ),
        "",
        "Return ONE JSON object only with exactly these fields:",
        (
            "title, author, status, is_series, series_name, series_number, "
            "confidence, searches_used, pages_fetched, sources, conflicts, "
            "notes, bonus_metadata"
        ),
        (
            "status must be VERIFIED, NOT_SERIES, LIKELY_NOT_SERIES, "
            "LIKELY_SERIES, UNFOUND, CONFLICT, or ERROR. TIMED_OUT is "
            "controller-owned; never return TIMED_OUT yourself."
        ),
        (
            "sources must be a list of objects with source_id, supports "
            "(list), and evidence."
        ),
        "",
        "BOOK:",
        json.dumps(clean_input, ensure_ascii=False),
        "",
        "LIVE FETCHED EVIDENCE:",
        json.dumps(evidence_for_model, ensure_ascii=False),
    ]

    if retry_errors:
        lines.extend(
            [
                "",
                "DETERMINISTIC VALIDATION RETRY:",
                json.dumps(retry_errors, ensure_ascii=False),
                "Previous rejected result:",
                json.dumps(previous_obj, ensure_ascii=False),
                (
                    "Correct the JSON/conclusion using the same supplied "
                    "evidence. Do not call any tools."
                ),
            ]
        )

    return "\\n".join(lines)


def run_hermes(prompt, job_id, attempt_number):
    cmd = [
        HERMES,
        "-p",
        "library",
        "chat",
        "-Q",
        "--source",
        "tool",
        "--max-turns",
        "4",
        "-q",
        prompt,
    ]

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    try:
        run = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=HERMES_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, (
            f"Hermes evidence evaluation timed out after "
            f"{HERMES_TIMEOUT_SECONDS} seconds on attempt {attempt_number}."
        )

    raw = (run.stdout or "") + "\n" + (run.stderr or "")
    raw_name = f"{job_id}_attempt{attempt_number}.txt"
    RAW_DIR.joinpath(raw_name).write_text(raw, encoding="utf-8")

    if run.returncode != 0:
        return None, (
            f"Hermes exited with code {run.returncode}; "
            f"raw output saved as {raw_name}"
        )

    try:
        obj = normalize_schema(extract_result(raw))
    except Exception as exc:
        return None, (
            f"Could not parse research JSON on attempt {attempt_number}: "
            f"{exc}. Raw output: {raw_name}"
        )

    return obj, None


def archive_job_evidence(job_tmp_dir, job_id, normalized, evidence, result):
    EVIDENCE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    destination = EVIDENCE_CACHE_ROOT / job_id

    try:
        if job_tmp_dir.exists():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(job_tmp_dir), str(destination))
        else:
            destination.mkdir(parents=True, exist_ok=True)

        manifest = {
            "job_id": job_id,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "normalized_title": normalized.get("search_title"),
            "normalized_author": normalized.get("search_author"),
            "publication_year": normalized.get("publication_year"),
            "live_query": evidence.get("query"),
            "search_cache_hit": evidence.get("search_cache_hit"),
            "evidence_status": evidence.get("status"),
            "result_status": result.get("status"),
            "sources": result.get("sources", []),
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(destination), None
    except Exception as exc:
        return None, repr(exc)


def _finish(
    result,
    job_tmp_dir,
    job_id,
    normalized,
    evidence,
    research_started=None,
    model_seconds=0.0,
):
    researched_at = datetime.now(timezone.utc)
    result["researched_at"] = researched_at.isoformat()

    bonus = result.get("bonus_metadata")
    if not isinstance(bonus, dict):
        bonus = {}
        result["bonus_metadata"] = bonus

    total_volumes = bonus.get("total_volumes_in_series")
    has_total_volumes = (
        total_volumes is not None
        and total_volumes != ""
        and total_volumes != []
    )
    bonus["series_total_as_of"] = (
        researched_at.date().isoformat()
        if has_total_volumes
        else None
    )

    timings = dict(evidence.get("timings") or {})
    timings["model_seconds"] = round(float(model_seconds or 0.0), 3)
    if research_started is not None:
        timings["total_seconds"] = round(
            time.monotonic() - research_started,
            3,
        )
    result["timings"] = timings

    result["live_research"] = {
        "query": evidence.get("query"),
        "search_cache_hit": evidence.get("search_cache_hit"),
        "status": evidence.get("status"),
        "candidate_count": len(evidence.get("candidates") or []),
        "fetched_page_count": len(evidence.get("fetched_pages") or []),
        "fetch_attempts": evidence.get("fetch_attempts") or [],
        "fallback": evidence.get("fallback"),
    }

    archive_path, archive_error = archive_job_evidence(
        job_tmp_dir,
        job_id,
        normalized,
        evidence,
        result,
    )
    result["evidence_archive"] = archive_path

    if archive_error:
        existing = str(result.get("notes") or "").strip()
        note = "Evidence archive warning: " + archive_error
        result["notes"] = existing + " " + note if existing else note

    return result


LEAD_STOPWORDS = {
    "a novel",
    "novel",
    "paperback",
    "hardcover",
    "kindle edition",
    "ebook",
    "audiobook",
    "large print",
    "mass market paperback",
    "book",
}


def deterministic_research_leads(evidence, clean_title):
    """
    Extract only high-confidence title-adjacent parenthetical phrases already
    present in fetched text. These are search leads, never answer evidence.
    This is intentionally narrow so it scales safely across the full catalog.
    """
    title = str(clean_title or "").strip()
    if not title:
        return []

    title_cf = title.casefold()
    leads = []
    seen = set()

    for page in evidence.get("fetched_pages") or []:
        haystacks = [
            str(page.get("search_text") or ""),
            str(page.get("text") or "")[:2600],
        ]

        for haystack in haystacks:
            if not haystack or title_cf not in haystack.casefold():
                continue

            # Examine a bounded area around the target title only.
            pos = haystack.casefold().find(title_cf)
            area = haystack[pos : pos + len(title) + 260]

            for match in re.finditer(r"\(([^()]{3,80})\)", area):
                candidate = re.sub(r"\s+", " ", match.group(1)).strip(" -,:;")
                cf = candidate.casefold()

                if not candidate or cf in LEAD_STOPWORDS:
                    continue
                if any(token in cf for token in ("isbn", "edition", "format")):
                    continue
                if re.fullmatch(r"[\d\s#.,:/-]+", candidate):
                    continue

                # Prefer compact named phrases; avoid whole descriptive blurbs.
                words = candidate.split()
                if not 1 <= len(words) <= 7:
                    continue

                # Strip trailing sequence markers so the catalog's hidden v.#
                # can never leak through this mechanism.
                candidate = re.sub(
                    r",?\s*(?:book\s*)?#?\d+(?:\.\d+)?\s*$",
                    "",
                    candidate,
                    flags=re.I,
                ).strip(" -,:;")

                if len(candidate) < 3:
                    continue

                key = candidate.casefold()
                if key not in seen:
                    seen.add(key)
                    leads.append(candidate)

                if len(leads) >= 3:
                    return leads

    return leads


def deterministic_candidate_leads(candidates, clean_title):
    """
    Extract narrow title-adjacent parenthetical leads from Google result text.
    These are research leads only and can never verify series membership.
    """
    title = str(clean_title or "").strip()
    if not title:
        return []

    title_cf = title.casefold()
    leads = []
    seen = set()

    for candidate in candidates or []:
        text = str(candidate.get("text") or "")
        text_cf = text.casefold()
        pos = text_cf.find(title_cf)
        if pos < 0:
            continue

        area = text[pos : pos + len(title) + 180]
        for match in re.finditer(r"\(([^()]{3,80})\)", area):
            lead = re.sub(r"\s+", " ", match.group(1)).strip(" -,:;")
            cf = lead.casefold()

            if not lead or cf in LEAD_STOPWORDS:
                continue
            if any(token in cf for token in ("isbn", "edition", "format")):
                continue
            if re.fullmatch(r"[\d\s#.,:/-]+", lead):
                continue
            if not 1 <= len(lead.split()) <= 7:
                continue

            lead = re.sub(
                r",?\s*(?:book\s*)?#?\d+(?:\.\d+)?\s*$",
                "",
                lead,
                flags=re.I,
            ).strip(" -,:;")
            if len(lead) < 3:
                continue

            key = lead.casefold()
            if key not in seen:
                seen.add(key)
                leads.append(lead)

            if len(leads) >= 3:
                return leads

    return leads


def merge_fallback_evidence(primary, fallback):
    # The primary pass was already inconclusive. Prioritize up to three
    # targeted, domain-diverse fallback pages and retain one useful primary
    # page for context. The model prompt remains capped at four pages.
    combined_pages = []
    seen = set()

    for page in (
        list(fallback.get("fetched_pages") or [])[:3]
        + list(primary.get("fetched_pages") or [])
    ):
        url = str(page.get("url") or "").strip()
        if not url or url.casefold() in seen:
            continue
        seen.add(url.casefold())
        combined_pages.append(page)
        if len(combined_pages) >= 4:
            break

    merged = dict(primary)
    merged["fetched_pages"] = combined_pages
    merged["fetch_attempts"] = (
        list(primary.get("fetch_attempts") or [])
        + list(fallback.get("fetch_attempts") or [])
    )
    merged["candidates"] = (
        list(primary.get("candidates") or [])
        + list(fallback.get("candidates") or [])
    )

    pt = primary.get("timings") or {}
    ft = fallback.get("timings") or {}
    merged["timings"] = {
        "search_seconds": round(
            float(pt.get("search_seconds") or 0.0)
            + float(ft.get("search_seconds") or 0.0),
            3,
        ),
        "fetch_seconds": round(
            float(pt.get("fetch_seconds") or 0.0)
            + float(ft.get("fetch_seconds") or 0.0),
            3,
        ),
        "evidence_total_seconds": round(
            float(pt.get("evidence_total_seconds") or 0.0)
            + float(ft.get("evidence_total_seconds") or 0.0),
            3,
        ),
    }
    merged["fallback"] = {
        "query": fallback.get("query"),
        "status": fallback.get("status"),
        "search_cache_hit": fallback.get("search_cache_hit"),
        "candidate_count": len(fallback.get("candidates") or []),
        "fetched_page_count": len(fallback.get("fetched_pages") or []),
        "ranking": "series_fallback_domain_diverse",
        "research_leads": fallback.get("research_leads") or [],
    }
    return merged


def research(title, author, year=None):
    research_started = time.monotonic()
    model_seconds = 0.0
    job_id = uuid.uuid4().hex[:12]
    normalized = normalize(title, author, year)

    if normalized.get("publication_year") is None and year is not None:
        normalized["publication_year"] = year

    job_tmp_dir = TMP_ROOT / job_id
    job_tmp_dir.mkdir(parents=True, exist_ok=True)

    clean_title = normalized.get("search_title") or title
    clean_author = normalized.get("search_author") or author

    def finish(result, current_evidence):
        return _finish(
            result,
            job_tmp_dir,
            job_id,
            normalized,
            current_evidence,
            research_started=research_started,
            model_seconds=model_seconds,
        )

    try:
        evidence = gather_live_evidence(
            clean_title,
            clean_author,
            job_tmp_dir,
        )
    except Exception as exc:
        evidence = {
            "status": "SEARCH_ERROR",
            "query": f"{clean_title} {clean_author}".strip(),
            "search_cache_hit": False,
            "candidates": [],
            "fetched_pages": [],
            "fetch_attempts": [],
            "notes": repr(exc),
            "timings": {},
        }

    searches_used = 1 if evidence.get("query") else 0
    pages_fetched = len(evidence.get("fetched_pages") or [])

    if evidence.get("status") in {"SEARCH_ERROR", "FETCH_ERROR"}:
        result = error_result(
            title,
            author,
            evidence.get("notes") or (
                "Live evidence acquisition failed."
            ),
            searches=searches_used,
            pages=pages_fetched,
        )
        return finish(result, evidence)

    if evidence.get("status") != "OK" or pages_fetched < 1:
        result = {
            "title": title,
            "author": author,
            "status": "UNFOUND",
            "is_series": None,
            "series_name": None,
            "series_number": None,
            "confidence": 0.0,
            "searches_used": searches_used,
            "pages_fetched": pages_fetched,
            "sources": [],
            "conflicts": [],
            "notes": (
                "Live research completed but produced no readable evidence "
                "sufficient for evaluation."
            ),
            "bonus_metadata": {
                "total_volumes_in_series": None,
                "series_status": None,
                "isbn_13": None,
                "isbn_10": None,
                "publisher": None,
                "original_publication_year": None,
                "alternate_series_names": [],
                "series_position_text": None,
            },
        }
        return finish(result, evidence)

    fallback_used = False
    previous_obj = None
    previous_errors = None
    attempt_number = 1

    while True:
        source_map = evidence_source_map(evidence)
        allowed_source_ids = set(source_map)
        pages_fetched = len(source_map)

        prompt = build_prompt(
            normalized,
            evidence,
            retry_errors=previous_errors,
            previous_obj=previous_obj,
        )

        model_started = time.monotonic()
        model_job_id = (
            f"{job_id}_fallback" if fallback_used else job_id
        )
        obj, run_error = run_hermes(
            prompt,
            model_job_id,
            attempt_number,
        )
        model_seconds += time.monotonic() - model_started

        if run_error:
            is_timeout = "timed out" in str(run_error).casefold()

            if is_timeout:
                # A model timeout is an evaluator failure, not evidence
                # uncertainty. Do not spend another search or inference pass.
                # Preserve the already acquired evidence for a later replay.
                result = timed_out_result(
                    title,
                    author,
                    run_error,
                    searches=searches_used,
                    pages=pages_fetched,
                )
                return finish(result, evidence)

            if attempt_number < MAX_MODEL_ATTEMPTS:
                previous_obj = error_result(
                    title,
                    author,
                    run_error,
                    searches=searches_used,
                    pages=pages_fetched,
                )
                previous_errors = [run_error]
                attempt_number += 1
                continue

            result = error_result(
                title,
                author,
                run_error,
                searches=searches_used,
                pages=pages_fetched,
            )
            return finish(result, evidence)

        # Python owns these counters.
        obj["searches_used"] = searches_used
        obj["pages_fetched"] = pages_fetched

        obj = enforce_not_series_semantics(obj, source_map)
        errors = validate(obj, allowed_source_ids)

        if errors:
            if attempt_number < MAX_MODEL_ATTEMPTS:
                previous_obj = obj
                previous_errors = errors
                attempt_number += 1
                continue

            result = error_result(
                title,
                author,
                "Deterministic validation failed after one correction "
                "attempt: " + "; ".join(errors),
                searches=searches_used,
                pages=pages_fetched,
            )
            return finish(result, evidence)

        # One targeted fallback search is allowed ONLY for the primary series
        # question when the first evidence set is unresolved or provisional.
        # Bonus metadata can never trigger this. LIKELY_* is therefore most
        # useful as a final post-fallback status, not a way to skip research.
        if (
            obj.get("status")
            in {"UNFOUND", "LIKELY_NOT_SERIES", "LIKELY_SERIES"}
            and not fallback_used
        ):
            fallback_used = True
            fallback_dir = job_tmp_dir / "fallback"

            # The catalog's v.# hint remains hidden. Reuse only narrow,
            # deterministic title-adjacent clues already present in live
            # evidence to sharpen the one allowed fallback.
            leads = deterministic_research_leads(evidence, clean_title)
            lead_clause = (
                " " + " ".join(f'"{lead}"' for lead in leads[:2])
                if leads
                else ""
            )
            fallback_title = (
                f'"{clean_title}"{lead_clause} series book order'
            )
            fallback_author = f'"{clean_author}"'

            try:
                fallback = gather_live_evidence(
                    fallback_title,
                    fallback_author,
                    fallback_dir,
                    max_candidate_urls=8,
                    max_fetched_pages=4,
                    total_fetch_budget_seconds=38,
                    candidate_rank_mode="series_fallback",
                    ranking_title=clean_title,
                )
            except Exception as exc:
                fallback = {
                    "status": "SEARCH_ERROR",
                    "query": (
                        f"{fallback_title} {fallback_author}".strip()
                    ),
                    "search_cache_hit": False,
                    "candidates": [],
                    "fetched_pages": [],
                    "fetch_attempts": [],
                    "notes": repr(exc),
                    "timings": {},
                }

            searches_used = 2
            fallback_candidate_leads = deterministic_candidate_leads(
                fallback.get("candidates") or [],
                clean_title,
            )
            combined_leads = []
            seen_leads = set()
            for lead in list(leads) + list(fallback_candidate_leads):
                key = str(lead).casefold()
                if key and key not in seen_leads:
                    seen_leads.add(key)
                    combined_leads.append(lead)
            fallback["research_leads"] = combined_leads[:3]

            if fallback.get("status") in {
                "SEARCH_ERROR",
                "FETCH_ERROR",
            }:
                result = error_result(
                    title,
                    author,
                    (
                        "Primary evidence was inconclusive and the one "
                        "targeted fallback research attempt failed: "
                        + str(fallback.get("notes") or fallback.get("status"))
                    ),
                    searches=searches_used,
                    pages=pages_fetched,
                )
                evidence["fallback"] = {
                    "query": fallback.get("query"),
                    "status": fallback.get("status"),
                    "search_cache_hit": fallback.get("search_cache_hit"),
                }
                return finish(result, evidence)

            if (
                fallback.get("status") == "OK"
                and fallback.get("fetched_pages")
            ):
                evidence = merge_fallback_evidence(evidence, fallback)
                previous_obj = None
                previous_errors = None
                # Give the targeted evidence a fresh model evaluation budget.
                attempt_number = 1
                continue

            # A completed fallback with no readable evidence is a bounded
            # UNFOUND, not an infrastructure failure.
            obj = resolve_source_ids(obj, source_map)
            return finish(obj, evidence)

        obj = resolve_source_ids(obj, source_map)
        return finish(obj, evidence)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--year")
    args = parser.parse_args()

    print(
        json.dumps(
            research(args.title, args.author, args.year),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
