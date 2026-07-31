// Records one intro-deck click. Deploy to the site that HOSTS the deck.
//
// This is a first-party beacon on the sender's own page, not a tracking pixel in an email.
// The distinction is the whole reason the feature exists: a pixel fires when Gmail caches the
// image and when a corporate scanner opens the message, so it measures machines. This fires
// when a browser loads the deck page — a person chose to look.
//
// It stores ONLY the name segment from the URL. No IP, no user-agent, no referrer, no cookie.

import { getStore } from "@netlify/blobs";

const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,38}$/;
const KEY = "hits";
const MAX = 500;   // rolling window; ApplyPilot re-reads the whole thing and dedupes.

export default async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

  let slug = "";
  try {
    const body = await req.json();
    slug = String(body?.slug ?? body?.v ?? "").trim().toLowerCase();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  // A hit with no valid slug is a page view by someone who did not arrive from an email —
  // /intro/ itself, or a shared link with the name stripped. 204, not 400: it is not an
  // error, there is simply nothing to attribute.
  if (!SLUG_RE.test(slug)) return new Response(null, { status: 204 });

  const store = getStore("deck-hits");
  const existing = (await store.get(KEY, { type: "json" })) ?? [];
  existing.push({ slug, at: new Date().toISOString() });

  // Trim from the FRONT so the newest survive. An unbounded blob would grow forever on a page
  // that is meant to be shared.
  await store.setJSON(KEY, existing.slice(-MAX));
  return new Response(null, { status: 204 });
};

export const config = { path: "/api/deck-hit" };
