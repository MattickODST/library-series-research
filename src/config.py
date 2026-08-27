"""Runtime configuration for the library research pipeline."""

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(
    os.getenv(
        "LIBRARY_RESEARCH_ROOT",
        str(Path(__file__).resolve().parents[1]),
    )
).resolve()

INPUT_XLSX = Path(
    os.getenv(
        "LIBRARY_INPUT_XLSX",
        str(PROJECT_ROOT / "input" / "Master_Series_List.xlsx"),
    )
)

DB_PATH = Path(
    os.getenv(
        "LIBRARY_DB_PATH",
        str(PROJECT_ROOT / "state" / "library_research.sqlite"),
    )
)

DEFAULT_OUTPUT = Path(
    os.getenv(
        "LIBRARY_OUTPUT_XLSX",
        str(PROJECT_ROOT / "output" / "Master_Series_List_researched.xlsx"),
    )
)

RAW_DIR = Path(
    os.getenv(
        "LIBRARY_RAW_DIR",
        str(PROJECT_ROOT / "state" / "raw"),
    )
)

TMP_ROOT = Path(
    os.getenv(
        "LIBRARY_TMP_DIR",
        str(PROJECT_ROOT / "tmp"),
    )
)

EVIDENCE_CACHE_ROOT = Path(
    os.getenv(
        "LIBRARY_EVIDENCE_CACHE",
        str(PROJECT_ROOT / "cache" / "evidence"),
    )
)

BROWSER_CACHE_DB = Path(
    os.getenv(
        "LIBRARY_BROWSER_CACHE_DB",
        str(PROJECT_ROOT / "cache" / "browser_search.sqlite"),
    )
)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:12b")

CAMOFOX_URL = os.getenv("CAMOFOX_URL", "http://localhost:9377")

# fetch_page.py can run in the same environment by default. Deployments that
# use a dedicated Scrapling virtualenv can override this.
FETCH_PYTHON = os.getenv("FETCH_PYTHON", sys.executable)
FETCH_SCRIPT = os.getenv(
    "FETCH_SCRIPT",
    str(PROJECT_ROOT / "src" / "fetch_page.py"),
)

# Used only by the legacy evaluator hook retained by researcher.py.
HERMES_EXECUTABLE = os.getenv("HERMES_EXECUTABLE", "hermes")
