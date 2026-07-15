"""
Crate launcher — entry point for the packaged app.

Serves the Flask app on a local port with a production WSGI server (waitress),
opens the browser, and lives in the system tray with a Quit option. Run with
`--install-browsers` (the installer does this post-install) to fetch the
Playwright Chromium used for article screenshots into the per-user cache.
"""

import os
import sys
import time
import socket
import threading
import webbrowser


def _bundle_dir():
    """Folder holding bundled resources (templates, bin/, icons). PyInstaller
    unpacks these to sys._MEIPASS; a dev run uses this file's folder."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _browsers_cache():
    """The standard per-user Playwright browser cache for this OS."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "ms-playwright")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or home
        return os.path.join(base, "ms-playwright")
    return os.path.join(home, ".cache", "ms-playwright")


def _set_browsers_path():
    """Pin Playwright's browser location to the standard per-user cache. The
    bundled driver otherwise resolves to a .local-browsers folder inside the
    (read-only, browser-less) app bundle. Both install and runtime must agree
    on this path so the Chromium fetched at install time is the one launched."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _browsers_cache()


def _setup_binaries():
    """Point the download engine at the bundled yt-dlp / ffmpeg (whose file
    names differ by platform: yt-dlp.exe/ffmpeg.exe on Windows, yt-dlp/ffmpeg
    elsewhere)."""
    bin_dir = os.path.join(_bundle_dir(), "bin")
    if not os.path.isdir(bin_dir):
        return
    for f in os.listdir(bin_dir):
        low = f.lower()
        if low in ("yt-dlp.exe", "yt-dlp"):
            os.environ["CRATE_YTDLP"] = os.path.join(bin_dir, f)
        elif low.startswith("ffmpeg"):
            os.environ["CRATE_FFMPEG_DIR"] = bin_dir


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def install_browsers():
    """Download the Chromium build Playwright needs, into the standard
    per-user cache (%LOCALAPPDATA%\\ms-playwright). Idempotent. Never raises —
    the installer runs this as a post-install step, and a network failure here
    (e.g. offline install) must not abort setup; article capture will just
    report a clear error until the browser is fetched on a later run."""
    saved = sys.argv
    try:
        _set_browsers_path()
        from playwright.__main__ import main as pw_main
        sys.argv = ["playwright", "install", "chromium"]
        pw_main()
    except SystemExit:
        pass
    except Exception:
        pass
    finally:
        sys.argv = saved


def _serve(port):
    import app as crate
    from waitress import serve
    serve(crate.app, host="127.0.0.1", port=port, threads=8)


def _tray(url):
    from PIL import Image
    import pystray

    icon_path = os.path.join(_bundle_dir(), "crate.png")
    image = Image.open(icon_path) if os.path.exists(icon_path) else Image.new("RGB", (64, 64), (63, 196, 166))

    def _open(icon, item):
        webbrowser.open(url)

    def _quit(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Crate", _open, default=True),
        pystray.MenuItem("Quit", _quit),
    )
    pystray.Icon("Crate", image, "Crate", menu).run()  # blocks until Quit


def _ensure_browsers_async():
    """Make sure the Chromium article-capture engine is present, without
    blocking startup. `playwright install` is idempotent and does no network
    work when the browser is already there — so this is a fast no-op on
    Windows (where the installer fetched it) and on every later launch, and a
    one-time background download on a Mac's first run. Images and videos work
    immediately regardless; only article screenshots wait on this."""
    threading.Thread(target=install_browsers, daemon=True).start()


def main():
    if "--install-browsers" in sys.argv:
        install_browsers()
        return

    _set_browsers_path()
    _setup_binaries()
    port = int(os.environ.get("CRATE_PORT", "5112"))
    url = f"http://127.0.0.1:{port}"

    # If an instance is already serving, just surface it and exit.
    if _port_open(port):
        webbrowser.open(url)
        return

    _ensure_browsers_async()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    for _ in range(80):
        if _port_open(port):
            break
        time.sleep(0.1)

    webbrowser.open(url)
    try:
        _tray(url)
    except Exception:
        # No tray available (rare) — keep serving until the process is killed.
        while True:
            time.sleep(3600)
    os._exit(0)


if __name__ == "__main__":
    main()
