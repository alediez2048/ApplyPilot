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

try:
    html = get(f"{deck}/zzcheck-not-a-person")
except Exception as e:
    print(f"  could not load the page: {e}"); sys.exit(0)

# ── link 5: can the beacon still SEE the name when it runs? ──────────────────
# The parse-time capture lands in the HTML itself (unlike the beacon, which compiles into a
# chunk), so this one IS greppable. Checked FIRST because it is the link that broke.
print("name captured before the router rewrites it:")
if "__deckSlug" in html:
    print("  YES — captured at parse time, the router cannot get there first")
else:
    print("  NO — nothing captures the name before hydration.")
    print("  If the beacon below reads location.pathname in a useEffect, it will see '/intro/'")
    print("  and send nothing. That is README step 3, and it is what produced 98 sent emails")
    print("  and zero recorded opens.")

print()
print("beacon in the shipped JavaScript:")

# Resolve the intro page's chunk the way Gatsby itself does, not by guessing:
#   page-data.json -> componentChunkName -> the runtime's id for that name -> its hash.
# A brute-force walk over the runtime's hash table LOOKS equivalent and is not: it depends on
# a URL shape that changes between builds, and it reported NOT FOUND against a live beacon
# once already. Anything that answers "is the tracking deployed" has to be right both ways.
found = None
try:
    import json
    pd = json.loads(get(f"{base}/page-data/intro/page-data.json"))
    chunk_name = pd.get("componentChunkName", "")
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    runtime = next((x for x in srcs if "webpack-runtime" in x), None)
    rt = get(base + runtime) if runtime else ""
    cid = re.search(r'(\d+):"' + re.escape(chunk_name) + r'"', rt)
    h = re.search(cid.group(1) + r':"([a-f0-9]{8,})"', rt) if cid else None
    if h:
        js = get(f"{base}/{chunk_name}-{h.group(1)}.js")
        if "deck-hit" in js or "sendBeacon" in js:
            found = f"{chunk_name}-{h.group(1)}.js"
        else:
            print(f"  NOT FOUND — {chunk_name}-{h.group(1)}.js has no beacon in it")
except Exception as e:
    print(f"  could not resolve the page chunk: {e}")
if found:
    print(f"  FOUND in {found}")
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
