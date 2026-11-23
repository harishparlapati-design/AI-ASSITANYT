"""Screen sharing and vision tests.

Run:  .venv/Scripts/python.exe tests/vision_test.py

The analysis tests talk to the real local vision model when one is installed
(moondream here). They assert honest outcomes either way: if no provider exists
the call must report a clear error and a hint, never invented text.
"""

import base64
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from harry_core import screen, vision  # noqa: E402

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} {detail}")


def make_screen_png(text="INVOICE 4821"):
    image = Image.new("RGB", (900, 420), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 44)
    except Exception:
        font = ImageFont.load_default()
    draw.text((40, 40), text, fill="black", font=font)
    draw.text((40, 140), "Acme Corporation", fill="black", font=font)
    draw.text((40, 240), "Status: PAID", fill="black", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_status():
    status = vision.status()
    check("status reports availability", isinstance(status.get("available"), bool), status)
    check("status names a provider", status.get("provider") in ("ollama", "openai_compatible", "none"), status)
    check("status exposes a hint when unavailable",
          status["available"] or bool(status.get("hint")), status)
    check("status lists vision models", isinstance(status.get("installed_vision_models"), list))


def test_analysis():
    png = make_screen_png()
    status = vision.status()
    if not status["available"]:
        result = vision.analyze(png, question="Read the text.")
        check("unavailable vision fails honestly", result.get("ok") is False, result)
        check("unavailable vision explains how to fix it", bool(result.get("hint")), result)
        print("     (skipped model assertions: no vision provider installed)")
        return
    started = time.perf_counter()
    result = vision.analyze(png, question="Read all the text in this image exactly.")
    elapsed = time.perf_counter() - started
    check("analysis succeeded", result.get("ok") is True, result)
    check("analysis used a real provider",
          str(result.get("method", "")).startswith(("ollama:", "openai_compatible")), result.get("method"))
    text = (result.get("text") or "").lower()
    check("analysis returned text", len(text) > 10, text[:80])
    check("analysis mentions content from the image",
          any(token in text for token in ("invoice", "4821", "acme", "paid", "document")), text[:160])
    print(f"     vision latency (model may be cold): {elapsed:.1f}s")

    cached_start = time.perf_counter()
    again = vision.analyze(png, question="Read all the text in this image exactly.")
    cached_ms = (time.perf_counter() - cached_start) * 1000
    check("repeat question is served from cache", again.get("cached") is True, again.get("cached"))
    check("cache is fast", cached_ms < 500, f"{cached_ms:.0f} ms")

    answers = vision.analyze(png, question="How many distinct lines of text are visible? Answer briefly.")
    check("a different question is not served from cache", answers.get("cached") is not True)


def test_bad_input():
    result = vision.analyze(b"not an image at all")
    check("garbage input rejected", result.get("ok") is False and result.get("kind") == "bad_image", result)
    huge = b"\xff\xd8\xff" + b"0" * (vision.MAX_INPUT_BYTES + 10)
    result = vision.analyze(huge)
    check("oversized image rejected", result.get("ok") is False
          and "larger" in str(result.get("error", "")), result.get("error"))


def test_screen_state_machine():
    session = screen.ScreenSession()
    png = make_screen_png("MEETING NOTES 77")

    result, status = session.accept_frame(png)
    check("frames refused while inactive", status == 409 and result["kind"] == "inactive", (status, result))

    started = session.start("browser")
    check("start activates sharing", started["state"] == "active" and started["sharing"] is True, started)

    result, status = session.accept_frame(png, question="Read all the text in this image exactly.")
    if vision.status()["available"]:
        check("active session analyses a frame", status == 200 and result.get("ok") is True,
              (status, result.get("error")))
        check("session keeps only text", isinstance(session.status()["last_analysis"], dict))
        check("session clears nothing before stop", bool(session.context_for_chat()), session.context_for_chat()[:60])
    else:
        check("active session reports provider failure", status == 503 and result.get("ok") is False, result)

    session.pause()
    time.sleep(screen.MIN_INTERVAL + 0.1)
    paused, status = session.accept_frame(png)
    check("paused session refuses frames", status == 409 and paused["kind"] == "paused", (status, paused))
    session.resume()
    check("resume restores active state", session.status()["state"] == "active")

    stopped = session.stop()
    check("stop deactivates", stopped["state"] == "inactive" and stopped["sharing"] is False, stopped)
    check("stop clears retained screen text", session.status()["last_analysis"] is None, session.status())
    check("context is empty after stop", session.context_for_chat() == "")


def test_rate_limit_gate():
    """The gate itself, with a stub provider so timing is deterministic."""
    import threading
    session = screen.ScreenSession()
    original = screen.vision.analyze
    try:
        screen.vision.analyze = lambda *a, **k: {"ok": True, "text": "stub answer",
                                                 "method": "stub"}
        session.start()
        first, first_status = session.accept_frame(b"frame-one")
        second, second_status = session.accept_frame(b"frame-two")
        check("first stub frame accepted", first_status == 200, (first_status, first))
        check("immediate second frame is rate limited",
              second_status == 429 and second.get("kind") == "rate_limited",
              (second_status, second))

        time.sleep(screen.MIN_INTERVAL + 0.1)

        def slow(*args, **kwargs):
            time.sleep(0.8)
            return {"ok": True, "text": "slow answer", "method": "stub"}

        screen.vision.analyze = slow
        results = {}

        def run():
            results["first"] = session.accept_frame(b"concurrent-one")

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.2)
        overlapping, overlapping_status = session.accept_frame(b"concurrent-two")
        worker.join()
        check("overlapping frame refused while analysing",
              overlapping_status == 429 and "already running" in overlapping.get("error", ""),
              (overlapping_status, overlapping.get("error")))
        check("the first concurrent frame still completed",
              results.get("first", (None, 0))[1] == 200, results.get("first"))
    finally:
        screen.vision.analyze = original
        session.stop()


def test_http_api():
    from harry_core.ui import create_app
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    session = screen.get_screen()
    session.stop()

    status = client.get("/api/screen").get_json()
    check("GET /api/screen", status.get("state") == "inactive", status.get("state"))
    check("screen status includes vision", "vision" in status, list(status))

    check("unknown action rejected",
          client.post("/api/screen", json={"action": "dance"}).status_code == 400)
    check("frame without image rejected",
          client.post("/api/screen/frame", json={}).status_code == 400)
    check("frame while inactive refused",
          client.post("/api/screen/frame", json={"image": "data:image/jpeg;base64,AAAA"}).status_code == 409)

    client.post("/api/screen", json={"action": "start"})
    bad = client.post("/api/screen/frame", json={"image": "data:image/jpeg;base64,!!!not-base64!!!"})
    check("invalid base64 rejected", bad.status_code == 400, bad.status_code)

    png = make_screen_png("API SCREEN")
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    result = client.post("/api/screen/frame", json={"image": data_url,
                                                     "question": "Read all the text in this image exactly."})
    if vision.status()["available"]:
        check("HTTP frame analysed", result.status_code == 200, result.status_code)
        body = result.get_json()
        check("HTTP frame returns text", bool((body.get("text") or "").strip()), body)
        check("HTTP frame reports method", bool(body.get("method")), body.get("method"))
    else:
        check("HTTP frame reports missing provider", result.status_code == 503, result.status_code)

    paused = client.post("/api/screen", json={"action": "pause"}).get_json()
    check("pause via API", paused.get("state") == "paused", paused.get("state"))
    client.post("/api/screen", json={"action": "resume"})
    stopped = client.post("/api/screen", json={"action": "stop"}).get_json()
    check("stop via API", stopped.get("state") == "inactive", stopped.get("state"))


def test_no_frame_storage():
    before = set(os.listdir(os.path.join(os.getcwd(), "data"))) if os.path.isdir(
        os.path.join(os.getcwd(), "data")) else set()
    png = make_screen_png("PRIVACY")
    session = screen.ScreenSession()
    session.start()
    session.accept_frame(png, question="Describe this image.")
    session.stop()
    after = set(os.listdir(os.path.join(os.getcwd(), "data"))) if os.path.isdir(
        os.path.join(os.getcwd(), "data")) else set()
    check("no files created for screen frames", before == after, (before, after))


def main():
    test_status()
    test_analysis()
    test_bad_input()
    test_screen_state_machine()
    test_rate_limit_gate()
    test_http_api()
    test_no_frame_storage()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
