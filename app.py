"""
Crate — manually managed boxes of downloadable assets.

Unlike Sourcer (which splits one pasted script into fixed paragraph boxes),
Crate lets you set a destination folder, add and remove boxes yourself, and
paste script segments into them however you like. Everything a box downloads
lands directly in that box's own flat folder inside the destination — no
images/video/articles subfolders.

Run:  python app.py   ->  http://127.0.0.1:5112
"""

import os
import re
import sys
import json
import time
import uuid
import threading
import subprocess
import mimetypes
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template, send_file, abort

import downloader as dl

FROZEN = getattr(sys, "frozen", False)


def _resource_dir():
    """Where bundled read-only assets (templates) live. PyInstaller unpacks
    them to sys._MEIPASS; a dev checkout uses this file's folder."""
    if FROZEN:
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """A writable per-user folder for the workspace file. The installed app's
    own folder is read-only (Program Files on Windows, inside the .app on Mac),
    so state must live in the OS's per-user app-data location instead."""
    d = os.environ.get("CRATE_DATA_DIR")
    if not d:
        if FROZEN:
            home = os.path.expanduser("~")
            if sys.platform == "darwin":
                d = os.path.join(home, "Library", "Application Support", "Crate")
            elif os.name == "nt":
                d = os.path.join(os.environ.get("LOCALAPPDATA") or home, "Crate")
            else:
                d = os.path.join(home, ".local", "share", "Crate")
        else:
            d = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(d, exist_ok=True)
    return d


def _default_dest():
    if os.environ.get("CRATE_DEST"):
        return os.environ["CRATE_DEST"]
    if FROZEN:
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        base = docs if os.path.isdir(docs) else os.path.expanduser("~")
        return os.path.join(base, "Crate Downloads")
    return os.path.join(DATA_DIR, "Downloads")


RES_DIR = _resource_dir()
DATA_DIR = _data_dir()
STATE_PATH = os.path.join(DATA_DIR, "workspace.json")
DEFAULT_DEST = _default_dest()
PORT = int(os.environ.get("CRATE_PORT", "5112"))

app = Flask(__name__, template_folder=os.path.join(RES_DIR, "templates"))
app.config["JSON_SORT_KEYS"] = False

_lock = threading.RLock()          # reentrant: held across a whole load+mutate+save
_pool = ThreadPoolExecutor(max_workers=3)
_running = 0                        # download jobs in flight
_progress = {}                     # (box_id, pos) -> 0-100, in-memory only


# ----------------------------------------------------------------- state plumbing

def default_state():
    return {"destination": DEFAULT_DEST, "boxes": []}


def load_state():
    with _lock:
        if not os.path.exists(STATE_PATH):
            return default_state()
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)


def save_state(state):
    with _lock:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)


def find_box(state, box_id):
    for b in state["boxes"]:
        if b["id"] == box_id:
            return b
    return None


def box_folder_name(box):
    # Blank-title boxes get a unique per-box folder rather than all colliding
    # into one "untitled" (dl.slugify's fallback), which would mix their files.
    if not (box.get("title") or "").strip():
        return "box-" + box["id"][:8]
    return dl.slugify(box["title"], max_words=8, max_len=60)


def box_dir(state, box):
    return os.path.join(state["destination"], box_folder_name(box))


def _asset_key(a):
    # A URL can legitimately repeat with a different clip range (several beats
    # pulled from one long video), so identity is (url, clip), not url alone.
    return (a["url"], tuple(a["clip"]) if a.get("clip") else None)


def _new_asset(item):
    return {
        "url": item["url"], "label": item["label"], "clip": item["clip"],
        "kind": None, "status": "pending", "error": None, "files": [], "title": "",
    }


def _merge_links(box, raw):
    """Reparse a box's link textarea into pending assets, keeping any that
    already downloaded and dropping deleted-and-never-fetched ones."""
    box["links_raw"] = raw
    parsed = [x for x in (dl.parse_link_line(l) for l in raw.splitlines()) if x]
    known = {_asset_key(a) for a in box["assets"]}
    for item in parsed:
        key = _asset_key(item)
        if key not in known:
            box["assets"].append(_new_asset(item))
            known.add(key)
    live = {_asset_key(i) for i in parsed}
    box["assets"] = [a for a in box["assets"] if _asset_key(a) in live or a["files"]]


# ----------------------------------------------------------------- pages / state

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def get_state():
    state = load_state()
    for box in state["boxes"]:
        box["folder"] = box_folder_name(box)
        for pos, a in enumerate(box["assets"]):
            if a["status"] == "working":
                pct = _progress.get((box["id"], pos))
                if pct is not None:
                    a["progress"] = pct
    state["running"] = _running
    return jsonify(state)


@app.post("/api/destination")
def set_destination():
    data = request.get_json(force=True)
    path = (data.get("destination") or "").strip()
    if not path:
        return jsonify({"error": "Enter a folder path."}), 400
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"Couldn't create or use that folder: {e}"}), 400
    with _lock:
        state = load_state()
        state["destination"] = path
        save_state(state)
    return jsonify({"destination": path})


# ----------------------------------------------------------------- box CRUD

@app.post("/api/boxes")
def add_box():
    data = request.get_json(silent=True) or {}
    with _lock:
        state = load_state()
        box = {
            "id": uuid.uuid4().hex,
            "title": data.get("title", ""),
            "script": data.get("script", ""),
            "links_raw": "",
            "assets": [],
        }
        state["boxes"].append(box)
        save_state(state)
    box["folder"] = box_folder_name(box)
    return jsonify(box)


@app.post("/api/boxes/<box_id>")
def update_box(box_id):
    data = request.get_json(force=True)
    with _lock:
        state = load_state()
        box = find_box(state, box_id)
        if not box:
            abort(404)
        if "title" in data:
            box["title"] = data["title"]
        if "script" in data:
            box["script"] = data["script"]
        if "links_raw" in data:
            _merge_links(box, data["links_raw"])
        save_state(state)
        box["folder"] = box_folder_name(box)
        return jsonify(box)


@app.delete("/api/boxes/<box_id>")
def delete_box(box_id):
    """Remove the box from the workspace. Files already on disk are left
    alone — deleting a box is about the list, not your downloads."""
    with _lock:
        state = load_state()
        before = len(state["boxes"])
        state["boxes"] = [b for b in state["boxes"] if b["id"] != box_id]
        if len(state["boxes"]) == before:
            abort(404)
        save_state(state)
    return jsonify({"ok": True})


@app.post("/api/boxes/<box_id>/import-pdf")
def import_pdf(box_id):
    """Import a production asset sheet PDF into this box: every hyperlink on
    every page (with any @start-end clip range) is added to the box's links."""
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Attach a .pdf file."}), 400
    try:
        items = dl.parse_pdf_shotlist(f.read())
    except Exception as e:
        return jsonify({"error": f"Could not read that PDF: {e}"}), 400
    if not items:
        return jsonify({"error": "No links found in that PDF."}), 400

    with _lock:
        state = load_state()
        box = find_box(state, box_id)
        if not box:
            abort(404)
        known = {_asset_key(a) for a in box["assets"]}
        imported = 0
        for item in items:
            key = _asset_key(item)
            if key not in known:
                box["assets"].append(_new_asset(item))
                known.add(key)
                imported += 1
        box["links_raw"] = "\n".join(_render_link_line(a) for a in box["assets"])
        save_state(state)
        box["folder"] = box_folder_name(box)
        return jsonify({**box, "imported": imported})


def _render_link_line(a):
    line = a["url"]
    if a.get("clip"):
        line += f" @{a['clip'][0]}-{a['clip'][1]}"
    if a.get("label"):
        line += f" | {a['label']}"
    return line


# ----------------------------------------------------------------- downloads

def _queue_box(box, force=False):
    global _running
    queued = 0
    for pos, a in enumerate(box["assets"]):
        if a["status"] == "done" and not force:
            continue
        if a["status"] in ("working", "queued"):
            continue
        a["status"] = "queued"
        a["error"] = None
        _pool.submit(run_one, box["id"], pos)
        queued += 1
    _running += queued
    return queued


@app.post("/api/boxes/<box_id>/download")
def download_box(box_id):
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    with _lock:
        state = load_state()
        box = find_box(state, box_id)
        if not box:
            abort(404)
        queued = _queue_box(box, force)
        save_state(state)
    return jsonify({"queued": queued})


@app.post("/api/download-all")
def download_all():
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    with _lock:
        state = load_state()
        queued = sum(_queue_box(b, force) for b in state["boxes"])
        save_state(state)
    return jsonify({"queued": queued})


def run_one(box_id, pos):
    global _running
    key = (box_id, pos)
    try:
        with _lock:
            state = load_state()
            box = find_box(state, box_id)
            if not box or pos >= len(box["assets"]):
                return
            a = box["assets"][pos]
            a["status"] = "working"
            save_state(state)
            dest = box_dir(state, box)
            destination_root = state["destination"]
            clip = tuple(a["clip"]) if a.get("clip") else None
            base = f"{pos + 1:02d}"
            if clip:
                # Name a clipped video by its timestamp range, so several clips
                # from one source video stay distinguishable in the folder.
                base += "_" + f"{clip[0]}-{clip[1]}".replace(":", "")
            elif a.get("label"):
                base += "_" + dl.slugify(a["label"], max_words=5)
            label = a.get("label", "")
            url = a["url"]

        # Download happens outside the lock — a video can take minutes and must
        # not block every other box's status bookkeeping.
        _progress[key] = 0
        kind, files, title = dl.fetch(
            url, dest, base, clip=clip, label=label,
            on_progress=lambda pct: _progress.__setitem__(key, pct),
        )

        with _lock:
            state = load_state()
            box = find_box(state, box_id)
            if box and pos < len(box["assets"]):
                a = box["assets"][pos]
                a.update({
                    "kind": kind, "status": "done", "error": None, "title": title,
                    "files": [os.path.relpath(f, state["destination"]) for f in files],
                })
                save_state(state)
    except Exception as e:
        try:
            with _lock:
                state = load_state()
                box = find_box(state, box_id)
                if box and pos < len(box["assets"]):
                    a = box["assets"][pos]
                    a["status"] = "error"
                    a["error"] = str(e)[:400]
                    save_state(state)
        except Exception:
            pass
    finally:
        _progress.pop(key, None)
        with _lock:
            _running = max(0, _running - 1)


# ----------------------------------------------------------------- media / reveal

@app.get("/api/media")
def media():
    state = load_state()
    rel = request.args.get("path", "")
    root = os.path.abspath(state["destination"])
    full = os.path.abspath(os.path.join(root, rel))
    if not full.startswith(root) or not os.path.exists(full):
        abort(404)
    mime, _ = mimetypes.guess_type(full)
    return send_file(full, mimetype=mime or "application/octet-stream")


def _focus_explorer_window(path):
    """Windows blocks background processes from stealing focus, so a folder
    opened by reveal() can pop up behind other windows. Find the new Explorer
    window by title prefix and flash its taskbar icon so it's not missed."""
    import ctypes
    from ctypes import wintypes

    title_prefix = os.path.basename(os.path.normpath(path))
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    matches = []

    def _callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.startswith(title_prefix):
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            if cls_buf.value == "CabinetWClass":
                matches.append(hwnd)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_callback)
    hwnd = 0
    for _ in range(20):
        time.sleep(0.15)
        matches.clear()
        user32.EnumWindows(enum_proc, 0)
        if matches:
            hwnd = matches[0]
            break
    if not hwnd:
        return

    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    cur_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(cur_thread, fg_thread, True)
    user32.AttachThreadInput(target_thread, fg_thread, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(cur_thread, fg_thread, False)
    user32.AttachThreadInput(target_thread, fg_thread, False)

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]
    FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 6, 0)
    user32.FlashWindowEx(ctypes.byref(info))


def _open_folder(path):
    os.makedirs(path, exist_ok=True)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # noqa
            try:
                _focus_explorer_window(path)
            except Exception:
                pass
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        return {"ok": False, "error": str(e), "path": path}
    return {"ok": True, "path": path}


@app.post("/api/boxes/<box_id>/reveal")
def reveal_box(box_id):
    state = load_state()
    box = find_box(state, box_id)
    if not box:
        abort(404)
    return jsonify(_open_folder(os.path.abspath(box_dir(state, box))))


@app.post("/api/reveal")
def reveal_destination():
    state = load_state()
    return jsonify(_open_folder(os.path.abspath(state["destination"])))


@app.post("/api/quit")
def quit_app():
    """Shut the whole app down. This is the reliable quit path in the packaged
    app (the system-tray Quit is a bonus on top). Exits a beat after replying
    so the browser gets its response first."""
    def _bye():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    state = load_state()
    os.makedirs(state["destination"], exist_ok=True)
    print(f"  Destination: {state['destination']}")
    print(f"  Open:        http://127.0.0.1:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
