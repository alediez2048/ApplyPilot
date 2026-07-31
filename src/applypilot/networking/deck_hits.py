"""Pull intro-deck clicks from the collector on the sender's own site.

The click happens on `jorgealejandrodiez.com`, and the dashboard binds `127.0.0.1` — deliberately,
and that property is worth keeping (see CLAUDE.md §Security posture). So nothing can push a
webhook at us; ApplyPilot **polls** instead. Outbound HTTPS from a local process needs no
inbound exposure at all.

The collector is a pair of Netlify Functions the operator deploys once (`deploy/netlify/`). This
module only knows the contract: GET the URL with a bearer token, receive a list of hits, record
them. It deliberately does not care which host serves it — the same code works against a Vercel
function, a Cloudflare Worker, or a text file on a web server.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

#: A local tool polling a personal site. Long enough to survive a cold serverless start,
#: short enough that a hung endpoint cannot stall an hourly `tick`.
TIMEOUT_SECONDS = 20


def configured() -> tuple[bool, str]:
    """Is the pull set up? Returns (ready, why-not)."""
    if not (os.environ.get("DECK_HITS_URL") or "").strip():
        return False, ("deck-click pull is off — set DECK_HITS_URL (see deploy/netlify/README.md) "
                       "or import by hand with `applypilot deck-hits`")
    if not (os.environ.get("DECK_HITS_TOKEN") or "").strip():
        return False, "DECK_HITS_URL is set but DECK_HITS_TOKEN is missing"
    return True, "ok"


def fetch() -> tuple[list[dict], str]:
    """GET the collector. Returns (hits, error). Never raises.

    `since` is deliberately NOT sent. The collector holds a small rolling window and we re-read
    all of it every time, because `mark_deck_viewed` is idempotent — re-seeing a click bumps a
    counter and announces nothing. A watermark would add a second piece of state that can drift,
    to save a request that costs nothing.
    """
    ok, why = configured()
    if not ok:
        return [], why

    url = os.environ["DECK_HITS_URL"].strip()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {os.environ['DECK_HITS_TOKEN'].strip()}",
        "Accept": "application/json",
        "User-Agent": "ApplyPilot",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # 401 is the one worth naming: it means the token drifted, which otherwise looks
        # exactly like "nobody clicked".
        detail = "wrong DECK_HITS_TOKEN" if e.code in (401, 403) else f"HTTP {e.code}"
        return [], f"deck-hit collector refused: {detail}"
    except Exception as e:  # noqa: BLE001
        return [], f"could not reach the deck-hit collector: {e}"

    try:
        payload = json.loads(body)
    except ValueError:
        # A plain text log is a legitimate response too — scan it like a pasted export.
        from applypilot.domain import deck
        return [{"slug": s, "at": ""} for s in deck.slugs_in(body)], ""

    from applypilot.domain import deck
    return deck.hits_from_payload(payload), ""


def poll(conn=None) -> dict:
    """Fetch clicks and record them. Idempotent; safe on an hourly timer."""
    from applypilot.database import log_event
    from applypilot.networking import store

    hits, err = fetch()
    if err:
        return {"ok": False, "note": err, "recorded": 0, "new": 0}
    if not hits:
        return {"ok": True, "note": "no clicks recorded", "recorded": 0, "new": 0}

    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()
    store.init_contacts(conn)
    contacts = [store.get_contact(c["id"], conn) for c in store.all_contacts_for_metrics(conn)]
    contacts = [c for c in contacts if c]

    by_slug = {(c.get("deck_slug") or "").lower(): c for c in contacts if c.get("deck_slug")}
    new_names, recorded = [], 0
    for hit in hits:
        contact = by_slug.get((hit.get("slug") or "").lower())
        if not contact:
            continue                     # deleted contact, or a link from another install
        recorded += 1
        if store.mark_deck_viewed(contact["id"], at=hit.get("at") or "", conn=conn):
            new_names.append(contact.get("full_name") or contact.get("email") or contact["id"])
            log_event(contact.get("job_url", ""), "outreach", "ok",
                      f"{contact.get('full_name') or 'They'} opened the intro deck.", conn)
    return {"ok": True, "recorded": recorded, "new": len(new_names), "names": new_names,
            "note": (f"{len(new_names)} new: {', '.join(new_names)}" if new_names
                     else f"{recorded} click(s), none new")}
