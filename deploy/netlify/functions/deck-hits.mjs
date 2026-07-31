// Hands the recorded clicks to ApplyPilot. Bearer-token protected.
//
// ApplyPilot runs on 127.0.0.1 and cannot receive a webhook — deliberately, and that property
// is worth keeping. So it POLLS this endpoint over outbound HTTPS, which needs no inbound
// exposure on the operator's machine at all.
//
// Reads without draining. `mark_deck_viewed` is idempotent (re-seeing a click bumps a counter
// and announces nothing), so a rolling window that is read repeatedly is simpler and safer than
// a destructive read that loses hits if the caller dies mid-response.

import { getStore } from "@netlify/blobs";

export default async (req) => {
  const expected = Netlify.env.get("DECK_HITS_TOKEN");
  if (!expected) {
    // Refuse rather than serve openly. A collector that answers everyone because its config is
    // missing is worse than one that is down: it looks like it is working.
    return new Response("collector not configured", { status: 503 });
  }
  const provided = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  // Constant-time-ish: compare lengths first, then whole strings. The token guards a list of
  // opaque ids, so this is defence in depth rather than the only thing standing in the way.
  if (provided.length !== expected.length || provided !== expected) {
    return new Response("unauthorized", { status: 401 });
  }

  const store = getStore("deck-hits");
  const hits = (await store.get("hits", { type: "json" })) ?? [];
  return new Response(JSON.stringify({ hits }), {
    status: 200,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
};

export const config = { path: "/api/deck-hits" };
