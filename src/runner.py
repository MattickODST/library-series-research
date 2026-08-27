#!/usr/bin/env python3
"""
Iris Library resilient production batch runner using direct Gemma4:12B (v3).

Production runner for the SQLite-backed batch pipeline. It preserves the
batch state logic while using the direct local-LLM evaluator.
which waits and retries the same book on transient browser-discovery failures.
"""

import batch
from evaluator import compatibility_check, ollama_check, research

DIRECT_STATUSES = {
    "VERIFIED",
    "NOT_SERIES",
    "LIKELY_NOT_SERIES",
    "LIKELY_SERIES",
    "UNFOUND",
    "CONFLICT",
    "TIMED_OUT",
    "ERROR",
}

batch.RESEARCH_STATUSES = set(DIRECT_STATUSES)
batch.research = research


def main():
    compatibility_check()
    ollama_check()
    print(
        "EVALUATOR=direct_ollama model=gemma4:12b ctx=16384 think=false "
        "browser_retry=primary_only:1m,3m,5m,10m_then_10m fallback_failure=preserve_primary",
        flush=True,
    )
    batch.main()


if __name__ == "__main__":
    main()
