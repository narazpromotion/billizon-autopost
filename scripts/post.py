#!/usr/bin/env python3
"""
Posts the next queued item to Instagram via the Meta Graph API.

Reads posts/queue.json, finds the first entry that has not been posted,
publishes it, then writes the queue back with the result recorded.

Environment:
    IG_USER_ID      Instagram business account id
    IG_TOKEN        Long lived Page access token (from GitHub Secrets)
    DRY_RUN         If "true", validate everything but do not publish
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRAPH = "https://graph.facebook.com/v21.0"

REPO_RAW = "https://raw.githubusercontent.com/narazpromotion/billizon-autopost/main"

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT / "posts" / "queue.json"
IMAGE_DIR = ROOT / "images"

# Instagram's own limits. Checked before we spend an API call.
MAX_CAPTION = 2200
MAX_HASHTAGS = 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIN_RATIO = 4 / 5      # 0.80, tallest Instagram allows
MAX_RATIO = 1.91       # widest Instagram allows


def jpeg_size(path):
    """
    Read width and height from a JPEG's SOF marker.

    Done by hand rather than with Pillow so the workflow needs no pip install
    step and cannot break when a dependency changes under us.
    """
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise PostError(f"{path.name} is not a JPEG despite its extension")
        while True:
            marker = fh.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                raise PostError(f"Could not read dimensions from {path.name}")
            code = marker[1]
            (length,) = int.from_bytes(fh.read(2), "big"),
            # SOF0..SOF15, excluding the non-dimension markers DHT/JPG/DAC
            if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                fh.read(1)  # precision
                height = int.from_bytes(fh.read(2), "big")
                width = int.from_bytes(fh.read(2), "big")
                return width, height
            fh.seek(length - 2, 1)


class PostError(Exception):
    """Anything that should stop this run without burning the queue entry."""


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def api_get(path, params):
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def api_post(path, params):
    url = f"{GRAPH}/{path}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise PostError(f"Graph API {exc.code} on {path}: {detail}") from exc


def load_queue():
    if not QUEUE_PATH.exists():
        raise PostError(f"No queue file at {QUEUE_PATH}")
    with QUEUE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_queue(queue):
    with QUEUE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(queue, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def next_unposted(queue):
    for entry in queue:
        if not entry.get("posted_at"):
            return entry
    return None


def validate(entry):
    """Catch the things Instagram rejects, before we call the API."""
    problems = []

    image = entry.get("image", "")
    if not image:
        problems.append("entry has no image")
    else:
        path = IMAGE_DIR / image
        if not path.exists():
            problems.append(f"image not found in images/: {image}")
        else:
            if path.suffix.lower() not in (".jpg", ".jpeg"):
                problems.append(
                    f"{image} is not a JPEG. Instagram rejects PNG and WebP outright."
                )
            size = path.stat().st_size
            if size > MAX_IMAGE_BYTES:
                problems.append(f"{image} is {size / 1e6:.1f} MB, over the 8 MB limit")

            try:
                width, height = jpeg_size(path)
                ratio = width / height
                if ratio < MIN_RATIO:
                    problems.append(
                        f"{image} is {width}x{height}, ratio {ratio:.3f}, taller than "
                        f"the 4:5 floor ({MIN_RATIO:.2f}). Pad it onto a 4:5 canvas "
                        f"rather than cropping."
                    )
                elif ratio > MAX_RATIO:
                    problems.append(
                        f"{image} is {width}x{height}, ratio {ratio:.3f}, wider than "
                        f"the 1.91:1 ceiling."
                    )
            except PostError as exc:
                problems.append(str(exc))

    caption = entry.get("caption", "")
    if len(caption) > MAX_CAPTION:
        problems.append(f"caption is {len(caption)} chars, over the {MAX_CAPTION} limit")
    hashtags = caption.count("#")
    if hashtags > MAX_HASHTAGS:
        problems.append(f"caption has {hashtags} hashtags, over the {MAX_HASHTAGS} limit")

    if problems:
        raise PostError("Queue entry failed validation:\n  - " + "\n  - ".join(problems))


def wait_for_container(container_id, token, attempts=20, delay=6):
    """
    A container is not publishable the instant it is created. Instagram
    fetches and processes the image first. Publishing too early returns a
    confusing error, so poll until it reports FINISHED.
    """
    for attempt in range(1, attempts + 1):
        status = api_get(
            container_id, {"fields": "status_code,status", "access_token": token}
        )
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise PostError(f"Instagram could not process the image: {status.get('status')}")
        log(f"  container {code}, waiting ({attempt}/{attempts})")
        time.sleep(delay)
    raise PostError("Container never reached FINISHED. Not publishing.")


PAGE_ID = "1337903236073632"  # the Billizon Facebook Page


def check_token(ig_user_id, token):
    """
    Confirm the secret works, say which kind of token it is, and above all
    report WHEN IT DIES.

    Being a Page token is not enough. A Page token minted from a short lived
    User token inherits that short life and stops working within hours, while a
    Page token minted from a long lived User token never expires. Both look
    identical to GET /me, which is why an earlier version of this function
    cheerfully printed "Does not expire" about a token that expired the next
    day. The only way to know is to ask, so we ask.
    """
    try:
        who = api_get("me", {"fields": "id,name", "access_token": token})
    except Exception as exc:
        raise PostError(
            "The IG_TOKEN secret was rejected by the Graph API. "
            f"Check it was copied whole and has not expired. Detail: {exc}"
        )

    kind = "Page" if who.get("id") == PAGE_ID else "non-Page"
    log(f"  token: {kind} token, identifies as '{who.get('name')}'")

    # Deliberately no warning here. A System User token reports the system user
    # rather than the Page, which is correct and is what we run on. Identity
    # says nothing useful about whether the token will still work tomorrow;
    # only expiry does, and the account check below proves it can actually
    # reach Instagram. Judging a token by which name comes back from /me is
    # what produced two confidently wrong diagnoses in September 2026.

    # expires_at of 0 means never. Anything else is a countdown.
    try:
        info = api_get("debug_token", {"input_token": token, "access_token": token})
        data = info.get("data", {})
        expires = data.get("expires_at")
        if expires == 0:
            log("  expiry: never. This is a permanent token, which is what we want.")
        elif expires:
            when = datetime.fromtimestamp(expires, timezone.utc)
            left = when - datetime.now(timezone.utc)
            hours = left.total_seconds() / 3600
            log(f"  EXPIRY: {when:%Y-%m-%d %H:%M} UTC, about {hours:.0f} hours from now.")
            log("  THIS TOKEN IS TEMPORARY AND WILL BREAK THE SCHEDULE WHEN IT DIES.")
            log("  A permanent one comes from a System User in Meta Business Settings")
            log("  with token expiration set to Never. Replace IG_TOKEN with that.")
        else:
            log("  expiry: could not be determined from debug_token.")
    except Exception as exc:
        log(f"  expiry: debug_token check failed ({exc}). Continuing anyway.")

    account = api_get(
        ig_user_id, {"fields": "username,followers_count", "access_token": token}
    )
    log(f"  account: @{account.get('username')} reachable, "
        f"{account.get('followers_count', 0)} followers")


def main():
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("IG_TOKEN", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").lower() == "true"

    if not ig_user_id or not token:
        raise PostError("IG_USER_ID and IG_TOKEN must both be set")

    queue = load_queue()
    entry = next_unposted(queue)

    if entry is None:
        log("Queue is empty. Nothing to post.")
        log("Add entries to posts/queue.json to keep the schedule running.")
        return 0

    log(f"Next up: {entry['image']}")
    validate(entry)
    log("Validation passed.")

    log("Checking credentials...")
    check_token(ig_user_id, token)

    image_url = f"{REPO_RAW}/images/{urllib.parse.quote(entry['image'])}"
    log(f"Image URL: {image_url}")

    if dry_run:
        log("DRY_RUN set. Stopping before publish.")
        log("--- caption as it would appear ---")
        print(entry.get("caption", ""))
        return 0

    log("Creating media container...")
    container = api_post(
        f"{ig_user_id}/media",
        {
            "image_url": image_url,
            "caption": entry.get("caption", ""),
            "access_token": token,
        },
    )
    container_id = container["id"]
    log(f"Container {container_id} created.")

    wait_for_container(container_id, token)

    log("Publishing...")
    published = api_post(
        f"{ig_user_id}/media_publish",
        {"creation_id": container_id, "access_token": token},
    )
    media_id = published["id"]
    log(f"Published. Media id {media_id}")

    entry["posted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["media_id"] = media_id
    save_queue(queue)
    log("Queue updated.")

    remaining = sum(1 for e in queue if not e.get("posted_at"))
    log(f"{remaining} post(s) left in the queue.")
    if remaining <= 3:
        log("WARNING: queue is nearly empty. Top it up.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PostError as exc:
        log(f"FAILED: {exc}")
        sys.exit(1)
