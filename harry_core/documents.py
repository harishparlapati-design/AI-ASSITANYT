"""Local document intelligence.

Reads and summarises documents without adding heavy dependencies:

  .txt/.md/.log/.json/.csv/.py/.html   plain text
  .xlsx                                native OOXML (zipfile + ElementTree)
  .docx                                native OOXML (zipfile + ElementTree)
  .pdf                                 pypdf when installed, otherwise a clear message

Summaries are extractive by default (sentence scoring, fully offline) and are
upgraded to a model-written abstract when an LLM is reachable. The result says
which method was used, so the answer is never overstated.
"""

import logging
import os
import re
import zipfile
from xml.etree import ElementTree

from . import llm, runtime_paths

log = logging.getLogger("harry.documents")

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".json", ".csv", ".tsv",
                   ".py", ".js", ".ts", ".html", ".htm", ".xml", ".yaml", ".yml",
                   ".ini", ".cfg", ".rst"}

_NS_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _path(p):
    p = str(p or "")
    if p and not os.path.isabs(p):
        return os.path.join(runtime_paths.workspace_root(), p)
    return p


def _read_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for para in root.iter(_NS_WORD + "p"):
        text = "".join(node.text or "" for node in para.iter(_NS_WORD + "t"))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def _read_xlsx(path):
    """Minimal OOXML reader: shared strings + cell values per worksheet."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(_NS_SHEET + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_NS_SHEET + "t")))
        sheets = sorted(n for n in z.namelist()
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        out = []
        for sheet in sheets[:10]:
            root = ElementTree.fromstring(z.read(sheet))
            rows = []
            for row in root.iter(_NS_SHEET + "row"):
                cells = []
                for cell in row.iter(_NS_SHEET + "c"):
                    value = cell.find(_NS_SHEET + "v")
                    text = value.text if value is not None else ""
                    if cell.get("t") == "s" and text.isdigit():
                        idx = int(text)
                        text = shared[idx] if idx < len(shared) else text
                    cells.append(text or "")
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells))
            out.append(f"--- {os.path.basename(sheet)} ---\n" + "\n".join(rows))
        return "\n\n".join(out)


def _read_pdf(path):
    try:
        from pypdf import PdfReader
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            raise RuntimeError("PDF reading needs pypdf: pip install pypdf")
    reader = PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages[:40], 1):
        pages.append(f"--- page {i} ---\n" + (page.extract_text() or ""))
    return "\n\n".join(pages)


def extract_text(path, max_chars=20000):
    path = _path(path)
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"document not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXTENSIONS:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    if ext == ".docx":
        return _read_docx(path)[:max_chars]
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path)[:max_chars]
    if ext == ".pdf":
        return _read_pdf(path)[:max_chars]
    raise RuntimeError(f"unsupported document type: {ext or 'unknown'}")


def read_document(args):
    path = args.get("path", "")
    try:
        text = extract_text(path, max_chars=int(args.get("max_chars", 20000)))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": _path(path), "chars": len(text), "text": text}


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n{2,}", text) if len(s.strip()) > 40]


def extractive_summary(text, max_sentences=7):
    """Offline summary: score sentences by word frequency, keep original order."""
    sentences = _sentences(text)
    if not sentences:
        return str(text).strip()[:800]
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9']+", text.lower())
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    scored = []
    for idx, sentence in enumerate(sentences):
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9']+", sentence.lower())
        if not tokens:
            continue
        score = sum(freq.get(t, 0) for t in tokens) / (len(tokens) ** 0.5)
        scored.append((score, idx, sentence))
    top = sorted(scored, key=lambda x: -x[0])[:max_sentences]
    ordered = [s for _, _, s in sorted(top, key=lambda x: x[1])]
    return " ".join(ordered)


def summarize_document(args):
    path = args.get("path", "")
    try:
        text = extract_text(path, max_chars=int(args.get("max_chars", 18000)))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not text.strip():
        return {"ok": False, "error": "document contains no extractable text"}
    summary, method = None, "extractive"
    try:
        summary = llm.chat([
            {"role": "system", "content": "You summarise documents faithfully. "
             "Use only the text given; do not add facts that are not present."},
            {"role": "user", "content": f"Summarise this document in at most "
             f"{int(args.get('sentences', 7))} sentences:\n\n{text[:12000]}"},
        ], task_class=llm.CHAT, options={"num_predict": 350})
        method = "model"
    except Exception as e:
        log.info("model summary unavailable (%s); using offline summary", e)
    if not summary:
        summary = extractive_summary(text, int(args.get("sentences", 7)))
    return {"ok": True, "path": _path(path), "summary": summary.strip(),
            "method": method, "chars_read": len(text)}


def register_document_tools(registry):
    from .tools import Tool
    from .security import SAFE
    registry.register(Tool(
        "read_document",
        "Read a document (.txt .md .csv .json .docx .xlsx .pdf) as text",
        read_document, SAFE, {"path": "str", "max_chars": "int"},
    ))
    registry.register(Tool(
        "summarize_document",
        "Summarise a local document (offline extractive fallback)",
        summarize_document, SAFE,
        {"path": "str", "sentences": "int", "max_chars": "int"},
    ))
