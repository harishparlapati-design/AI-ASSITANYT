"""End-to-end latency benchmark through the real Harry pipeline.

Run: python tests/perf_bench.py [count]

Runs a fixed conversational script through Brain.handle_input (the same path
the dashboard and voice use), then reports time to first visible text and total
latency: median, mean, p95, worst case, and the first five versus the last five
exchanges. Speech is disabled so audio never pollutes the measurement.

Requires the configured local model; it measures real inference, not a mock.
"""

import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harry_core import config, timing  # noqa: E402
from harry_core.brain import get_brain  # noqa: E402

SCRIPT = [
    "hello harry",
    "what's the time",
    "what is a capacitor",
    "what is the difference between a resistor and a capacitor",
    "explain how a transformer works",
    "what is python used for",
    "why is the sky blue",
    "what does HTTP stand for",
    "tell me about the solar system",
    "what is 15% of 240",
    "who are you",
    "ela unnaru",
    "naaku konchem help kavali, what can you do",
    "ee roju date enti",
    "what is machine learning",
    "explain that in simpler terms",
    "what about deep learning",
    "how do I improve my typing speed",
    "what is the capital of Australia",
    "summarize what we discussed so far",
    "what is the difference between RAM and ROM",
    "how does a lithium battery work",
    "what is voltage",
    "give me three tips to sleep better",
    "what is the speed of light",
    "how do computers store text",
    "what is a good daily routine",
    "thanks",
    "what did you say about capacitors",
    "goodbye",
]


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


def summarise(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    if not values:
        return None
    return {
        "n": len(values),
        "median": round(statistics.median(values)),
        "mean": round(statistics.mean(values)),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else len(SCRIPT)
    config.CONFIG.setdefault("voice", {})["output_mode"] = "text"
    brain = get_brain()
    brain.speaker.enabled = False

    # Mirror the real startup path: models are warmed in the background before
    # the user's first question, so their load time is measured separately.
    from harry_core.llm import get_router
    warm = get_router().warm()
    print(f"warm-up: {warm}", flush=True)

    rows = []
    for index, text in enumerate(SCRIPT[:count], 1):
        started = time.perf_counter()
        answer = brain.handle_input(text, source="bench")
        elapsed = (time.perf_counter() - started) * 1000
        trace = (timing.recent(1) or [{}])[0]
        row = {
            "i": index,
            "text": text[:48],
            "total_ms": trace.get("total_ms") or round(elapsed),
            "first_token_ms": trace.get("first_token_ms"),
            "chars": len(answer or ""),
            "model": trace.get("model") or "",
        }
        rows.append(row)
        first = f"{row['first_token_ms']}ms" if row["first_token_ms"] else "-"
        print(f"{index:2d}. first={first:>8} total={row['total_ms']:>6}ms "
              f"chars={row['chars']:>4} {text[:40]!r}", flush=True)

    report = {
        "exchanges": len(rows),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": summarise(rows, "total_ms"),
        "first_visible": summarise(rows, "first_token_ms"),
        "first_five_total_mean": round(
            statistics.mean([r["total_ms"] for r in rows[:5]])) if rows else None,
        "last_five_total_mean": round(
            statistics.mean([r["total_ms"] for r in rows[-5:]])) if rows else None,
        "rows": rows,
    }
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "_backups")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "perf_latest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
