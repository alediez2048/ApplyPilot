#!/bin/sh
# Is intro-deck click tracking actually working?
#
# The chain has four links and only the last one is hard to see:
#
#   1. emails carry /intro/<name>      2. the page serves
#   3. the collector stores + reads    4. THE PAGE CALLS THE COLLECTOR
#
# On 2026-08-05 links 1-3 were all healthy and link 4 had never existed: the collector held two
# hits in five days, both verification probes, while ~70 named links went out. The August
# verification checked write 204, invalid slug 204, wrong method 405, unauthenticated 401,
# authenticated read round-trips, page 200 — every one of which tests the API or a status code,
# and none of which touches whether the page calls anything.
#
# So this checks link 4 first, and says which failure it is.
#
# Usage:  sh scripts/deck-check.sh            # report state
#         sh scripts/deck-check.sh --probe    # also print a throwaway URL to open in a BROWSER

set -eu

ENV_FILE="$HOME/.applypilot/.env"
val() { grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\'' '; }

TOKEN=$(val DECK_HITS_TOKEN)
HITS_URL=$(val DECK_HITS_URL)
DECK_URL=$(val INTRO_DECK_URL)
[ -n "$DECK_URL" ] || DECK_URL="https://www.jorgealejandrodiez.com/intro/"

[ -n "$HITS_URL" ] || { echo "DECK_HITS_URL is not set in $ENV_FILE"; exit 1; }
[ -n "$TOKEN" ]    || { echo "DECK_HITS_TOKEN is not set in $ENV_FILE"; exit 1; }

# ── link 4: is the beacon actually on the page a recipient loads? ────────────
# Fetched with a made-up name so nothing is attributed to a real contact.
PROBE_PAGE="${DECK_URL%/}/zzcheck-not-a-person"
echo "beacon on the page ($PROBE_PAGE):"
if curl -fsS "$PROBE_PAGE" 2>/dev/null | grep -qE 'deck-hit|/api/deck|sendBeacon'; then
  echo "  FOUND — the page references the collector"
else
  echo "  MISSING — the page never calls /api/deck-hit."
  echo "  That is step 3 of deploy/netlify/README.md, and it is a DIFFERENT failure from a"
  echo "  broken collector. Both look identical if you only test the API."
fi

# ── links 1-3: what the collector has ───────────────────────────────────────
echo
echo "collector ($HITS_URL):"
curl -fsS -H "Authorization: Bearer $TOKEN" "$HITS_URL" | python3 -c '
import json, sys
hits = json.load(sys.stdin).get("hits", [])
print(f"  {len(hits)} hit(s) stored")
for h in hits[-10:]:
    print("    %s  %s" % (h["at"][:19], h["slug"]))
' || echo "  could not read the collector"

if [ "${1:-}" = "--probe" ]; then
  echo
  echo "Open this in a REAL BROWSER, then re-run this script:"
  echo "  ${DECK_URL%/}/zzprobe-$(date +%s)"
  echo
  echo "curl will NOT do. The beacon is JavaScript; curl fetches the HTML and runs none of it,"
  echo "so a curl'd page never reports a hit even when everything is working perfectly."
fi
