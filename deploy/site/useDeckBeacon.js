/* The deck beacon, for a Gatsby (or any React) intro page.
 *
 * This is step 3 of deploy/netlify/README.md, and on this install it is the step that was never
 * done: the two functions were deployed and the token was set, but the PAGE never called them.
 * The collector held two hits for five days, both of them verification probes, while ~70 named
 * links went out. "Nobody opened the deck" was unknowable, not false.
 *
 * It lives in this repo for the same reason the functions do: the site copy drifted once
 * already — a deployed `deck-hit.mjs` validating `body.v` against an 8-hex regex while the
 * beacon sent `{slug}`, which would have returned 204 (its success code) on every real click
 * and stored nothing. Three pieces, one contract, one canonical copy.
 *
 * ── Why it reads the URL and not Gatsby's page props ──────────────────────────────────
 *
 * The Netlify rewrite is `/intro/* -> /intro/index.html` with status 200, so EVERY named path
 * serves the same page. Gatsby therefore believes the path is always `/intro/` — measured on
 * the live site, `/intro/zzprobe-not-a-person` returns a document containing
 * `window.pagePath="/intro/"`. A `useEffect` reading `pageContext`, `props.path` or
 * `useLocation()` from Gatsby's router would send the wrong slug or none at all, and would look
 * completely correct in review.
 *
 * `window.location.pathname` is the only thing that knows which name was actually opened.
 */

import { useEffect, useRef } from "react";

// The same expression the collector enforces (`deck-hit.mjs`). Duplicated deliberately: the
// client should not send what the server will reject, and a slug that fails here is a page view
// nobody can be attributed for rather than an error.
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,38}$/;

const ENDPOINT = "/api/deck-hit";
const OPT_OUT_KEY = "deck-beacon-off";

/** The name segment of `/intro/gina`, or "" when there isn't one. */
export function slugFromPath(pathname) {
  const seg = String(pathname || "").replace(/\/+$/, "").split("/").pop();
  if (!seg || seg === "intro") return "";
  const slug = seg.toLowerCase();
  return SLUG_RE.test(slug) ? slug : "";
}

/** Fire once per page load. Returns nothing; failures are silent by design. */
export default function useDeckBeacon() {
  const fired = useRef(false);

  useEffect(() => {
    // React 18 StrictMode mounts twice in development. The collector appends rather than
    // dedupes, so without this a dev visit is two clicks in the rolling window.
    if (fired.current) return;
    fired.current = true;

    try {
      // Looking at your own deck must not record the recipient opening it. `?notrack=1` once
      // per browser is enough — visit https://…/intro/gina?notrack=1 and this device stops
      // reporting for good. Without it, previewing what you sent someone silently tells you
      // they read it, which is worse than no signal at all.
      const params = new URLSearchParams(window.location.search);
      if (params.get("notrack") === "1") {
        window.localStorage.setItem(OPT_OUT_KEY, "1");
        return;
      }
      if (window.localStorage.getItem(OPT_OUT_KEY) === "1") return;

      const slug = slugFromPath(window.location.pathname);
      // No name in the URL: `/intro/` itself, or a link someone forwarded with the name
      // stripped. The server 204s these anyway; not sending keeps them out of the 500-item
      // rolling window, where they would push real clicks off the end.
      if (!slug) return;

      const body = JSON.stringify({ slug });
      // sendBeacon first: it survives the tab being closed or navigated away from immediately,
      // which is exactly what skimming a deck looks like. `fetch` with keepalive is the
      // fallback for browsers without it.
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/json" }));
      } else {
        fetch(ENDPOINT, {
          method: "POST",
          body,
          headers: { "content-type": "application/json" },
          keepalive: true,
        });
      }
    } catch {
      // A analytics beacon must never be able to break the page it is measuring.
    }
  }, []);
}
