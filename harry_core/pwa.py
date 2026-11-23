"""Installable web app (PWA) assets.

The dashboard is served by Flask, so these are three small routes instead of a
static build step:

    /manifest.webmanifest   app identity, icons, shortcuts
    /sw.js                  offline shell cache; API traffic is never cached
    /icons/<size>.png       generated icons (192/512/maskable/apple-touch)

Icons are drawn once with Pillow and kept in memory, so there are no binary
assets in Git and no missing-icon 404s.
"""

import io
import threading

THEME = "#0b1526"
ACCENT = "#7c9dff"
NAME = "Harry AI"
SHORT = "Harry"

_icon_cache = {}
_icon_lock = threading.Lock()


def manifest(start_url="/"):
    return {
        "name": NAME,
        "short_name": SHORT,
        "description": "Personal AI assistant: conversation, voice, vision, files and tasks.",
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": THEME,
        "theme_color": THEME,
        "categories": ["productivity", "utilities"],
        "icons": [
            {"src": "/icons/192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Conversation", "url": "/"},
        ],
    }


def icon_png(size=512, maskable=False):
    """Draw the Harry mark: a glowing orb with the H monogram."""
    key = (int(size), bool(maskable))
    with _icon_lock:
        if key in _icon_cache:
            return _icon_cache[key]
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (size, size), THEME)
    draw = ImageDraw.Draw(image)
    inset = 0 if maskable else max(2, size // 16)
    # soft concentric rings, then the monogram
    for i in range(10, 0, -1):
        radius = int(size * (0.30 + i * 0.021))
        shade = (20 + i * 4, 34 + i * 6, 62 + i * 9)
        box = (size // 2 - radius, size // 2 - radius, size // 2 + radius, size // 2 + radius)
        draw.ellipse(box, fill=shade)
    ring = int(size * 0.335)
    draw.ellipse((size // 2 - ring, size // 2 - ring, size // 2 + ring, size // 2 + ring),
                 outline=ACCENT, width=max(2, size // 64))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", int(size * 0.42))
    except Exception:
        try:
            font = ImageFont.truetype("arialbd.ttf", int(size * 0.42))
        except Exception:
            font = ImageFont.load_default()
    text = "H"
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((size - (box[2] - box[0])) / 2 - box[0],
               (size - (box[3] - box[1])) / 2 - box[1]),
              text, font=font, fill="#eaf1ff")
    if inset:
        image = image.crop((inset, inset, size - inset, size - inset)).resize((size, size))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    with _icon_lock:
        _icon_cache[key] = data
    return data


SERVICE_WORKER = """/* Harry AI service worker.
   Caches only the application shell. Every /api request stays on the network so
   private conversations, documents and media are never stored by the browser
   cache as a side effect of this worker. */
const CACHE = 'harry-shell-v1';
const SHELL = ['/', '/manifest.webmanifest', '/icons/192.png', '/icons/512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;   // live, private data: network only
  if (url.pathname === '/sw.js') return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put('/', copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match('/').then((hit) => hit || new Response(
          '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
          '<body style="font-family:system-ui;background:#0b1526;color:#e9effb;padding:28px">' +
          '<h1>Harry is offline</h1><p>The app shell is installed but the server cannot be ' +
          'reached. Reconnect to continue; nothing was lost.</p></body>',
          {headers: {'Content-Type': 'text/html; charset=utf-8'}}
        )))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((hit) => hit || fetch(request).then((response) => {
      if (response.ok && (request.destination === 'image' || request.destination === 'font')) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
      }
      return response;
    }))
  );
});
"""
