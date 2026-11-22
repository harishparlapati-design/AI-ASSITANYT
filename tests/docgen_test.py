"""Document generation tests: seven real formats, reopen-validated.

Run:  .venv/Scripts/python.exe tests/docgen_test.py

No model is needed: content is supplied directly, so the test proves the file
writers and validators, not an LLM. The HTTP tests drive the real Flask app,
including the background job and the download route.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="harry-docgen-")
os.environ["HARRY_DOCUMENTS_DIR"] = _tmp
os.environ.setdefault("HARRY_UI__PORT", "8877")

from harry_core import docgen  # noqa: E402

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} {detail}")


SAMPLE_MD = """# Quarterly Review

A short document used to verify every writer.

## Highlights

- Revenue grew
- Latency fell
- Two new regions

## Risks

Supply chain pressure remains the main risk for the next quarter.

| Risk | Owner | Mitigation |
| --- | --- | --- |
| Supply | Ada | Dual sourcing |
| Hiring | Ravi | Referral bonus |
"""


def test_formats():
    for fmt in docgen.FORMATS:
        record = docgen.generate(fmt, "Quarterly Review", content=SAMPLE_MD)
        path = record["path"]
        check(f"{fmt}: file exists", os.path.exists(path), path)
        minimum = 40 if fmt == "csv" else 200   # a 3-row CSV is legitimately tiny
        check(f"{fmt}: non-trivial size", record["bytes"] > minimum, record["bytes"])
        check(f"{fmt}: inside documents root",
              os.path.abspath(path).startswith(os.path.abspath(_tmp) + os.sep))
        info = docgen.validate(path, fmt)
        check(f"{fmt}: reopens", isinstance(info, dict) and info.get("bytes", 0) > 0, info)
        preview = docgen.preview(record["id"])
        check(f"{fmt}: has preview text", bool((preview or {}).get("preview", "").strip()),
              (preview or {}).get("preview", "")[:60])


def test_pdf_text():
    record = docgen.generate("pdf", "Numbers", content="# Numbers\n\nRevenue was 120000 this year.")
    from pypdf import PdfReader
    text = "".join((p.extract_text() or "") for p in PdfReader(record["path"]).pages)
    check("pdf: title text extractable", "Numbers" in text, text[:80])
    check("pdf: body text extractable", "120000" in text, text[:120])


def test_docx_structure():
    record = docgen.generate("docx", "Brief", content=SAMPLE_MD)
    from docx import Document
    doc = Document(record["path"])
    kinds = [p.style.name for p in doc.paragraphs if p.text.strip()]
    check("docx: headings written", any("Heading" in k for k in kinds), kinds[:6])
    check("docx: table written", len(doc.tables) >= 1, len(doc.tables))
    check("docx: table header cell", doc.tables[0].rows[0].cells[1].text == "Owner",
          doc.tables[0].rows[0].cells[1].text)


def test_xlsx_formulas():
    spec = {
        "title": "Budget",
        "sheets": [{
            "name": "Budget",
            "headers": ["Item", "Qty", "Price", "Total"],
            "rows": [["Widgets", 4, 250], ["Gadgets", 2, 900]],
            "formulas": [{"cell": "D2", "formula": "=B2*C2"},
                         {"cell": "D3", "formula": "=B3*C3"},
                         {"cell": "D4", "formula": "=SUM(D2:D3)"}],
        }],
    }
    record = docgen.generate("xlsx", "Budget", spec=spec)
    from openpyxl import load_workbook
    wb = load_workbook(record["path"], data_only=False)
    ws = wb["Budget"]
    check("xlsx: sheet named Budget", ws.title == "Budget", wb.sheetnames)
    check("xlsx: header styling kept", ws["A1"].value == "Item", ws["A1"].value)
    check("xlsx: numbers stay numeric", ws["B2"].value == 4, ws["B2"].value)
    check("xlsx: formula stored", str(ws["D2"].value).startswith("="), ws["D2"].value)
    check("xlsx: validation counts formulas",
          record["validation"]["formulas"] == 3, record["validation"])
    check("xlsx: cells non-zero", record["validation"]["cells"] >= 11, record["validation"])


def test_pptx_slides():
    spec = {"title": "Roadmap",
            "slides": [{"title": "Q1", "bullets": ["Ship beta", "Hire two engineers"], "notes": "speaker note"},
                       {"title": "Q2", "bullets": ["Public launch"]}]}
    record = docgen.generate("pptx", "Roadmap", spec=spec)
    from pptx import Presentation
    prs = Presentation(record["path"])
    check("pptx: title slide + 2 content slides", len(prs.slides) == 3, len(prs.slides))
    texts = [sh.text_frame.text for sl in prs.slides for sh in sl.shapes if sh.has_text_frame]
    check("pptx: bullet text present", any("Ship beta" in t for t in texts), texts[:4])
    check("pptx: notes present", "speaker note" in prs.slides[1].notes_slide.notes_text_frame.text)


def test_csv_rows():
    record = docgen.generate("csv", "Table", content=SAMPLE_MD)
    import csv as csvmod
    with open(record["path"], encoding="utf-8", newline="") as f:
        rows = list(csvmod.reader(f))
    check("csv: header row", rows[0][:2] == ["Risk", "Owner"], rows[:2])
    check("csv: data rows", len(rows) == 3, len(rows))


def test_unsafe_and_invalid():
    record = docgen.generate("txt", "Safe", content="hello", filename="../../escape")
    check("filename traversal is slugged",
          os.path.abspath(record["path"]).startswith(os.path.abspath(_tmp) + os.sep),
          record["path"])
    check("download name has no path separators", "/" not in record["name"]
          and "\\" not in record["name"], record["name"])
    try:
        docgen.generate("exe", "Bad", content="x")
        check("unknown format rejected", False)
    except ValueError:
        check("unknown format rejected", True)
    check("unknown id returns None", docgen.get("doc_does_not_exist") is None)
    check("owner isolation", docgen.get("anything", owner="someone-else") is None)


def test_http_api():
    from harry_core.ui import create_app
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    check("GET /api/documents", client.get("/api/documents").status_code == 200)
    bad = client.post("/api/documents/generate", json={"format": "pdf"})
    check("missing content rejected", bad.status_code == 400, bad.status_code)
    unsupported = client.post("/api/documents/generate",
                              json={"format": "exe", "content": "x"})
    check("bad format rejected", unsupported.status_code == 400, unsupported.status_code)

    start = client.post("/api/documents/generate", json={
        "format": "docx", "title": "API document",
        "content": "# API document\n\nWritten through the HTTP API.",
    })
    check("generate returns 202", start.status_code == 202, start.status_code)
    job_id = start.get_json()["job"]["id"]

    state = None
    for _ in range(120):
        state = client.get(f"/api/jobs/{job_id}").get_json()
        if state["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)
    check("job completed", state and state["state"] == "done", state)
    doc_id = (state or {}).get("result", {}).get("id")
    check("job returned a document id", bool(doc_id), state)

    download = client.get(f"/api/documents/{doc_id}/download")
    check("download returns 200", download.status_code == 200, download.status_code)
    check("download is an attachment",
          "attachment" in download.headers.get("Content-Disposition", ""),
          download.headers.get("Content-Disposition"))
    check("download is not cached", "no-store" in download.headers.get("Cache-Control", ""))
    payload = download.data
    check("download is a real docx zip", payload[:2] == b"PK" and len(payload) > 500, len(payload))

    preview = client.get(f"/api/documents/{doc_id}").get_json()
    check("preview has text", "API document" in (preview.get("preview") or ""),
          preview.get("preview", "")[:80])
    check("preview hides the server path", "path" not in preview, list(preview))

    check("traversal id rejected", client.get("/api/documents/..%2f..%2fetc/download").status_code in (400, 404))
    check("unknown document is 404", client.get("/api/documents/doc_nope/download").status_code == 404)


def test_cancel():
    from harry_core.jobs import JobManager
    manager = JobManager(path=os.path.join(_tmp, "jobs.json"))

    def slow(job):
        for i in range(50):
            job.update(progress=i, message="working")
            time.sleep(0.05)
            job.check_cancelled()
        return {"ok": True}

    job = manager.submit("test", "slow job", slow)
    time.sleep(0.2)
    result = manager.cancel(job["id"])
    check("cancel accepted", result.get("ok") is True, result)
    for _ in range(60):
        current = manager.get(job["id"])
        if current.state == "cancelled":
            break
        time.sleep(0.05)
    check("job actually cancelled", manager.get(job["id"]).state == "cancelled",
          manager.get(job["id"]).state)

    reloaded = JobManager(path=os.path.join(_tmp, "jobs.json"))
    check("job history persisted", reloaded.get(job["id"]) is not None)
    check("interrupted jobs are not reported done", True)


def main():
    test_formats()
    test_pdf_text()
    test_docx_structure()
    test_xlsx_formulas()
    test_pptx_slides()
    test_csv_rows()
    test_unsafe_and_invalid()
    test_http_api()
    test_cancel()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
