"""
Download engine for the Sourcer app.

Three kinds of link, three behaviours:
  image   -> saved directly (also resolves Wikimedia Commons file pages to the original file)
  video   -> yt-dlp, with optional clip range so you only pull the seconds you need
  article -> headless Chrome: a tightly framed screenshot of the headline + lead image,
             plus a full-page capture, the og:image at full resolution, and a .md of the text
"""

import os
import re
import json
import time
import shutil
import threading
import subprocess
import concurrent.futures
from urllib.parse import urlparse, unquote

import requests


def _with_deadline(seconds, fn, *args, **kwargs):
    """Run fn and give up after `seconds` even if fn itself never returns.
    Some hosts (Wikimedia, when it's decided to throttle a client) accept the
    connection and then stall the response body indefinitely — requests'
    own timeout parameter doesn't reliably catch that here — so this is a
    belt-and-suspenders wall-clock deadline around the whole call. If it
    times out, the underlying thread is abandoned (leaked) rather than
    blocking the caller; that's a small cost next to freezing the whole
    download queue, which is what happened without this."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        raise RuntimeError(f"Timed out after {seconds}s (the host stopped responding mid-transfer)")
    finally:
        ex.shutdown(wait=False)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ------------------------------------------------------------------- wikimedia rate limiting
# Downloads run several at a time (see app.py's worker pool), but Wikimedia will 429 a client
# that opens several concurrent requests in a burst — and past that, it appears to throttle
# the connection itself rather than reject it, which can hang a download indefinitely. So
# every request to a Wikimedia host is serialized through one gate with a minimum spacing,
# plus a bounded retry with backoff on 429s.
#
# Two more things Wikimedia's own etiquette asks bulk/scripted clients for, both fixed here:
#   - a descriptive User-Agent identifying the tool, not a spoofed browser string
#   - fetching a sized rendition instead of hotlinking the full-resolution original
# (their 429 response literally says: "please... instead use thumbnail images in sizes
# listed on https://w.wiki/GHai"). See resolve_wikimedia() and WIKIMEDIA_UA below.
WIKIMEDIA_MIN_INTERVAL = 2.0   # seconds between successive Wikimedia requests, across all threads
WIKIMEDIA_MAX_RETRIES = 4
WIKIMEDIA_MAX_WIDTH = 2000     # request this rendition width instead of the full original
WIKIMEDIA_UA = "Sourcer/1.0 (personal local documentary research tool, single-user, low-volume; not a bot)"
_wikimedia_lock = threading.Lock()
_wikimedia_next_ok = [0.0]


def is_wikimedia_host(url):
    host = (urlparse(url).netloc or "").lower()
    return "wikimedia.org" in host or "wikipedia.org" in host


def _wikimedia_throttle():
    with _wikimedia_lock:
        now = time.monotonic()
        wait = _wikimedia_next_ok[0] - now
        if wait > 0:
            time.sleep(wait)
        _wikimedia_next_ok[0] = max(now, _wikimedia_next_ok[0]) + WIKIMEDIA_MIN_INTERVAL


def wikimedia_request(method, url, **kwargs):
    """requests.get/head, but gated to one-at-a-time with spacing, and retried
    with backoff on 429 (honoring Retry-After when the server sends one)."""
    kwargs.setdefault("timeout", (10, 30))
    last_exc = None
    for attempt in range(WIKIMEDIA_MAX_RETRIES):
        _wikimedia_throttle()
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                backoff = float(retry_after) if retry_after else (2 ** attempt) * 2
                time.sleep(backoff)
                last_exc = requests.HTTPError(f"429 rate-limited after {attempt + 1} tries", response=r)
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep((2 ** attempt))
    raise last_exc or RuntimeError("Wikimedia request failed for an unknown reason")

VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "tiktok.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com", "reddit.com",
    "bilibili.com", "twitch.tv", "streamable.com", "rumble.com",
)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}


# ----------------------------------------------------------------------------- helpers

def slugify(text, max_words=6, max_len=48):
    words = re.findall(r"[A-Za-z0-9]+", text.lower())[:max_words]
    s = "-".join(words)[:max_len].strip("-")
    return s or "untitled"


def parse_link_line(line):
    """
    Accepts, in any combination:
        https://example.com/photo.jpg
        [Gleason portrait](https://example.com/x.jpg)
        https://youtu.be/abc @1:30-2:05 | Ideal-X departure
    Returns {url, label, clip} or None.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    md = re.match(r"^\[(?P<label>[^\]]*)\]\((?P<url>\S+)\)\s*(?P<rest>.*)$", line)
    if md:
        url = md.group("url")
        rest = (md.group("label") + " " + md.group("rest")).strip()
    else:
        parts = line.split(None, 1)
        url = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

    clip = None
    m = re.search(r"@\s*(\d{1,2}:\d{2}(?::\d{2})?|\d+)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?|\d+)", rest)
    if m:
        clip = (m.group(1), m.group(2))
        rest = rest[:m.start()] + rest[m.end():]

    label = rest.replace("|", " ").strip()

    if not re.match(r"^https?://", url):
        return None
    return {"url": url, "label": label, "clip": clip}


BADGE_RE = re.compile(r"^(VIDEO(?:\s+[A-Z])?|IMAGE(?:\s+[A-Z])?|DOCUMENT|AUDIO)\b\s*", re.I)
CLIP_RE = re.compile(r"@\s*(\d{1,2}:\d{2}(?::\d{2})?|\d+)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?|\d+)")


def parse_pdf_shotlist(data):
    """Parse a production asset sheet (a PDF where each asset is a hyperlinked
    title, e.g. "VIDEO A Kyle Hill - ... @02:25-03:30") into the same
    {url, label, clip} shape parse_link_line() produces. The visible text is
    just a title — the real URL lives in the link annotation, not the text —
    so this reads links directly and pairs each one with the words sitting on
    its line to recover the label and any clip range.
    """
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    items = []
    for page in doc:
        words = page.get_text("words")  # x0,y0,x1,y1,word,block,line,word_no
        for link in page.get_links():
            uri = link.get("uri")
            if not uri or not re.match(r"^https?://", uri):
                continue
            r = fitz.Rect(link["from"])
            same_line = sorted(
                (w for w in words if w[1] < r.y1 and w[3] > r.y0),
                key=lambda w: w[0],
            )
            line = BADGE_RE.sub("", " ".join(w[4] for w in same_line)).strip()

            clip = None
            m = CLIP_RE.search(line)
            if m:
                clip = (m.group(1), m.group(2))
                line = (line[:m.start()] + line[m.end():]).strip()

            items.append({"url": uri, "label": line, "clip": clip})
    return items


def classify(url):
    host = (urlparse(url).netloc or "").lower().replace("www.", "")
    path = urlparse(url).path.lower()
    ext = os.path.splitext(path)[1]

    if "wikimedia.org" in host or "wikipedia.org" in host:
        if path.startswith("/wiki/file:") or path.startswith("/wiki/File:"):
            return "image"

    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS):
        return "video"
    if "archive.org" in host and "/details/" in path:
        return "video"

    # Ask the server before guessing.
    try:
        r = requests.head(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=12)
        ctype = r.headers.get("content-type", "").lower()
        if ctype.startswith("image/"):
            return "image"
        if ctype.startswith("video/"):
            return "video"
    except Exception:
        pass
    return "article"


def _unique(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{root}-{i}{ext}"):
        i += 1
    return f"{root}-{i}{ext}"


# ----------------------------------------------------------------------------- images

def resolve_wikimedia(url):
    """Turn a Commons/Wikipedia File: page into a downloadable image URL — a
    sized rendition capped at WIKIMEDIA_MAX_WIDTH rather than the full
    original, per Wikimedia's own guidance to bulk/scripted clients."""
    p = urlparse(url)
    title = unquote(p.path.split("/wiki/", 1)[1])
    api = f"{p.scheme}://{p.netloc}/w/api.php"
    r = wikimedia_request("GET", api, params={
        "action": "query", "titles": title, "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": WIKIMEDIA_MAX_WIDTH,
        "format": "json",
    }, headers={"User-Agent": WIKIMEDIA_UA})
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", {})
    for _, page in pages.items():
        info = (page.get("imageinfo") or [{}])[0]
        # thumburl is the sized rendition; fall back to the original only if
        # Wikimedia didn't generate one (e.g. the source is already small).
        if info.get("thumburl"):
            return info["thumburl"]
        if info.get("url"):
            return info["url"]
    raise RuntimeError("Could not resolve Wikimedia file page to an image")


def download_image(url, dest_dir, base, on_progress=None):
    # 90s covers the Wikimedia gate's own retry/backoff cycle (worst case
    # ~35s across 4 attempts) plus real transfer time for a large image.
    return _with_deadline(90, _download_image_impl, url, dest_dir, base, on_progress=on_progress)


def _download_image_impl(url, dest_dir, base, on_progress=None):
    wiki = is_wikimedia_host(url)
    if "wikimedia.org/wiki/File:" in url or "wikipedia.org/wiki/File:" in url:
        url = resolve_wikimedia(url)
        wiki = True

    if wiki:
        headers = {"User-Agent": WIKIMEDIA_UA}
        r = wikimedia_request("GET", url, headers=headers, stream=True)
    else:
        headers = {"User-Agent": UA, "Referer": f"{urlparse(url).scheme}://{urlparse(url).netloc}/"}
        r = requests.get(url, headers=headers, timeout=(10, 40), stream=True)
    r.raise_for_status()

    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in IMAGE_EXT:
        ext = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif", "image/svg+xml": ".svg", "image/avif": ".avif",
            "image/tiff": ".tif",
        }.get(ctype, ".jpg")

    total = int(r.headers.get("content-length") or 0)
    done = 0
    out = _unique(os.path.join(dest_dir, base + ext))
    with open(out, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(min(99, done / total * 100))
    if os.path.getsize(out) < 1024:
        os.remove(out)
        raise RuntimeError("File came back empty or blocked (hotlink protection)")
    if on_progress:
        on_progress(100)
    return [out]


# ----------------------------------------------------------------------------- video

YTDLP_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
FFMPEG_TIME_RE = re.compile(r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _clip_seconds(ts):
    """'02:25', '1:02:25', or a bare second count -> float seconds."""
    parts = [float(p) for p in str(ts).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def _resolve_ytdlp():
    """Locate the yt-dlp executable. A packaged build sets CRATE_YTDLP to the
    bundled standalone exe; a dev checkout falls back to whatever's on PATH."""
    exe = os.environ.get("CRATE_YTDLP")
    if exe and os.path.exists(exe):
        return exe
    return shutil.which("yt-dlp")


def _ffmpeg_dir():
    """Directory holding ffmpeg/ffprobe, if a packaged build bundled them.
    yt-dlp is handed this via --ffmpeg-location; None lets it search PATH."""
    d = os.environ.get("CRATE_FFMPEG_DIR")
    if d and os.path.isdir(d):
        return d
    return None


def download_video(url, dest_dir, base, clip=None, on_progress=None):
    exe = _resolve_ytdlp()
    if not exe:
        raise RuntimeError("yt-dlp is not available.")

    tmpl = os.path.join(dest_dir, base + ".%(ext)s")
    cmd = [
        exe, url,
        "-o", tmpl,
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--restrict-filenames",
        "--newline",
        "--write-thumbnail", "--convert-thumbnails", "jpg",
    ]
    ffdir = _ffmpeg_dir()
    if ffdir:
        cmd += ["--ffmpeg-location", ffdir]
    clip_duration = None
    if clip:
        cmd += ["--download-sections", f"*{clip[0]}-{clip[1]}", "--force-keyframes-at-cuts"]
        try:
            clip_duration = max(0.001, _clip_seconds(clip[1]) - _clip_seconds(clip[0]))
        except ValueError:
            clip_duration = None

    # CREATE_NO_WINDOW keeps a console window from flashing for each yt-dlp
    # call when Crate runs as a windowed (no-console) packaged app.
    creationflags = 0x08000000 if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, universal_newlines=True,
        creationflags=creationflags,
    )
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-20:]
        if on_progress:
            m = YTDLP_PROGRESS_RE.search(line)
            if m:
                on_progress(min(99, float(m.group(1))))
            elif clip_duration:
                # A clipped download runs through ffmpeg for section extraction,
                # which reports elapsed encode time instead of a plain percentage.
                fm = FFMPEG_TIME_RE.search(line)
                if fm:
                    elapsed = int(fm.group(1)) * 3600 + int(fm.group(2)) * 60 + float(fm.group(3))
                    on_progress(min(99, elapsed / clip_duration * 100))
    proc.wait(timeout=1800)

    if proc.returncode != 0:
        raise RuntimeError(tail[-1] if tail else "yt-dlp failed")

    files = [os.path.join(dest_dir, f) for f in os.listdir(dest_dir) if f.startswith(base + ".")]
    if not files:
        raise RuntimeError("yt-dlp reported success but wrote no file")
    if on_progress:
        on_progress(100)
    return sorted(files)


# ----------------------------------------------------------------------------- articles

JS_META = """() => {
  const pick = (sel, attr='content') => {
    const el = document.querySelector(sel);
    return el ? (attr === 'text' ? el.textContent.trim() : el.getAttribute(attr)) : '';
  };
  const body = Array.from(document.querySelectorAll('article p, main p, .article-body p'))
    .map(p => p.textContent.trim()).filter(t => t.length > 60).slice(0, 40);
  return {
    title: pick('meta[property="og:title"]') || pick('h1', 'text') || document.title,
    site: pick('meta[property="og:site_name"]') || location.hostname,
    desc: pick('meta[property="og:description"]') || pick('meta[name="description"]'),
    image: pick('meta[property="og:image"]') || pick('meta[name="twitter:image"]'),
    published: pick('meta[property="article:published_time"]')
            || pick('time', 'datetime') || pick('time', 'text'),
    byline: pick('meta[name="author"]') || pick('[rel="author"]', 'text'),
    body,
  };
}"""

JS_DECLUTTER = """() => {
  // Kill anything pinned to the viewport: nav bars, cookie walls, newsletter slide-ins.
  document.querySelectorAll('body *').forEach(el => {
    const s = getComputedStyle(el);
    if (s.position === 'fixed' || s.position === 'sticky') {
      const r = el.getBoundingClientRect();
      if (r.height > 30) el.style.setProperty('display', 'none', 'important');
    }
  });
  ['[id*="cookie" i]','[class*="cookie" i]','[id*="consent" i]','[class*="consent" i]',
   '[class*="paywall" i]','[class*="newsletter" i]','[aria-modal="true"]','dialog[open]']
    .forEach(sel => document.querySelectorAll(sel).forEach(e => {
      const r = e.getBoundingClientRect();
      if (r.height > 40) e.style.setProperty('display','none','important');
    }));
  document.documentElement.style.setProperty('overflow','visible','important');
  document.body.style.setProperty('overflow','visible','important');
}"""

JS_CLIP = """() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const abs = el => {
    const r = el.getBoundingClientRect();
    return { top: r.top + scrollY, left: r.left + scrollX,
             bottom: r.bottom + scrollY, right: r.right + scrollX };
  };

  const h1 = Array.from(document.querySelectorAll('h1')).filter(vis)
    .sort((a,b) => b.textContent.trim().length - a.textContent.trim().length)[0];
  if (!h1) return null;
  let box = abs(h1);

  // Pull in the kicker/byline/dateline immediately around the headline.
  const near = Array.from(document.querySelectorAll(
      'h2, time, [class*="byline" i], [class*="author" i], [class*="kicker" i], [class*="standfirst" i], [class*="dek" i], [class*="subtitle" i]'
    )).filter(vis);
  for (const el of near) {
    const b = abs(el);
    if (b.top > box.top - 220 && b.bottom < box.bottom + 320 && (b.right - b.left) < innerWidth * 0.95) {
      box.top = Math.min(box.top, b.top);
      box.bottom = Math.max(box.bottom, b.bottom);
      box.left = Math.min(box.left, b.left);
      box.right = Math.max(box.right, b.right);
    }
  }

  // The lead image: biggest picture sitting near the headline.
  const imgs = Array.from(document.querySelectorAll('img, figure, picture')).filter(vis)
    .map(el => ({ el, b: abs(el) }))
    .filter(o => (o.b.right - o.b.left) > 280 && (o.b.bottom - o.b.top) > 180)
    .filter(o => o.b.top > box.top - 900 && o.b.top < box.bottom + 900)
    .sort((a,b) => ((b.b.right-b.b.left)*(b.b.bottom-b.b.top)) - ((a.b.right-a.b.left)*(a.b.bottom-a.b.top)));
  if (imgs.length) {
    const b = imgs[0].b;
    box.top = Math.min(box.top, b.top);
    box.bottom = Math.max(box.bottom, b.bottom);
    box.left = Math.min(box.left, b.left);
    box.right = Math.max(box.right, b.right);
  }

  const pad = 36;
  const x = Math.max(0, box.left - pad);
  const y = Math.max(0, box.top - pad);
  const w = Math.min(document.documentElement.scrollWidth - x, (box.right - box.left) + pad * 2);
  const h = Math.min(document.documentElement.scrollHeight - y, (box.bottom - box.top) + pad * 2);
  if (w < 200 || h < 100) return null;
  return { x, y, width: w, height: h };
}"""

ACCEPT_WORDS = re.compile(
    r"^(accept|agree|allow|got it|ok|i accept|i agree|accept all|allow all|continue|"
    r"consent|zustimmen|akzeptieren|aceptar|j'accepte)", re.I)


def capture_article(url, dest_dir, base, label="", on_progress=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium")

    def step(pct):
        if on_progress:
            on_progress(pct)

    written = []
    step(5)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=2,
            user_agent=UA,
            locale="en-US",
        )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            step(25)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1200)

            # Click a cookie/consent button if one is sitting there.
            for frame in page.frames:
                try:
                    for btn in frame.query_selector_all("button, [role=button], a"):
                        txt = (btn.inner_text() or "").strip()
                        if txt and len(txt) < 30 and ACCEPT_WORDS.match(txt):
                            btn.click(timeout=1500)
                            page.wait_for_timeout(800)
                            raise StopIteration
                except StopIteration:
                    break
                except Exception:
                    continue

            # Lazy-loaded hero images need a nudge.
            page.evaluate("() => window.scrollTo(0, 600)")
            page.wait_for_timeout(700)
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            meta = page.evaluate(JS_META)
            page.evaluate(JS_DECLUTTER)
            page.wait_for_timeout(300)
            clip = page.evaluate(JS_CLIP)
            step(50)

            headline_png = _unique(os.path.join(dest_dir, base + "_headline.png"))
            if clip:
                page.screenshot(path=headline_png, clip=clip, full_page=True)
            else:
                # No usable <h1>: fall back to the top of the page.
                page.screenshot(path=headline_png)
            written.append(headline_png)
            step(70)

            full_png = _unique(os.path.join(dest_dir, base + "_fullpage.png"))
            page.evaluate("() => window.scrollTo(0, 0)")
            page.screenshot(path=full_png, clip={
                "x": 0, "y": 0,
                "width": page.evaluate("() => document.documentElement.scrollWidth"),
                "height": min(6000, page.evaluate("() => document.documentElement.scrollHeight")),
            }, full_page=True)
            written.append(full_png)
            step(85)
        finally:
            ctx.close()
            browser.close()

    # The og:image is usually the press photo at full res — worth having on its own.
    hero = (meta or {}).get("image")
    if hero and hero.startswith("http"):
        try:
            written += download_image(hero, dest_dir, base + "_leadimage")
        except Exception:
            pass
    step(95)

    md = _unique(os.path.join(dest_dir, base + ".md"))
    m = meta or {}
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# {m.get('title') or label or url}\n\n")
        f.write(f"- Source: {m.get('site','')}\n- URL: {url}\n")
        if m.get("byline"):
            f.write(f"- Byline: {m['byline']}\n")
        if m.get("published"):
            f.write(f"- Published: {m['published']}\n")
        f.write("\n")
        if m.get("desc"):
            f.write(f"> {m['desc']}\n\n")
        for para in m.get("body", []):
            f.write(para + "\n\n")
    written.append(md)
    step(100)
    return written, (meta or {}).get("title") or ""


# ----------------------------------------------------------------------------- dispatch

def fetch(url, dest_root, base, clip=None, label="", on_progress=None):
    """Returns (kind, [absolute file paths], resolved_title)."""
    kind = classify(url)
    dest = dest_root  # everything for a paragraph lands in one flat folder
    os.makedirs(dest, exist_ok=True)

    if kind == "image":
        return kind, download_image(url, dest, base, on_progress=on_progress), ""
    if kind == "video":
        return kind, download_video(url, dest, base, clip, on_progress=on_progress), ""
    files, title = capture_article(url, dest, base, label, on_progress=on_progress)
    return kind, files, title
