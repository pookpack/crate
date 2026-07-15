"""Download a static darwin-arm64 ffmpeg binary from eugeneware/ffmpeg-static's
latest GitHub release into build_tools/bin_mac/ffmpeg.

Searches for the right asset by pattern (matches "darwin" + "arm64", excludes
license/text files) instead of one hardcoded exact name, because release
asset names on that repo can carry a compression suffix that varies between
releases (e.g. "darwin-arm64" vs "darwin-arm64.gz"). Transparently decompresses
gzip if the matched asset turns out to be gzipped.

If no asset matches, this prints every actual asset name in the release before
exiting non-zero — so a naming mismatch is diagnosable straight from the CI
log, no need to query the GitHub API separately to find out what's there.
"""

import io
import os
import re
import sys
import gzip
import stat
import urllib.error
import urllib.request

REPO = "eugeneware/ffmpeg-static"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin_mac")
OUT_PATH = os.path.join(OUT_DIR, "ffmpeg")

# Matches an asset that's plausibly the darwin/arm64 ffmpeg binary itself,
# not its license or readme.
CANDIDATE_RE = re.compile(r"darwin.*arm64|arm64.*darwin", re.I)
EXCLUDE_RE = re.compile(r"license|readme|\.txt$|\.md$|\.sha\d*$", re.I)


def _headers(extra=None):
    h = {"User-Agent": "crate-build-script"}
    # GITHUB_TOKEN is provided automatically in every Actions run. Authenticated
    # requests to api.github.com get a 1,000/hour-per-repo quota instead of the
    # unauthenticated 60/hour-per-IP quota — and that IP is shared by every
    # other Actions job running anywhere in the world at that moment, so the
    # unauthenticated quota can already be exhausted before we ever ask.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    if extra:
        h.update(extra)
    return h


def fetch_json(url):
    req = urllib.request.Request(url, headers=_headers({"Accept": "application/vnd.github+json"}))
    import json
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Querying {API} ...")
    print(f"(authenticated: {'yes' if (os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')) else 'NO — GITHUB_TOKEN not set, subject to the tight unauthenticated rate limit'})")
    try:
        data = fetch_json(API)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("\nERROR: HTTP 403 from api.github.com — almost always the unauthenticated")
            print("rate limit (60 requests/hour, shared by every Actions runner IP worldwide).")
            print("Fix: make sure the workflow step passes GITHUB_TOKEN, e.g.:")
            print("  env:\n    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}")
        raise
    assets = data.get("assets") or []
    if not assets:
        print("ERROR: latest release has no assets at all.")
        print(f"Release: {data.get('html_url')}")
        sys.exit(1)

    names = [a["name"] for a in assets]
    print(f"Found {len(names)} assets in release {data.get('tag_name')}:")
    for n in names:
        print(f"  - {n}")

    match = None
    for a in assets:
        n = a["name"]
        if CANDIDATE_RE.search(n) and not EXCLUDE_RE.search(n):
            match = a
            break

    if not match:
        print("\nERROR: none of the asset names above matched the darwin/arm64 pattern.")
        print("Fix CANDIDATE_RE in build_tools/fetch_mac_ffmpeg.py to match one of the names printed above.")
        sys.exit(1)

    url = match["browser_download_url"]
    print(f"\nDownloading: {match['name']}  <-  {url}")
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()

    # Transparently decompress if it's gzip (magic bytes 1F 8B).
    if raw[:2] == b"\x1f\x8b":
        print("Asset is gzip-compressed, decompressing...")
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()

    with open(OUT_PATH, "wb") as f:
        f.write(raw)
    st = os.stat(OUT_PATH)
    os.chmod(OUT_PATH, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Wrote {OUT_PATH} ({len(raw):,} bytes)")


if __name__ == "__main__":
    main()
