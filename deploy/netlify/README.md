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

**3. Add the beacon — at PARSE TIME, not in a `useEffect`.**

> **The `useEffect` version cannot work on a rewritten path, and this cost 98 emails.**
> The rewrite in step 2 is what puts the name in the browser's URL. A client-side router then
> hydrates, does not recognise `/intro/gina` as one of its routes, and **replaces the URL with
> the canonical `/intro/`**. Measured on the live site: the tab title still reads
> `/intro/zzprobe-live-check-b2` while `location.pathname` is already `/intro/`. By the time a
> `useEffect` runs, the name is gone and the `seg === "intro"` guard returns.
>
> Everything else looks perfect while this is broken: the link is right, the page serves 200,
> the function returns 204, the collector reads back. Two real browser loads of named URLs
> produced zero hits while direct POSTs to the same endpoint landed fine.

Put it in the document, so it runs before any framework touches the URL:

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

**Gatsby:** put that script in `gatsby-ssr.js` via `onRenderBody({ setPreBodyComponents })`, so
it lands in the HTML ahead of hydration. If you would rather keep the logic in the component,
capture the name at parse time and read the captured value:

```js
// gatsby-ssr.js — runs before React, while the URL still has the name
setPreBodyComponents([
  <script key="deck-slug" dangerouslySetInnerHTML={{ __html:
    'window.__deckSlug=location.pathname.replace(/\\/+$/,"").split("/").pop()||"";' }} />,
])
```

```js
// intro.js — one line changes
const seg = window.__deckSlug || ""       // NOT location.pathname, which the router has rewritten
```

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

## Verify

```bash
sh scripts/deck-check.sh --probe     # in the ApplyPilot repo
```

It checks the two things that fail silently: whether the beacon is in the shipped JavaScript
(the page CHUNK, not the HTML — grepping the HTML reports missing against a working beacon), and
whether a real browser load actually reaches the collector.

Open the probe URL it prints **in a real browser**, wait ~15 seconds, re-run. Your slug must
appear. curl will not do: the beacon is JavaScript and curl runs none of it.

**Never open a real contact's `/intro/<name>`.** It records that person opening the deck.
