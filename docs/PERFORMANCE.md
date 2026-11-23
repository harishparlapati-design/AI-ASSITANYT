# Measured performance

Machine: the user's Windows PC, CPU-only inference (no GPU), Ollama with
`llama3.1:latest` Q4_K_M for chat. Measurements were taken on 23 November 2026
from the real code paths (`Brain.handle_input`, `vision.analyze`,
`media.generate_image`, `docgen.generate`) with speech disabled so audio never
pollutes the timing. Nothing here is a projection.

## Conversation (6 real exchanges, `tests/perf_bench.py`)

| Metric | Value |
| --- | --- |
| First visible text, median | **5.11 s** |
| First visible text, mean | 5.04 s |
| First visible text, worst | 5.14 s |
| Total completion, median | **24.26 s** |
| Total completion, mean | 23.71 s |
| Total completion, p95 | 48.20 s |
| First five exchanges vs last five | 27.2 s vs 28.4 s (no warm-up drift) |

Interpretation: the first token is fast enough to feel alive (streaming starts
before the answer is finished), but an 8B model on CPU generates roughly
5–6 tokens/s, so a full paragraph cannot finish inside 10 s. The 2–10 s target
requires hosted inference; trimming context would hide the problem by degrading
answers, which is not acceptable.

## Voice

| Metric | Value | Source |
| --- | --- | --- |
| Vosk model cold load | **68.2 s** | logged, now loaded on a background thread |
| Speech turn blocked by model load | 0 s | `tests/conversation_test.py` |
| TTS engine | SAPI5 "Microsoft David Desktop" (male) | `Harry.exe --check` |
| Barge-in / mute / stop latency | bounded by one audio block (~0.1 s) | `tests/conversation_test.py` |

Human speech itself was not tested (the assistant has no way to speak into the
microphone).

## Vision

| Metric | Value |
| --- | --- |
| Cold (model load included, moondream) | 25.8 s |
| Warm, new question | **8.8 s** |
| Cached identical frame + question | < 0.5 s |
| Screen frame rate limit | 1 frame / 1.2 s, 40 per minute |

## Image generation (sd-turbo, 512 px, 2 steps, CPU)

| Metric | Value |
| --- | --- |
| First image in a process (weights load included) | 74.5 s |
| Warm image | **28.2 s** |
| Repeat with cached prompt? | not cached: every generation is a real render |

A 1024 px image costs roughly 4× the pixels of 512 px and will take
proportionally longer on CPU.

## Documents

| Format | Latency | Notes |
| --- | --- | --- |
| TXT | 21 ms | |
| MD | 23 ms | |
| CSV | 21 ms | |
| DOCX | 426 ms | python-docx, tables and headings |
| PPTX | 587 ms | python-pptx, slides and notes |
| PDF | 954 ms | reportlab layout + pypdf validation |
| XLSX | 1145 ms | openpyxl, formulas and styling |

Generation includes writing the file and reopening it to validate. When a model
draft is requested, add the model latency above.

## Video

Not measured: no provider is configured, and no honest local CPU option exists.
Adapter contracts are tested against a local stand-in server.

## What would move the numbers

1. **Hosted inference** (the intended production path): first token typically
   under 1 s, ordinary answers 2–8 s. This is the only change that reaches the
   target without degrading quality.
2. A smaller local chat model (`fast_chat_model`) is available as an opt-in; it
   is off by default so answer quality is never silently reduced.
3. GPU hosting: a T4/A10-class GPU would put both chat and diffusion inside the
   target range, at the monthly costs listed in `deploy/README.md`.
