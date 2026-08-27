# Architecture

The pipeline enriches book records from a local (to me) library with series metadata using live web evidence and llm evaluation.

```mermaid
flowchart TD
    A[Excel source workbook] --> B[Python batch controller]
    B --> C[(SQLite job state)]
    C --> D[Research controller]
    D --> E[Browser search]
    E --> F[Camofox]
    D --> G[Page retrieval]
    G --> H[Scrapling]
    F --> I[Evidence set]
    H --> I
    I --> J[Local LLM evaluator]
    J --> K[Ollama / Gemma4 12B]
    K --> L[Structured classification]
    L --> C
    C --> M[Excel exporter]
    M --> N[Human-review workbook]
```

## Key design choices

- SQLite is the runtime source of truth rather than Excel.
- Each record is checkpointed independently.
- Interrupted work can be resumed.
- Web discovery and page retrieval are separated from LLM evaluation.
- Failed or ambiguous records can be retried without rerunning successful records.
