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

Only the name segment from the URL (`gina`), plus a timestamp. **No IP, no user-agent, no
referrer, no cookie.** The link is a named path — `/intro/gina`, not `/intro/?v=9b83068a`. Both identify the reader;
only one looks like it, and this link goes in a message whose whole point is sounding personal.
The honest trade: a name is readable, so a forwarded link tells the new reader who it was for.
Personalisation is a real benefit rather than a disguise.

## Install (once, ~5 minutes)

**1. Copy the functions into your site repo**

```
netlify/functions/deck-hit.mjs      <- from functions/deck-hit.mjs
netlify/functions/deck-hits.mjs     <- from functions/deck-hits.mjs
```

Netlify Blobs needs no setup; it is available to functions on all plans.

**2. Serve `/intro/<name>` from the same page.** In `netlify.toml`:

```toml
[[redirects]]
  from = "/intro/*"
  to = "/intro/index.html"
  status = 200
```

`200` is a REWRITE, not a redirect: the browser keeps `/intro/gina` in the address bar, which is
what lets the page read the name. A 301 would strip it before the page ever loaded.

**3. Add the beacon to the deck page**, before `</body>` (or as a `useEffect` in the component):

```html
<script>
  (function () {
    var seg = location.pathname.replace(/\/+$/, "").split("/").pop();
    if (!seg || seg === "intro" || !/^[a-z0-9][a-z0-9-]{0,38}$/.test(seg)) return;
    var body = JSON.stringify({ slug: seg });
    navigator.sendBeacon
      ? navigator.sendBeacon("/api/deck-hit", new Blob([body], { type: "application/json" }))
      : fetch("/api/deck-hit", { method: "POST", body: body,
          headers: { "content-type": "application/json" }, keepalive: true });
  })();
</script>
```

`sendBeacon` is used first because it survives the user navigating away immediately — which is
exactly what someone skimming a deck does.

**4. Set the shared secret** in Netlify → Site configuration → Environment variables:

```
DECK_HITS_TOKEN = <a long random string>
```

Generate one with: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

**5. Point ApplyPilot at it** — in `~/.applypilot/.env`:

```
DECK_HITS_URL=https://www.jorgealejandrodiez.com/api/deck-hits
DECK_HITS_TOKEN=<the same string>
```

**6. Check it**

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
