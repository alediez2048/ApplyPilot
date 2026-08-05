#!/bin/sh
# Is intro-deck click tracking actually working?
#
# The chain has five links, and the last two are the ones that break silently:
#
#   1. emails carry /intro/<name>       2. the page serves
#   3. the collector stores + reads     4. the beacon is IN the shipped JavaScript
#   5. the beacon can still SEE the name when it runs
#
# Link 5 is the one that was broken here, and it is invisible from every other angle. Netlify
# rewrites /intro/* to /intro/index.html with a 200, so the browser keeps the name — and then
# Gatsby hydrates, does not recognise the path as a route, and REPLACES the URL with /intro/.
# By the time the component's useEffect reads location.pathname the name is gone, the
# `seg === "intro"` guard fires, and nothing is sent. Beacon present, endpoint healthy, link
# correct, zero hits.
#
# Link 4 is easy to get wrong in the other direction: grepping the HTML finds nothing even when
# the beacon IS deployed, because a useEffect compiles into a LAZILY-LOADED page chunk that the
# HTML only references by hash. An earlier version of this script grepped the HTML and reported
# MISSING against a working beacon.
#
# Usage:  sh scripts/deck-check.sh            # collector + is the beacon in the bundle
#         sh scripts/deck-check.sh --probe    # also print a URL to open in a REAL BROWSER

set -eu

ENV_FILE="$HOME/.applypilot/.env"
val() { grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\'' '; }

TOKEN=$(val DECK_HITS_TOKEN)
HITS_URL=$(val DECK_HITS_URL)
DECK_URL=$(val INTRO_DECK_URL)
[ -n "$DECK_URL" ] || DECK_URL="https://www.jorgealejandrodiez.com/intro/"

[ -n "$HITS_URL" ] || { echo "DECK_HITS_URL is not set in $ENV_FILE"; exit 1; }
[ -n "$TOKEN" ]    || { echo "DECK_HITS_TOKEN is not set in $ENV_FILE"; exit 1; }

# ── link 4: is the beacon in the JavaScript that actually ships? ─────────────
DECK_URL="$DECK_URL" python3 - <<'PY'
import os, re, sys, urllib.request

deck = os.environ["DECK_URL"].rstrip("/")
base = "/".join(deck.split("/")[:3])

def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

print("beacon in the shipped JavaScript:")
try:
    html = get(f"{deck}/zzcheck-not-a-person")
except Exception as e:
    print(f"  could not load the page: {e}"); sys.exit(0)

# The page chunk is loaded by the webpack runtime, not named in the HTML, so walk the runtime's
# hash table and try each chunk. Grepping the HTML alone finds nothing even when it is there.
srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
runtime = next((s for s in srcs if "webpack-runtime" in s), None)
found = None
if runtime:
    rt = get(base + runtime)
    for cid, h in re.findall(r'(\d+):"([a-f0-9]{8,})"', rt):
        for name in (f"{cid}-{h}.js", f"component---src-pages-intro-js-{h}.js"):
            try:
                js = get(f"{base}/{name}")
            except Exception:
                continue
            if "deck-hit" in js or "sendBeacon" in js:
                found = name
            break
        if found:
            break
print(f"  FOUND in {found}" if found
      else "  NOT FOUND in any chunk — the beacon is not deployed (README step 3)")
PY

# ── links 1-3: what the collector has ───────────────────────────────────────
echo
echo "collector ($HITS_URL):"
curl -fsS -H "Authorization: Bearer $TOKEN" "$HITS_URL" | python3 -c '
import json, sys
hits = json.load(sys.stdin).get("hits", [])
print("  %d hit(s) stored" % len(hits))
for h in hits[-10:]:
    print("    %s  %s" % (h["at"][:19], h["slug"]))
' || echo "  could not read the collector"

if [ "${1:-}" = "--probe" ]; then
  SLUG="zzprobe-$(date +%s)"
  echo
  echo "Open this in a REAL BROWSER, wait ~15s, then re-run this script:"
  echo "  ${DECK_URL%/}/$SLUG"
  echo
  echo "curl will NOT do: the beacon is JavaScript and curl runs none of it."
  echo
  echo "Watch the address bar. If it changes to ${DECK_URL%/}/ before the page settles, the"
  echo "router has stripped the name and the beacon cannot see it — that is link 5, and it is"
  echo "the failure that produced 98 sent emails and zero recorded opens."
  echo
  echo "Reads lag ~10s behind writes; a slug missing immediately is not yet a failure."
fi
