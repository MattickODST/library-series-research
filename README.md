# Library Series Research Pipeline

A local-first book data-enrichment pipeline for researching book-series membership using live web evidence, browser automation, persistent SQLite state, and local LLM inference.

The original production workload processed **17,467 book records** while preserving source provenance and explicit uncertainty states.

## Why I Built It

The source dataset is from a local library and contained thousands of books that needed research into series membership, series name, and position that they use to evaluate whether they needed more books in a certain series. This was supplimental data that was eventually added to an existing dataset that included other items like rate of checkout, checkout status, etc. A spreadsheet-only workflow became difficult to resume, validate, and recover at this scale, so the project evolved into a persistent batch-processing system where SQLite is the runtime source of truth and Excel is primarily an input and human-review format.

## Features

- Persistent SQLite job queue with WAL journaling
- Resume/recovery after interrupted processing
- Local inference through Ollama
- Browser-based live web discovery
- Page retrieval with Scrapling
- Search caching and retrieval time budgets
- Structured LLM output validation
- Source provenance, confidence, and conflict tracking
- Explicit timeout/error states
- Selective retry of unresolved records
- Excel review export
- Local-model replay benchmarks
- Automated retry-safety tests

## Research States

| Status | Meaning |
|---|---|
| VERIFIED | Confirmed series membership |
| NOT_SERIES | Confirmed standalone title |
| LIKELY_SERIES | Series membership appears likely |
| LIKELY_NOT_SERIES | Standalone classification appears likely |
| UNFOUND | Insufficient usable evidence |
| CONFLICT | Sources materially disagree |
| TIMED_OUT | Research exceeded a technical time budget |
| ERROR | Infrastructure or processing failure |

## CLI

- Initialize: `python src/batch.py init`
- Status: `python src/batch.py status`
- Run: `python src/runner.py run`
- Retry unresolved: `python src/batch.py retry`
- Export: `python src/export.py`

The retry command requeues only `CONFLICT`, `UNFOUND`, `TIMED_OUT`, and `ERROR`. Successful and likely classifications are left untouched, and this behavior is covered by automated tests.

## Architecture

See [Architecture](docs/architecture.md).

## Benchmarks

On a matched 14-record replay, Gemma4 12B averaged **12.344 seconds** per model call and Qwen3.5 9B averaged **5.840 seconds**. Neither exceeded the 60-second production timeout threshold. 

I ended up primarily running the Gemma4 12B as it seemed to provide more accurate results, even at the cost of speed. 

See [Model Benchmarks](docs/benchmarks.md).

## Technology

Python 3.12, SQLite, openpyxl, Scrapling, Ollama, Gemma4 12B, Camofox, Docker, and Excel.

## Development

Create a virtual environment, install `requirements-dev.txt`, then run:

`python -m unittest discover -s tests -v`

## Project Status

This repository is a sanitized and documented version of a working personal data-enrichment pipeline. Production library data and development-only artifacts have intentionally been removed.
