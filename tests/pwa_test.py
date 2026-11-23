"""PWA tests: manifest, service worker, icons and install wiring.

Run:  .venv/Scripts/python.exe tests/pwa_test.py
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} {detail}")


def main():
    from harry_core.ui import create_app
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    manifest_response = client.get("/manifest.webmanifest")
    check("manifest served", manifest_response.status_code == 200, manifest_response.status_code)
    check("manifest content type",
          "application/manifest+json" in manifest_response.headers.get("Content-Type", ""),
          manifest_response.headers.get("Content-Type"))
    manifest = manifest_response.get_json()
    check("manifest names the app", manifest.get("name") and manifest.get("short_name"), manifest.get("name"))
    check("manifest starts at the root", manifest.get("start_url") == "/", manifest.get("start_url"))
    check("manifest is standalone", manifest.get("display") == "standalone", manifest.get("display"))
    check("manifest has theme colours",
          manifest.get("theme_color") and manifest.get("background_color"), manifest.get("theme_color"))
    sizes = {icon.get("sizes") for icon in manifest.get("icons", [])}
    check("manifest declares 192 and 512 icons", {"192x192", "512x512"} <= sizes, sizes)
    check("manifest declares a maskable icon",
          any("maskable" in (icon.get("purpose") or "") for icon in manifest.get("icons", [])),
          manifest.get("icons"))

    for icon in manifest.get("icons", []):
        served = client.get(icon["src"])
        check(f"icon {icon['src']} resolves", served.status_code == 200, served.status_code)
        check(f"icon {icon['src']} is a PNG", served.data[:8] == b"\x89PNG\r\n\x1a\n",
              served.data[:8])

    png = client.get("/icons/192.png")
    from PIL import Image
    image = Image.open(io.BytesIO(png.data))
    check("192 icon has the declared size", image.size == (192, 192), image.size)
    large = Image.open(io.BytesIO(client.get("/icons/512.png").data))
    check("512 icon has the declared size", large.size == (512, 512), large.size)
    check("apple touch icon served", client.get("/icons/180.png").status_code == 200)
    check("favicon served", client.get("/icons/favicon.ico").status_code == 200)
    check("unknown icon is 404", client.get("/icons/999.png").status_code == 404)
    check("icon path traversal refused", client.get("/icons/../config.json").status_code == 404)

    sw = client.get("/sw.js")
    check("service worker served", sw.status_code == 200, sw.status_code)
    check("service worker is javascript",
          "javascript" in sw.headers.get("Content-Type", ""), sw.headers.get("Content-Type"))
    check("service worker is not cached", sw.headers.get("Cache-Control") == "no-cache",
          sw.headers.get("Cache-Control"))
    body = sw.get_data(as_text=True)
    check("service worker scoped to the root",
          sw.headers.get("Service-Worker-Allowed") == "/")
    check("service worker caches the shell", "harry-shell" in body)
    check("service worker skips API requests", "startsWith('/api/')" in body)
    check("service worker has an offline fallback", "Harry is offline" in body)
    check("service worker handles navigation", "request.mode === 'navigate'" in body)

    html = client.get("/").get_data(as_text=True)
    check("page links the manifest", 'rel="manifest" href="/manifest.webmanifest"' in html)
    check("page sets theme colour", 'name="theme-color"' in html)
    check("page has an apple touch icon", 'rel="apple-touch-icon"' in html)
    check("page is mobile ready", "viewport-fit=cover" in html)
    check("page registers the service worker", "navigator.serviceWorker.register('/sw.js')" in html)
    check("page has an install button", 'id="installApp"' in html)
    check("page shows offline state", 'id="netState"' in html)
    check("page handles beforeinstallprompt", "beforeinstallprompt" in html)
    check("page falls back to the browser menu", "Add to home screen" in html)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
