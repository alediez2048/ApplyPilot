"""Per-person intro-deck links — the one engagement signal worth trusting.

Email *open* tracking was considered and rejected. It needs a 1×1 pixel, and every layer between
you and the reader defeats it: Gmail proxies and caches every image on delivery, Apple Mail
Privacy Protection pre-fetches all remote images by default, and corporate gateways fetch
everything to scan it. You would get "Gina opened your email" from her employer's spam filter
and could not tell it from a real read.

A **click** is different. Nobody's spam filter follows a link and reads a deck. And because the
deck is hosted on the sender's own site, the click can be counted with no pixel, no third-party
analytics, and nothing in the message beyond a link the recipient can see.

**The link is a NAMED PATH, not a tracking parameter.**

    https://www.jorgealejandrodiez.com/intro/gina        ← reads as "I made this for you"
    https://www.jorgealejandrodiez.com/intro/?v=9b83068a ← reads as surveillance

Both identify the reader; only one looks like it. The first version used an opaque token, and
a token in a query string is exactly the shape people have been trained to distrust — it
undercuts the warm, personal tone the email is trying to strike, in the one message where that
tone is the whole point. A path segment carrying their first name looks deliberate, because it
is: the deck really was sent to them.

The honest trade, stated plainly: an opaque token means nothing to anyone who sees it, while a
name is readable. If the link is forwarded, the new reader learns who it was for. That is a
small cost, and personalisation is a real benefit rather than a disguise.
"""

from __future__ import annotations

import re
import unicodedata

#: Reserved because they are (or could become) real pages under /intro/.
_RESERVED = {"index", "assets", "static", "api", "admin", "deck", "slides", "intro"}

_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")


def slugify(name: str | None) -> str:
    """A person's name as a clean URL segment: "Gina Johnson" -> "gina".

    First name only. A full name makes a long URL and reads like a database record; a first
    name reads like it was written for them. Accents are folded rather than percent-encoded,
    because "%C3%A9" in a link is exactly the ugliness this replaced.
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    first = raw.split()[0]
    folded = unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:38]
    return "" if not slug or slug in _RESERVED else slug


def disambiguate(slug: str, taken: set[str], full_name: str | None = None) -> str:
    """Keep a slug unique without making it ugly.

    Two Ginas is the normal case, not an edge case. The second gets a last initial
    ("gina-j"), and only if that is also taken does it fall back to a number. Nobody looking
    at `/intro/gina-j` thinks they are being tracked; `/intro/gina-2f9c` is where it starts
    looking like a token again, which is the thing being fixed.
    """
    if not slug:
        return ""
    if slug not in taken:
        return slug
    parts = (full_name or "").split()
    if len(parts) > 1:
        initial = slugify(parts[-1])[:1]
        if initial and f"{slug}-{initial}" not in taken:
            return f"{slug}-{initial}"
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


def deck_url(base: str, slug: str) -> str:
    """The deck link for one person: base + /slug, query string untouched.

    Returns `base` unchanged when there is no slug — an un-attributed click is a far smaller
    loss than a broken deck link, and a link that 404s costs the actual conversation.
    """
    base = (base or "").strip()
    if not base or not slug:
        return base
    head, sep, query = base.partition("?")
    return f"{head.rstrip('/')}/{slug}" + (sep + query if sep else "")


def strip_slug(url: str, slug: str) -> str:
    """The deck link without the person — for comparing two URLs that differ only by reader."""
    if not slug:
        return url or ""
    return re.sub(rf"/{re.escape(slug)}(/|$|\?)", r"\1", url or "")


def slugs_in(text: str | None, known: set[str] | None = None) -> set[str]:
    """Every deck slug in a blob of text — an analytics export, a server log, a pasted list.

    Scans for the SHAPE (a path segment under /intro/) rather than parsing one provider's
    format, which is what lets this accept Plausible, Vercel, Cloudflare and a hand-pasted list
    without an adapter per source.

    `known` narrows to slugs actually issued. Without it a log line for /intro/assets or a
    genuine sub-page would be read as a visitor — the path is a much broader namespace than a
    query parameter was, so this filter matters more than it did.
    """
    if not text:
        return set()
    found = {m.group(1).lower()
             for m in re.finditer(r"/intro/([A-Za-z0-9][A-Za-z0-9-]{0,38})(?=[/?\s\"'&#]|$)",
                                  text)}
    found = {s for s in found if _SLUG_OK.match(s) and s not in _RESERVED}
    return {s for s in found if s in known} if known is not None else found


#: Any deck link, in either scheme: the old `?v=<hex>` token or a named path. The trailing
#: lookahead is what keeps a sentence's full stop out of the match — the drafts really do end
#: "…/intro/?v=4e49a7ad." and swallowing that period would rewrite the sentence, not the link.
_ANY_DECK_LINK = re.compile(
    r"https?://[^\s<>\"')]*?/intro/?(?:\?v=[0-9a-f]+|[A-Za-z0-9][A-Za-z0-9-]{0,38})?"
    r"(?=[\s<>\"')]|[.,;!?](?:\s|$)|$)")


def relink(text: str | None, new_url: str) -> tuple[str, int]:
    """Point every deck link in `text` at `new_url`. Returns (text, replacements).

    For updating drafts that were written under an older link scheme WITHOUT regenerating
    them. Regeneration is the obvious move and the wrong one: it spends an LLM call, and it
    rewrites copy the operator may have already read and edited, to change a URL. Swapping the
    link preserves every other word.
    """
    if not text or not new_url:
        return text or "", 0

    # Only OUR deck. The host comes from `new_url`, so nothing has to be passed in and a link
    # to somebody else's /intro/ page — a post, an article — is left alone. Without this the
    # match is "any /intro/ URL anywhere", which is a rewrite waiting to mangle a real citation.
    host = re.sub(r"^https?://", "", new_url).split("/")[0].lower()

    def _swap(m: re.Match) -> str:
        got = re.sub(r"^https?://", "", m.group(0)).split("/")[0].lower()
        return new_url if got == host else m.group(0)

    out = _ANY_DECK_LINK.sub(_swap, text)
    n = sum(1 for m in _ANY_DECK_LINK.finditer(text)
            if re.sub(r"^https?://", "", m.group(0)).split("/")[0].lower() == host)
    return out, n


def hits_from_payload(payload) -> list[dict]:
    """Normalise whatever the collector returned into [{slug, at}].

    Accepts the shapes a hand-rolled endpoint actually produces — a bare list, a list of
    objects, or an object wrapping either — because the endpoint is something the operator
    deploys and edits, and rejecting their JSON over a key name is a silly way to lose a click.
    """
    if isinstance(payload, dict):
        for key in ("hits", "events", "data", "results"):
            if key in payload:
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if isinstance(item, str):
            slug, at = item, ""
        elif isinstance(item, dict):
            slug = str(item.get("slug") or item.get("v") or item.get("path") or
                       item.get("id") or "")
            at = str(item.get("at") or item.get("ts") or item.get("time") or "")
        else:
            continue
        found = slugs_in(slug)
        if found:
            slug = next(iter(found))
        slug = slug.strip().strip("/").lower()
        if _SLUG_OK.match(slug) and slug not in _RESERVED:
            out.append({"slug": slug, "at": at})
    return out


def match_contacts(slugs: set[str], contacts: list[dict]) -> list[dict]:
    """Which contacts those slugs belong to.

    Reads the slug STORED on each contact. Unlike the old token it cannot be derived — two
    people are often called Gina — so the assignment is recorded when the link is first made
    and is the only thing that can resolve it afterwards.
    """
    if not slugs:
        return []
    by_slug = {(c.get("deck_slug") or "").lower(): c for c in contacts if c.get("deck_slug")}
    return [by_slug[s] for s in slugs if s in by_slug]
