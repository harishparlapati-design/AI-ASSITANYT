"""Real document generation.

Creates actual files - never placeholders - for the seven formats Harry
promises: PDF, DOCX, XLSX, PPTX, CSV, TXT and Markdown.

Two inputs are supported:

* structured content (headings, paragraphs, bullets, tables, sheets, slides)
* plain text / Markdown, which is parsed into the same structure

When an LLM is reachable the API can also turn a natural-language request into
that structure. If the model is unavailable the caller must supply content;
nothing is invented silently.

Every generated file is reopened with the library that wrote it (pypdf,
python-docx, openpyxl, python-pptx) before the result is reported as ok.
"""

import csv
import json
import logging
import os
import re
import threading
import time
import uuid

from . import runtime_paths

log = logging.getLogger("harry.docgen")

FORMATS = ("pdf", "docx", "xlsx", "pptx", "csv", "txt", "md")
MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 20000
MAX_SLIDES = 120
MAX_SECTIONS = 200

_lock = threading.RLock()
_index_cache = {"path": None, "documents": {}}


# ------------------------------------------------------------------ locations

def documents_root():
    """Writable folder holding generated documents (git-ignored)."""
    base = os.environ.get("HARRY_DOCUMENTS_DIR", "").strip()
    if base:
        return os.path.abspath(base)
    return os.path.join(runtime_paths.workspace_root(), "data", "documents")


def _index_path():
    return os.path.join(documents_root(), "index.json")


def _load_index():
    path = _index_path()
    with _lock:
        if _index_cache["path"] == path:
            return _index_cache["documents"]
        documents = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("documents"), dict):
                documents = raw["documents"]
        except Exception:
            documents = {}
        _index_cache["path"] = path
        _index_cache["documents"] = documents
        return documents


def _save_index(documents):
    root = documents_root()
    os.makedirs(root, exist_ok=True)
    path = _index_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"documents": documents}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with _lock:
        _index_cache["path"] = path
        _index_cache["documents"] = documents


# ------------------------------------------------------------------ helpers

def _normalise_format(value, filename=""):
    fmt = str(value or "").strip().lower().lstrip(".")
    if not fmt and filename:
        fmt = os.path.splitext(str(filename))[1].lower().lstrip(".")
    aliases = {"markdown": "md", "text": "txt", "csv": "csv"}
    fmt = aliases.get(fmt, fmt)
    if fmt not in FORMATS:
        raise ValueError(f"unsupported format: {value!r} (use {', '.join(FORMATS)})")
    return fmt


def _slug(text, fallback="document"):
    text = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-").lower()
    return (text[:48] or fallback)


def _text(value):
    return str(value if value is not None else "")


def _num(value, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, default if default else 0))


def _cell_value(value):
    """Spreadsheet cells: numbers stay numbers, everything else is text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    raw = _text(value).strip()
    if not raw:
        return ""
    if raw.startswith("="):
        return raw                       # a real formula, not text
    try:
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if re.fullmatch(r"-?\d+\.\d+", raw):
            return float(raw)
    except Exception:
        pass
    return raw


def _table(spec):
    if not isinstance(spec, dict):
        return None
    headers = [_text(h) for h in (spec.get("headers") or [])]
    rows = spec.get("rows") or []
    if not headers and not rows:
        return None
    clean = []
    for row in rows[:MAX_ROWS]:
        if isinstance(row, dict):
            clean.append([row.get(h, "") for h in headers])
        elif isinstance(row, (list, tuple)):
            clean.append(list(row))
        else:
            clean.append([row])
    return {"headers": headers, "rows": clean}


def normalise_spec(payload, fmt):
    """Accept a dict spec or a plain string, return a validated structure."""
    if isinstance(payload, str):
        spec = parse_markdown(payload)
    elif isinstance(payload, dict):
        spec = dict(payload)
    else:
        raise ValueError("content must be text or an object")
    spec["format"] = fmt
    spec["title"] = _text(spec.get("title") or "Untitled document").strip() or "Untitled document"
    spec["subtitle"] = _text(spec.get("subtitle") or "").strip()
    sections = spec.get("sections") or []
    if not isinstance(sections, list):
        raise ValueError("sections must be a list")
    clean_sections = []
    for section in sections[:MAX_SECTIONS]:
        if isinstance(section, str):
            section = {"heading": "", "paragraphs": [section]}
        if not isinstance(section, dict):
            continue
        paragraphs = [_text(p) for p in (section.get("paragraphs") or []) if _text(p).strip()]
        bullets = [_text(b) for b in (section.get("bullets") or []) if _text(b).strip()]
        entry = {
            "heading": _text(section.get("heading") or "").strip(),
            "paragraphs": paragraphs,
            "bullets": bullets,
            "table": _table(section.get("table")),
        }
        if entry["heading"] or paragraphs or bullets or entry["table"]:
            clean_sections.append(entry)
    spec["sections"] = clean_sections
    return spec


def parse_markdown(text):
    """Parse Markdown-ish text into sections (headings, bullets, tables)."""
    sections = []
    current = {"heading": "", "paragraphs": [], "bullets": [], "table": None}
    table_lines = []

    def flush_table():
        nonlocal table_lines
        if len(table_lines) >= 2:
            rows = []
            for line in table_lines:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                rows.append(cells)
            header = rows[0]
            body = [r for r in rows[1:] if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
            current["table"] = {"headers": header, "rows": body}
        table_lines = []

    def flush_section():
        nonlocal current
        flush_table()
        if (current["heading"] or current["paragraphs"] or current["bullets"]
                or current.get("table")):
            sections.append(current)
        current = {"heading": "", "paragraphs": [], "bullets": [], "table": None}

    title = ""
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_table()
            continue
        if re.match(r"^\s*\|.*\|\s*$", line):
            table_lines.append(line)
            continue
        flush_table()
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()
            if level == 1 and not title:
                title = heading
            else:
                flush_section()
                current["heading"] = heading
            continue
        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            current["bullets"].append(m.group(1).strip())
            continue
        m = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if m:
            current["bullets"].append(m.group(1).strip())
            continue
        current["paragraphs"].append(line.strip())
    flush_section()
    return {"title": title or "Untitled document", "sections": sections}


# ------------------------------------------------------------------ writers

def _write_txt(spec, path):
    lines = [spec["title"]]
    if spec["subtitle"]:
        lines.append(spec["subtitle"])
    lines.append("")
    for section in spec["sections"]:
        if section["heading"]:
            lines.extend([section["heading"], ""])
        for para in section["paragraphs"]:
            lines.extend([para, ""])
        for bullet in section["bullets"]:
            lines.append(f"* {bullet}")
        if section["bullets"]:
            lines.append("")
        table = section.get("table")
        if table:
            if table["headers"]:
                lines.append(" | ".join(table["headers"]))
                lines.append("-" * 40)
            for row in table["rows"][:MAX_ROWS]:
                lines.append(" | ".join(_text(c) for c in row))
            lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def _write_md(spec, path):
    out = [f"# {spec['title']}", ""]
    if spec["subtitle"]:
        out.extend([f"_{spec['subtitle']}_", ""])
    for section in spec["sections"]:
        if section["heading"]:
            out.extend([f"## {section['heading']}", ""])
        for para in section["paragraphs"]:
            out.extend([para, ""])
        for bullet in section["bullets"]:
            out.append(f"- {bullet}")
        if section["bullets"]:
            out.append("")
        table = section.get("table")
        if table:
            headers = table["headers"] or [f"Column {i + 1}" for i in range(
                max((len(r) for r in table["rows"]), default=0))]
            out.append("| " + " | ".join(headers) + " |")
            out.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in table["rows"][:MAX_ROWS]:
                cells = [_text(c).replace("|", "\\|") for c in row]
                out.append("| " + " | ".join(cells) + " |")
            out.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out).rstrip() + "\n")


def _write_csv(spec, path):
    table = None
    for section in spec["sections"]:
        if section.get("table"):
            table = section["table"]
            break
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if table is None:
            writer.writerow(["title", "content"])
            writer.writerow([spec["title"], " ".join(
                p for s in spec["sections"] for p in s["paragraphs"])])
            return
        if table["headers"]:
            writer.writerow(table["headers"])
        for row in table["rows"][:MAX_ROWS]:
            writer.writerow([_text(c) for c in row])


def _write_xlsx(spec, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    default = wb.active
    default.title = "Summary"
    default["A1"] = spec["title"]
    default["A1"].font = Font(bold=True, size=14)
    row = 3
    if spec["subtitle"]:
        default["A2"] = spec["subtitle"]
    for section in spec["sections"]:
        if section["heading"]:
            default.cell(row=row, column=1, value=section["heading"]).font = Font(bold=True)
            row += 1
        for para in section["paragraphs"]:
            default.cell(row=row, column=1, value=para)
            row += 1
        for bullet in section["bullets"]:
            default.cell(row=row, column=1, value=f"- {bullet}")
            row += 1
        row += 1
    default.column_dimensions["A"].width = 72

    sheets = spec.get("sheets") or []
    if not isinstance(sheets, list):
        sheets = []
    if not sheets:
        # Every workbook gets a real data sheet: the first table or a summary.
        table = next((s["table"] for s in spec["sections"] if s.get("table")), None)
        sheets = [{"name": "Data", "table": table or {
            "headers": ["Item", "Detail"],
            "rows": [[s["heading"] or "Section", " ".join(s["paragraphs"])]
                     for s in spec["sections"]] or [["Note", "No data supplied"]],
        }}]

    for spec_sheet in sheets[:20]:
        if not isinstance(spec_sheet, dict):
            continue
        name = re.sub(r"[\\/*?:\[\]]", " ", _text(spec_sheet.get("name") or "Data"))[:31] or "Data"
        ws = wb.create_sheet(name if name not in wb.sheetnames else (name[:28] + "_2"))
        table = spec_sheet.get("table") or spec_sheet
        headers = [_text(h) for h in (table.get("headers") or [])]
        rows = table.get("rows") or []
        start = 1
        if headers:
            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=header)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="3C58C4")
                cell.alignment = Alignment(horizontal="center")
            start = 2
        for r, values in enumerate(rows[:MAX_ROWS], start):
            for c, value in enumerate(values if isinstance(values, (list, tuple)) else [values], 1):
                ws.cell(row=r, column=c, value=_cell_value(value))
        formulas = spec_sheet.get("formulas") or []
        for item in formulas[:500]:
            if not isinstance(item, dict):
                continue
            ref = _text(item.get("cell") or "").strip()
            formula = _text(item.get("formula") or "").strip()
            if re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", ref) and formula.startswith("="):
                ws[ref] = formula
        for col in range(1, max(len(headers), 1) + 1):
            width = max((len(_text(ws.cell(row=r, column=col).value))
                         for r in range(1, ws.max_row + 1)), default=8)
            ws.column_dimensions[get_column_letter(col)].width = min(52, max(10, width + 3))
        ws.freeze_panes = "A2" if headers else "A1"
    if "Summary" in wb.sheetnames and len(wb.sheetnames) > 1 and not spec["sections"]:
        del wb["Summary"]
    wb.save(path)


def _write_docx(spec, path):
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading(spec["title"], level=0)
    if spec["subtitle"]:
        para = doc.add_paragraph(spec["subtitle"])
        para.runs[0].italic = True
    for section in spec["sections"]:
        if section["heading"]:
            doc.add_heading(section["heading"], level=1)
        for text in section["paragraphs"]:
            doc.add_paragraph(text)
        for bullet in section["bullets"]:
            doc.add_paragraph(bullet, style="List Bullet")
        table = section.get("table")
        if table and (table["headers"] or table["rows"]):
            columns = len(table["headers"]) or max((len(r) for r in table["rows"]), default=1)
            table_obj = doc.add_table(rows=1, cols=columns)
            table_obj.style = "Table Grid"
            header_cells = table_obj.rows[0].cells
            for i in range(columns):
                value = table["headers"][i] if i < len(table["headers"]) else ""
                header_cells[i].text = _text(value)
                for paragraph in header_cells[i].paragraphs:
                    for run in paragraph.runs:
                        run.bold = True
            for row in table["rows"][:MAX_ROWS]:
                cells = table_obj.add_row().cells
                for i in range(columns):
                    cells[i].text = _text(row[i]) if i < len(row) else ""
            doc.add_paragraph()
    for section in doc.sections:
        for paragraph in section.footer.paragraphs:
            paragraph.text = "Generated by Harry"
            for run in paragraph.runs:
                run.font.size = Pt(8)
    doc.save(path)


def _pdf_styles():
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib import colors

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "HarryTitle", parent=styles["Title"], fontSize=22, leading=26,
        spaceAfter=6, textColor=colors.HexColor("#17243b"),
    ))
    styles.add(ParagraphStyle(
        "HarrySubtitle", parent=styles["Normal"], fontSize=11, leading=15,
        textColor=colors.HexColor("#566783"), spaceAfter=14,
    ))
    styles.add(ParagraphStyle(
        "HarryH2", parent=styles["Heading2"], fontSize=14, leading=18,
        spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#3c58c4"),
    ))
    return styles


def _escape(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _write_pdf(spec, path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)
    from reportlab.lib import colors

    styles = _pdf_styles()
    doc = SimpleDocTemplate(path, pagesize=A4, title=spec["title"],
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    story = [Paragraph(_escape(spec["title"]), styles["HarryTitle"])]
    if spec["subtitle"]:
        story.append(Paragraph(_escape(spec["subtitle"]), styles["HarrySubtitle"]))
    for section in spec["sections"]:
        if section["heading"]:
            story.append(Paragraph(_escape(section["heading"]), styles["HarryH2"]))
        for text in section["paragraphs"]:
            story.append(Paragraph(_escape(text), styles["BodyText"]))
            story.append(Spacer(1, 4))
        for bullet in section["bullets"]:
            story.append(Paragraph("&bull; " + _escape(bullet), styles["BodyText"]))
        table = section.get("table")
        if table and (table["headers"] or table["rows"]):
            data = []
            if table["headers"]:
                data.append([Paragraph(f"<b>{_escape(h)}</b>", styles["BodyText"])
                             for h in table["headers"]])
            for row in table["rows"][:2000]:
                data.append([Paragraph(_escape(_text(c)), styles["BodyText"]) for c in row])
            if data:
                table_obj = Table(data, repeatRows=1 if table["headers"] else 0)
                table_obj.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b9c6dd")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eefb")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ]))
                story.append(Spacer(1, 6))
                story.append(table_obj)
        story.append(Spacer(1, 8))
    if len(story) == 1:
        story.append(Paragraph("This document has no body content.", styles["BodyText"]))

    def _footer(canvas, document):
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#66779a"))
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"Page {document.page}")
        canvas.drawString(20 * mm, 10 * mm, "Generated by Harry")

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)


def _write_pptx(spec, path):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = spec["title"]
    if spec["subtitle"] and len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = spec["subtitle"]

    slides = spec.get("slides") or []
    if not isinstance(slides, list):
        slides = []
    if not slides:
        # Build one slide per document section so the deck is never empty.
        slides = [{"title": s["heading"] or spec["title"],
                   "bullets": s["bullets"] or s["paragraphs"]}
                  for s in spec["sections"]]
    if not slides:
        slides = [{"title": spec["title"], "bullets": ["No content supplied."]}]

    for item in slides[:MAX_SLIDES]:
        if isinstance(item, str):
            item = {"title": item, "bullets": []}
        if not isinstance(item, dict):
            continue
        layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = _text(item.get("title") or spec["title"])
        bullets = [_text(b) for b in (item.get("bullets") or []) if _text(b).strip()]
        if not bullets and item.get("body"):
            bullets = [_text(item.get("body"))]
        body = slide.placeholders[1].text_frame
        body.clear()
        if bullets:
            body.text = bullets[0]
            for bullet in bullets[1:]:
                paragraph = body.add_paragraph()
                paragraph.text = bullet
        for paragraph in body.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(16)
        notes = _text(item.get("notes") or "").strip()
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    prs.save(path)


_WRITERS = {
    "txt": _write_txt, "md": _write_md, "csv": _write_csv,
    "xlsx": _write_xlsx, "docx": _write_docx, "pdf": _write_pdf,
    "pptx": _write_pptx,
}


# ------------------------------------------------------------------ validation

def validate(path, fmt):
    """Reopen a generated file with the library that wrote it."""
    size = os.path.getsize(path)
    if size <= 0:
        raise ValueError("generated file is empty")
    info = {"bytes": size}
    if fmt == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("generated PDF is unexpectedly encrypted")
        pages = len(reader.pages)
        text = "".join((page.extract_text() or "") for page in reader.pages[:5])
        if pages < 1:
            raise ValueError("generated PDF has no pages")
        info.update(pages=pages, chars=len(text.strip()))
    elif fmt == "docx":
        from docx import Document
        doc = Document(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        if not paragraphs:
            raise ValueError("generated DOCX has no text")
        info.update(paragraphs=len(paragraphs),
                    tables=len(doc.tables), chars=sum(len(p) for p in paragraphs))
    elif fmt == "xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=False)
        sheets = wb.sheetnames
        cells = 0
        formulas = 0
        for name in sheets:
            for row in wb[name].iter_rows():
                for cell in row:
                    if cell.value is not None:
                        cells += 1
                        if isinstance(cell.value, str) and cell.value.startswith("="):
                            formulas += 1
        if cells == 0:
            raise ValueError("generated XLSX has no cells")
        info.update(sheets=sheets, cells=cells, formulas=formulas)
    elif fmt == "pptx":
        from pptx import Presentation
        prs = Presentation(path)
        slides = len(prs.slides)
        texts = 0
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    texts += 1
        if slides < 1:
            raise ValueError("generated PPTX has no slides")
        info.update(slides=slides, text_shapes=texts)
    elif fmt == "csv":
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if not rows:
            raise ValueError("generated CSV has no rows")
        info.update(rows=len(rows), columns=max(len(r) for r in rows))
    else:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            raise ValueError("generated text file is empty")
        info.update(lines=len(text.splitlines()), chars=len(text))
    return info


# ------------------------------------------------------------------ public API

def generate(fmt, title, content=None, spec=None, subtitle="", filename=None,
             owner="local"):
    """Write one document and return its metadata (validated).

    ``content`` may be a string (Markdown-ish) or a dict spec.
    ``spec`` (dict) wins when both are given.
    """
    fmt = _normalise_format(fmt)
    payload = spec if isinstance(spec, dict) else (content or "")
    document_spec = normalise_spec(payload, fmt)
    if title:
        document_spec["title"] = _text(title).strip() or document_spec["title"]
    if subtitle:
        document_spec["subtitle"] = _text(subtitle).strip()

    root = documents_root()
    os.makedirs(root, exist_ok=True)
    doc_id = f"doc_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    stem = _slug(filename or document_spec["title"])
    stored_name = f"{stem}-{doc_id[-6:]}.{fmt}"
    path = os.path.join(root, stored_name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(root):
        raise ValueError("refusing to write outside the documents folder")

    started = time.perf_counter()
    _WRITERS[fmt](document_spec, path)
    if os.path.getsize(path) > MAX_BYTES:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise ValueError("generated document exceeds 25 MB")
    info = validate(path, fmt)
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    documents = _load_index()
    record = {
        "id": doc_id,
        "name": f"{_slug(document_spec['title'], 'document')}.{fmt}",
        "format": fmt,
        "path": path,
        "bytes": info["bytes"],
        "title": document_spec["title"],
        "created": time.time(),
        "elapsed_ms": elapsed_ms,
        "owner": owner,
        "validation": info,
    }
    documents[doc_id] = record
    _save_index(documents)
    log.info("generated %s (%s bytes, %sms) at %s", fmt, info["bytes"], elapsed_ms, path)
    try:
        from . import events
        events.emit("document_ready", {k: record[k] for k in
                                       ("id", "name", "format", "bytes", "title")})
    except Exception:
        pass
    return record


def listing(owner=None, limit=50):
    documents = _load_index()
    items = list(documents.values())
    if owner:
        items = [d for d in items if d.get("owner") == owner]
    items.sort(key=lambda d: d.get("created", 0), reverse=True)
    return items[:max(1, min(int(limit or 50), 200))]


def get(document_id, owner=None):
    """Look a document up by id. Ids are opaque; paths never come from clients."""
    doc = _load_index().get(str(document_id or ""))
    if not doc:
        return None
    if owner and doc.get("owner") != owner:
        return None
    path = doc.get("path") or ""
    root = os.path.abspath(documents_root())
    if not os.path.abspath(path).startswith(root + os.sep) or not os.path.exists(path):
        return None
    return doc


def preview(document_id, owner=None, max_chars=4000):
    doc = get(document_id, owner=owner)
    if not doc:
        return None
    fmt = doc["format"]
    text = ""
    try:
        if fmt in ("txt", "md", "csv"):
            with open(doc["path"], "r", encoding="utf-8", errors="replace") as f:
                text = f.read(max_chars)
        elif fmt == "docx":
            from docx import Document
            text = "\n".join(p.text for p in Document(doc["path"]).paragraphs if p.text.strip())
        elif fmt == "xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(doc["path"], data_only=True)
            rows = []
            for name in wb.sheetnames[:2]:
                rows.append(f"[{name}]")
                for row in wb[name].iter_rows(max_row=25, values_only=True):
                    if any(c is not None for c in row):
                        rows.append(" | ".join("" if c is None else str(c) for c in row))
            text = "\n".join(rows)
        elif fmt == "pptx":
            from pptx import Presentation
            chunks = []
            for i, slide in enumerate(Presentation(doc["path"]).slides, 1):
                for shape in slide.shapes:
                    if shape.has_text_frame and shape.text_frame.text.strip():
                        chunks.append(f"Slide {i}: {shape.text_frame.text.strip()}")
            text = "\n".join(chunks)
        elif fmt == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(doc["path"])
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:3])
    except Exception as e:
        log.warning("preview failed for %s: %s", document_id, e)
        text = ""
    return {**doc, "preview": text[:max_chars]}


# ------------------------------------------------------------------ tools

def _spec_from_model(instructions, fmt, title):
    """Ask the local model for a document structure. Raises if unavailable."""
    from . import llm
    shape = {
        "pdf": "sections with heading, paragraphs, bullets and optional table",
        "docx": "sections with heading, paragraphs, bullets and optional table",
        "xlsx": "sheets: [{name, headers, rows, formulas:[{cell,formula}]}]",
        "pptx": "slides: [{title, bullets, notes}]",
        "csv": "one sections entry containing a table {headers, rows}",
        "txt": "sections with heading, paragraphs, bullets",
        "md": "sections with heading, paragraphs, bullets",
    }[fmt]
    prompt = (
        "Return ONLY JSON describing a document. Schema: "
        '{"title": str, "subtitle": str, "sections": [{"heading": str, '
        '"paragraphs": [str], "bullets": [str], "table": {"headers": [str], '
        '"rows": [[str]]}}], "sheets": [...], "slides": [...]}. '
        f"For a {fmt} document use {shape}. Write complete, specific content "
        "with no placeholders.\n\nRequest: " + str(instructions)
    )
    raw = llm.chat([
        {"role": "system", "content": "You produce structured JSON for document generation. "
                                      "Never output commentary, only JSON."},
        {"role": "user", "content": prompt},
    ], task_class=llm.CHAT, options={"num_predict": 1800})
    match = re.search(r"[{].*[}]", raw or "", re.DOTALL)
    if not match:
        raise RuntimeError("the model did not return a document structure")
    spec = json.loads(match.group(0))
    if title and not spec.get("title"):
        spec["title"] = title
    return spec


def generate_document(args):
    fmt = args.get("format") or args.get("type") or ""
    title = _text(args.get("title") or "").strip()
    instructions = _text(args.get("instructions") or args.get("prompt") or "").strip()
    content = args.get("content")
    spec = args.get("spec")
    method = "supplied"
    if spec is None and content is None:
        if not instructions:
            return {"ok": False, "error": "provide content, spec or instructions"}
        try:
            spec = _spec_from_model(instructions, _normalise_format(fmt), title)
            method = "model"
        except Exception as e:
            return {"ok": False, "error": f"could not draft the document: {e}"}
    try:
        record = generate(fmt, title or (spec or {}).get("title", ""),
                          content=content, spec=spec,
                          subtitle=_text(args.get("subtitle") or ""),
                          filename=args.get("filename"), owner="local")
    except Exception as e:
        log.warning("document generation failed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "id": record["id"], "format": record["format"],
            "name": record["name"], "bytes": record["bytes"],
            "title": record["title"], "method": method,
            "validation": record["validation"],
            "download": f"/api/documents/{record['id']}/download",
            "message": f"Created {record['name']} ({record['bytes']} bytes, validated)."}


def register_document_generation_tools(registry):
    from .tools import Tool
    from .security import SAFE

    registry.register(Tool(
        "generate_document",
        "Create a real PDF/DOCX/XLSX/PPTX/CSV/TXT/MD file from text or instructions",
        generate_document, SAFE,
        {"format": "str", "title": "str", "instructions": "str",
         "content": "str|object", "filename": "str"},
    ))
    registry.register(Tool(
        "list_documents",
        "List generated documents with their download links",
        lambda args: {"ok": True, "documents": [
            {k: d[k] for k in ("id", "name", "format", "bytes", "created")}
            for d in listing(limit=int(args.get("limit", 20) or 20))]},
        SAFE, {"limit": "int"},
    ))
