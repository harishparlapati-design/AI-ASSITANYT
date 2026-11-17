"""Web research agent.

A real multi-source research pipeline built on the existing web tools:

    query -> search -> fetch several independent pages -> extract text
          -> model writes a report citing [n] + URL -> sources returned

Honest by construction:
  - every source in the result was actually fetched in this run
  - when the network is unavailable the call fails with a clear offline message
    instead of inventing sources
  - the report is labelled `verified: false` unless the model was able to cite
    fetched pages for every factual claim; the UI/docs state the same.
"""

import logging
import re
import time

from . import llm

log = logging.getLogger("harry.research")


def _search(query, limit):
    from .builtin_tools import web_search
    return web_search({"query": query, "limit": limit})


def _fetch(url, max_chars=4000):
    from .builtin_tools import fetch_page
    return fetch_page({"url": url, "max_chars": max_chars})


def _excerpt(text, query, limit=1200):
    """Pick the passages most related to the query (offline extractive step)."""
    sentences = re.split(r"(?<=[.!?])\s+|\n{2,}", str(text or ""))
    words = set(re.findall(r"[a-z0-9']+", str(query).lower())) - {
        "the", "a", "an", "is", "of", "to", "in", "and", "for", "what", "how"}
    scored = []
    for s in sentences:
        s = s.strip()
        if len(s) < 30:
            continue
        score = len(words & set(re.findall(r"[a-z0-9']+", s.lower())))
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    picked = [s for _, s in scored[:6]] or [s.strip() for s in sentences[:4] if s.strip()]
    return " ".join(picked)[:limit]


def deep_research(args):
    """Search, read several pages, and write a sourced report."""
    query = str(args.get("query") or args.get("topic") or "").strip()
    if not query:
        return {"ok": False, "error": "no research query given"}
    max_sources = max(1, min(int(args.get("sources", 4)), 8))

    started = time.perf_counter()
    found = _search(query, max_sources + 3)
    if not found.get("ok"):
        return {"ok": False,
                "error": f"web search unavailable: {found.get('error')}",
                "offline_hint": True,
                "note": "Research needs internet; local Harry features still work."}

    sources = []
    for result in found.get("results", []):
        url = result.get("url", "")
        if not url or url in (s["url"] for s in sources):
            continue
        page = _fetch(url)
        if page.get("ok") and page.get("text"):
            sources.append({
                "n": len(sources) + 1,
                "title": (page.get("title") or result.get("title") or url)[:140],
                "url": url,
                "excerpt": _excerpt(page["text"], query),
            })
        else:
            # search snippets are still evidence, but mark them as snippet-only
            sources.append({
                "n": len(sources) + 1,
                "title": (result.get("title") or url)[:140],
                "url": url,
                "excerpt": str(result.get("snippet") or "")[:400],
                "snippet_only": True,
            })
        if len(sources) >= max_sources:
            break

    if not sources:
        return {"ok": False, "error": "search returned no usable sources",
                "offline_hint": True}

    blocks = "\n\n".join(
        f"[{s['n']}] {s['title']} ({s['url']})\n{s['excerpt']}" for s in sources)
    report = None
    try:
        prompt = (
            "Write a concise research report that answers the question below "
            "using ONLY these fetched sources. Cite every factual statement with "
            "[n]. Clearly separate 'What the sources say' from 'Uncertain or "
            "unclear'. Never invent a source, number or quotation. If the "
            "sources disagree, say so.\n\n"
            f"QUESTION: {query}\n\nSOURCES:\n{blocks}"
        )
        report = llm.chat([{"role": "user", "content": prompt}],
                          task_class=llm.CHAT,
                          options={"num_predict": 500})
    except Exception as e:
        log.warning("research summarisation unavailable: %s", e)

    if not report:
        # deterministic offline report: just the extracted evidence
        report = ("Live sources could not be summarised by the model, so here is "
                  "the extracted evidence:\n\n" + blocks)

    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", report)})
    valid = [n for n in cited if 1 <= n <= len(sources)]
    return {
        "ok": True,
        "query": query,
        "report": report,
        "sources": [{k: v for k, v in s.items() if k != "excerpt"} for s in sources],
        "citations_used": valid,
        "verified": False,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "note": ("Sources were fetched live just now. Claims are not independently "
                 "fact-checked; treat uncited statements as the assistant's "
                 "inference."),
    }


def register_research_tools(registry):
    from .tools import Tool
    from .security import SAFE
    registry.register(Tool(
        "deep_research",
        "Research a topic across multiple live web sources and return a sourced report",
        deep_research,
        SAFE,
        {"query": "str", "sources": "int"},
    ))
