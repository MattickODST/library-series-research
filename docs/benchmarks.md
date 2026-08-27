# Local Model Benchmarks

I reviewed a few LLM's before landing on the evaluator I wanted. Below are some of the results.

## Matched 14-record replay

Both models were evaluated on the same 14 previously researched records using:

- 16,384-token context
- temperature 0
- reasoning/thinking disabled
- 60-second production timeout threshold
- identical research prompt/controller version

| Model | Records | Average model wall time | >60s | Output distribution |
|---|---:|---:|---:|---|
| Gemma4 12B | 14 | 12.344 s | 0 | 5 VERIFIED, 1 LIKELY_SERIES, 8 LIKELY_NOT_SERIES |
| Qwen3.5 9B | 14 | 5.840 s | 0 | 7 VERIFIED, 7 LIKELY_NOT_SERIES |

Qwen3.5 9B was approximately 2.1x faster in this small replay benchmark.

## Interpretation

This was primarily a latency and production-compatibility benchmark, not an accuracy benchmark. The models produced different classification distributions, and the replay did not include an independent ground-truth label set sufficient to claim one model was more accurate but in further (not included) testing, I did find the Gemma4 12B seemed to be accurate enough for the purpose of this project.

Gemma4 12B was ultimately used by the production runner represented in this repository.
