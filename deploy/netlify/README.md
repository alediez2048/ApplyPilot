# Deck-click collector (Netlify)

Records who opened the intro deck, and lets ApplyPilot pull it.

**This is not email open tracking, and the difference matters.** An open-tracking pixel fires
when Gmail proxies and caches the image on delivery, when Apple Mail pre-fetches it (default-on
since iOS 15), and when a corporate gateway scans the message — so it mostly measures machines.
A confidently wrong signal is worse than none. This measures a **click**: a browser loaded the
deck page, which means a person chose to look.

It is a first-party beacon on your own site. No pixel in the email, no third-party analytics,
and nothing in the message beyond a link the recipient can see.

## What gets stored

Only the opaque 8-character token from the URL, plus a timestamp. **No IP, no user-agent, no
referrer, no cookie.** The token is an HMAC of a contact id and your ApplyPilot install secret,
so it means nothing to anyone who does not have that secret — including anyone who reads the
blob store.

## Install (once, ~5 minutes)

**1. Copy the functions into your site repo**

```
netlify/functions/deck-hit.mjs      <- from functions/deck-hit.mjs
netlify/functions/deck-hits.mjs     <- from functions/deck-hits.mjs
```

Netlify Blobs needs no setup; it is available to functions on all plans.

**2. Add the beacon to the deck page** (`/intro/`), before `</body>`:

```html
<script>
  (function () {
    var v = new URLSearchParams(location.search).get("v");
    if (!v) return;                       // arrived without a link — nothing to attribute
    navigator.sendBeacon
      ? navigator.sendBeacon("/api/deck-hit", new Blob([JSON.stringify({ v: v })],
          { type: "application/json" }))
      : fetch("/api/deck-hit", { method: "POST", body: JSON.stringify({ v: v }),
          headers: { "content-type": "application/json" }, keepalive: true });
  })();
</script>
```

`sendBeacon` is used first because it survives the user navigating away immediately — which is
exactly what someone skimming a deck does.

**3. Set the shared secret** in Netlify → Site configuration → Environment variables:

```
DECK_HITS_TOKEN = <a long random string>
```

Generate one with: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

**4. Point ApplyPilot at it** — in `~/.applypilot/.env`:

```
DECK_HITS_URL=https://www.jorgealejandrodiez.com/api/deck-hits
DECK_HITS_TOKEN=<the same string>
```

**5. Check it**

```
applypilot doctor            # shows "Deck clicks: on"
applypilot tick --dry-run    # shows how many clicks are waiting
```

From then on the hourly `tick` records them, and a **👁 opened the deck** pill appears on the
contact. Nothing else to run.

## If you skip this

`applypilot deck-hits <file>` still imports from any text containing the URLs — a Plausible or
GA export, a pasted list. The links already carry their tokens either way.

## Notes

- The blob keeps a rolling window of the last 500 hits. ApplyPilot re-reads all of it every
  poll and dedupes; recording a click twice bumps a counter and announces nothing.
- The read endpoint does **not** drain. A destructive read loses hits if the caller dies
  mid-response, and idempotence on the ApplyPilot side makes draining pointless.
- `deck-hits.mjs` returns 503 when `DECK_HITS_TOKEN` is unset rather than serving openly. A
  collector that answers everyone because its config is missing is worse than one that is
  down — it looks like it is working.
