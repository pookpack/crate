"""CI smoke test: drives an already-running Crate server and fails (nonzero
exit) if an image, a clipped video, or an article doesn't download. Used by
the macOS build workflow to verify the app on a real Apple Silicon Mac."""

import os
import sys
import json
import time
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('CRATE_PORT', '5119')}"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path))


def main():
    box = post("/api/boxes", {})
    bid = box["id"]
    post(f"/api/boxes/{bid}", {"title": "CI Smoke"})
    links = "\n".join([
        "https://commons.wikimedia.org/wiki/File:Chicken_eggs.jpg | Eggs",
        "https://www.youtube.com/watch?v=suXj_rgWHE8 @0:03-0:08 | Clip",
        "https://en.wikipedia.org/wiki/Egg_as_food | Article",
    ])
    post(f"/api/boxes/{bid}", {"links_raw": links})
    post(f"/api/boxes/{bid}/download", {})

    for _ in range(90):
        time.sleep(2)
        assets = get("/api/state")["boxes"][0]["assets"]
        if assets and all(a["status"] in ("done", "error") for a in assets):
            break

    assets = get("/api/state")["boxes"][0]["assets"]
    failed = False
    for a in assets:
        print(f"  {a['status']:6} {a['kind']} | {a['label']} | {(a.get('error') or '')[:140]}")
        if a["status"] != "done":
            failed = True

    if failed or len(assets) != 3:
        print("SMOKE TEST FAILED")
        sys.exit(1)
    print("SMOKE TEST PASSED: image + clip + article all downloaded")


if __name__ == "__main__":
    main()
