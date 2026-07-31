"""Per-contact intro-deck links — the one engagement signal worth trusting.

Email *open* tracking was considered and rejected. It needs a 1×1 pixel, and every layer between
you and the reader now defeats it: Gmail proxies and caches every image on delivery, Apple Mail
Privacy Protection pre-fetches all remote images by default, and corporate security gateways
fetch everything to scan it. You would get "Gina opened your email" from her employer's spam
filter and could not tell it from a real read. A confidently wrong signal is worse than none —
it would drive follow-up decisions that the data cannot support.

A **click** is different. Nobody's spam filter follows a link and reads a deck. Somebody chose
to look. And because the deck is hosted on the sender's OWN site, the click can be counted
without a tracking pixel, without third-party analytics, and without anything embedded in the
message body beyond a link the recipient can see.

The token is derived, not stored-then-looked-up: `token_for()` is a pure function of the contact
id plus a per-install secret, so the mapping survives a database restore and no lookup table can
drift out of sync with it.
"""

from __future__ import annotations

import hashlib
import hmac
import re

#: Short enough to look tidy in a URL, long enough that guessing one is pointless. 8 hex chars
#: is 4 billion; the token grants nothing anyway — it identifies a click, it does not authorise.
TOKEN_LEN = 8

#: The query parameter. `v` for "visitor" — short, and not obviously a tracker, because a URL
#: that reads as instrumentation invites people not to click it.
TOKEN_PARAM = "v"

_TOKEN_RE = re.compile(rf"\b[0-9a-f]{{{TOKEN_LEN}}}\b")


def token_for(contact_id: str, secret: str) -> str:
    """Stable, opaque token for one contact.

    HMAC rather than a plain hash so the tokens cannot be enumerated by anyone who guesses the
    id scheme — contact ids are a hash of (job, identity) and are therefore reproducible by
    anyone who knows the inputs. `secret` is per-install.

    Deliberately NOT random-and-stored: derived means a database restore, a re-discovered
    contact, or a second machine all produce the same token, and there is no table to fall out
    of step with the links already sitting in somebody's inbox.
    """
    cid = (contact_id or "").strip()
    if not cid:
        return ""
    return hmac.new((secret or "").encode(), cid.encode(), hashlib.sha256).hexdigest()[:TOKEN_LEN]


def deck_url(base: str, token: str) -> str:
    """Append the token to the deck link, preserving whatever query it already carries.

    Returns `base` untouched when there is no token — a link that still works is better than a
    malformed one, and an un-attributed click is a smaller loss than a broken deck.
    """
    base = (base or "").strip()
    if not base or not token:
        return base
    if f"{TOKEN_PARAM}=" in base:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{TOKEN_PARAM}={token}"


def strip_token(url: str) -> str:
    """The deck link without its token — for comparing two URLs that differ only by visitor."""
    return re.sub(rf"[?&]{TOKEN_PARAM}=[0-9a-f]+", "", url or "").rstrip("?&")


def tokens_in(text: str | None) -> set[str]:
    """Every token-shaped string in a blob of text.

    The import path: paste an analytics export, a server log, a Vercel/Cloudflare log — anything
    containing the URLs that were hit. Scanning for the SHAPE rather than parsing one specific
    format is what lets this accept all of them without a per-provider adapter.
    """
    if not text:
        return set()
    # Only tokens that appear as our query parameter, so a random 8-hex string elsewhere in a
    # log line (a request id, a git sha) cannot be mistaken for a visitor.
    return {m.group(1) for m in re.finditer(rf"[?&]{TOKEN_PARAM}=([0-9a-f]{{{TOKEN_LEN}}})\b",
                                            text)}


def hits_from_payload(payload) -> list[dict]:
    """Normalise whatever the collector returned into [{token, at}].

    Accepts the shapes a hand-rolled endpoint actually produces — a bare list of tokens, a list
    of objects, or an object wrapping either under `hits`/`events`/`data` — because the endpoint
    is something the operator deploys and edits, and rejecting their JSON on a key name is a
    silly way to lose a click. Anything unrecognised yields [] rather than raising.
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
            token, at = item, ""
        elif isinstance(item, dict):
            token = str(item.get("v") or item.get("token") or item.get("id") or "")
            at = str(item.get("at") or item.get("ts") or item.get("time") or "")
        else:
            continue
        # A raw URL is a legitimate item too — take the token out of it.
        found = tokens_in(token)
        if found:
            token = next(iter(found))
        token = token.strip().lower()
        if _TOKEN_RE.fullmatch(token):
            out.append({"token": token, "at": at})
    return out


def match_contacts(tokens: set[str], contacts: list[dict], secret: str) -> list[dict]:
    """Which contacts those tokens belong to.

    Returns the contact dicts, not ids, because the caller invariably wants the name to show.
    A token with no matching contact is silently ignored — it is most likely a deleted contact
    or a link from a different install, and neither is worth an error.
    """
    if not tokens:
        return []
    by_token = {token_for(c["id"], secret): c for c in contacts if c.get("id")}
    return [by_token[t] for t in tokens if t in by_token]
