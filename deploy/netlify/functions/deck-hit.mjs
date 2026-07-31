// Records one intro-deck click. Deploy to the site that HOSTS the deck.
//
// This is a first-party beacon on the sender's own page, not a tracking pixel in an email.
// The distinction is the whole reason the feature exists: a pixel fires when Gmail caches the
// image and when a corporate scanner opens the message, so it measures machines. This fires
// when a browser loads the deck page — a person chose to look.
//
// It stores ONLY the opaque token from the URL. No IP, no user-agent, no referrer, no cookie.
// The token means nothing to anyone without the ApplyPilot install secret, so this endpoint
// leaks nothing even if the blob store is read.

import { getStore } from "@netlify/blobs";

const TOKEN_RE = /^[0-9a-f]{8}$/;
const KEY = "hits";
const MAX = 500;   // rolling window; ApplyPilot re-reads the whole thing and dedupes.

export default async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

  let token = "";
  try {
    const body = await req.json();
    token = String(body?.v ?? "").trim().toLowerCase();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  // A hit with no valid token is a page view by someone who did not arrive from an email.
  // 204, not 400: it is not an error, there is simply nothing to attribute.
  if (!TOKEN_RE.test(token)) return new Response(null, { status: 204 });

  const store = getStore("deck-hits");
  const existing = (await store.get(KEY, { type: "json" })) ?? [];
  existing.push({ v: token, at: new Date().toISOString() });

  // Trim from the FRONT so the newest survive. An unbounded blob would grow forever on a page
  // that is meant to be shared.
  await store.setJSON(KEY, existing.slice(-MAX));
  return new Response(null, { status: 204 });
};

export const config = { path: "/api/deck-hit" };
