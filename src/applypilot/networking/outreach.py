"""LLM outreach drafting, a short, specific email per contact.

Reuses the multi-provider LLM client and the tailor JSON-extraction + sanitize
helpers. Produces {subject, body}; the user reviews/edits before any send (NET-4).
"""

from __future__ import annotations

import logging
import os
import re

from applypilot.llm import get_client
from applypilot.scoring.tailor import extract_json
from applypilot.scoring.validator import sanitize_text

log = logging.getLogger(__name__)

_LINKEDIN_LIMIT = 300
#: A DM to someone you are ALREADY connected to is not a connection-request note: no 300-char
#: cap, and links are not penalised. Warm notes were being cut at 300 anyway, contradicting the
#: warm prompt that tells the model the cap does not apply, so they arrived truncated with an
#: ellipsis. Still bounded, because a DM should not be an essay.
_LINKEDIN_DM_LIMIT = 900

_SYSTEM = """You write short, casual networking messages for a job seeker reaching out to
someone at a company they just applied to. Think: a friendly, real message you'd actually
send another human, warm, a little personable, genuinely curious. NOT a cover letter, NOT a
hard sell, NOT corporate.

Voice:
- Casual and conversational. Use contractions ("I'm", "I'd", "it's"). Sound like a real person,
  not a template.
- A touch of genuine warmth or personality is great, keep it grounded, never cheesy or fake.
- Absolutely no buzzwords, no "I hope this finds you well", no "I am writing to", no
  "leverage/synergy/circle back". If it sounds like HR wrote it, rewrite it.
- Never invent facts about the sender, and don't flatter the recipient with made-up specifics.
- NEVER attach a number of years to a specific tool or framework unless the profile explicitly
  says so. A total career length is TOTAL experience, never "N years of PyTorch/LangChain/etc."
  Prefer honest framing like "the last few years focused on AI engineering" over false tenure.

Produce TWO things:

1. An EMAIL (subject + body):
   - 3-4 short sentences. Open warm and human, not stiff.
   - Name the SPECIFIC role the sender applied to and the company, plus one real, relevant thing
     about the sender (from their profile).
   - CALL TO ACTION: invite them to a quick call to connect. If a SCHEDULING LINK is provided
     below, weave it in naturally so they can grab a time directly (e.g. "if you're open to a
     quick call, grab a time that works here: <link>"). If no link is provided, just suggest a
     short call/chat. Keep it low-pressure, not pushy.
   - Sign off casually with the sender's first name only. No signature block. The ONLY link
     allowed is the scheduling link (when provided).
   - Subject: short, casual, specific (e.g. "quick q about the <role> role").

2. A LINKEDIN connection note (linkedin_note):
   - MUST be 300 characters or fewer (hard limit, count carefully, aim for ~230).
   - 1-2 warm sentences. Shorter and friendlier than the email; it's a connection request note.
   - Mention the role + a quick genuine hook, and that you'd love to connect and maybe find a
     time to chat. Do NOT paste the scheduling link here (LinkedIn connect notes strip/penalize
     links and space is tight), just express interest in connecting/talking.
   - Sign with the first name.

If the user provides a STYLE DIRECTION below, follow it closely while keeping the messages
honest, casual, and concise.

- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the clearest signal that text was pasted out of a chatbot, and a reader who spots one re-reads the whole message as machine-written. Use a comma, a full stop, or rewrite the sentence. A plain hyphen in a compound word ("large-scale") is fine.

Return ONLY a JSON object: {"subject": "...", "body": "...", "linkedin_note": "..."}"""


def _scheduling_link(profile: dict) -> str:
    """The sender's calendar/scheduling link (Calendly, cal.com, Google appt schedule, …).

    Priority: SCHEDULING_LINK env → profile['personal']['scheduling_link'] → ''. When present, the
    email CTA invites a call and includes this link so recipients can book a time directly.
    """
    import os
    return (os.environ.get("SCHEDULING_LINK", "").strip()
            or ((profile or {}).get("personal", {}).get("scheduling_link") or "").strip())


#: The exact sentence the operator asked for, verbatim. The LLM is TOLD to include the link,
#: but a prompt instruction is not a guarantee, so `ensure_intro_deck()` appends this line
#: when the model leaves it out. Every outreach email carries the deck; LinkedIn notes never do.
INTRO_DECK_SENTENCE = "Here's a good intro deck we could go over during the call: {url}"


def _flag(name: str) -> bool:
    return (os.environ.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _intro_deck_url(profile: dict, contact: dict | None = None) -> str:
    """The intro-deck LINK offered in every outreach email.

    Distinct from `INTRO_DECK_PATH`, which ATTACHES a PDF. Priority mirrors
    `_scheduling_link`: INTRO_DECK_URL env → profile['personal']['intro_deck_url'] → ''.

    When a contact is supplied the link carries their NAME as a path segment , 
    `/intro/gina`, not `/intro/?v=9b83068a`. Both identify the reader; only one looks like it.
    A token in a query string is the shape people have been trained to distrust, and it
    undercuts the warm tone of the one message where that tone is the whole point. See
    `domain/deck.py`.
    """
    base = (os.environ.get("INTRO_DECK_URL", "").strip()
            or ((profile or {}).get("personal", {}).get("intro_deck_url") or "").strip())
    if not base or not contact or not contact.get("id"):
        return base

    # OFF until the site can actually serve /intro/<name>. Learned the hard way: the link
    # scheme was switched while the Netlify rewrite was still sitting uncommitted, so every
    # freshly-written draft pointed at a 404 on the live site. A personalised link that does
    # not resolve is far worse than an un-attributed one that does, it costs the conversation,
    # which is the entire point of sending it.
    #
    # `applypilot doctor` checks the live URL and tells you when to turn this on.
    if not _flag("INTRO_DECK_PATHS"):
        return base

    from applypilot.domain import deck
    from applypilot.networking.store import ensure_deck_slug
    try:
        slug = ensure_deck_slug(contact["id"], contact.get("full_name") or "")
    except Exception:  # noqa: BLE001
        log.debug("Could not assign a deck slug", exc_info=True)
        return base          # a plain deck link still works; an un-attributed click is cheap
    return deck.deck_url(base, slug)


def ensure_intro_deck(body: str, url: str) -> str:
    """Guarantee the deck link is in `body`, appending the standard sentence if it is missing.

    The prompt asks for it; this makes it true. A model that drops the link, paraphrases the
    URL, or splits it across lines would otherwise silently ship an email without the one thing
    the operator asked to be in every email. Idempotent: an existing link is left exactly as the
    model wrote it, so a naturally-phrased mention is preserved rather than duplicated.
    """
    if not url:
        return body
    if url.rstrip("/") in (body or "").replace("\n", " ").rstrip("/"):
        return body
    sentence = INTRO_DECK_SENTENCE.format(url=url)
    body = (body or "").rstrip()

    # Sit ABOVE the sign-off, a link under "Thanks, / Alejandro" reads as a footer and gets
    # skimmed past. The sign-off is the final paragraph, and it is a BLOCK ("Thanks," and the
    # name are two lines of one paragraph); splitting it and inserting between the two lines is
    # the bug this replaced.
    paras = [p for p in body.split("\n\n")]
    if len(paras) >= 2 and _looks_like_signoff(paras[-1]):
        paras.insert(len(paras) - 1, sentence)
        return "\n\n".join(p.strip("\n") for p in paras)
    return f"{body}\n\n{sentence}"


def _looks_like_signoff(para: str) -> bool:
    """A closing block: at most three short lines, e.g. "Thanks,\\nAlejandro".

    Length is the signal, not a keyword list, the drafts sign off as "Thanks", "Best",
    "Cheers", or just the bare first name, and a keyword list would miss whichever one the
    model invents next.
    """
    lines = [ln.strip() for ln in para.strip().split("\n") if ln.strip()]
    return bool(lines) and len(lines) <= 3 and all(len(ln) <= 40 for ln in lines)


def _resolve_style(profile: dict, style: str = "") -> str:
    """The custom style directive, in priority order: explicit arg → env → profile field."""
    return (
        (style or "").strip()
        or os.environ.get("OUTREACH_STYLE", "").strip()
        or ((profile or {}).get("outreach_style") or "").strip()
    )


def _sender_name(profile: dict) -> str:
    p = (profile or {}).get("personal", {})
    full = p.get("preferred_name") or p.get("full_name") or ""
    return full.split()[0] if full else "there"


def sender_background(profile: dict) -> list[str]:
    """Real, groundable facts about the sender, for any prompt that makes claims about them.

    Extracted so cold outreach and REPLIES draw on the same source. A reply is where this
    matters most and where it was missing: answer a recruiter's "do you have experience with X?"
    without the real background in the prompt and the model invents a plausible yes, a
    fabricated claim, made directly to the person who can check it, in a live conversation
    (§Lessons 9, with the stakes raised).

    Prefers the LinkedIn-derived block: it is the accurate one, and it is what stops "10 years
    of PyTorch" when that is the whole career length.
    """
    personal = (profile or {}).get("personal", {})
    experience = (profile or {}).get("experience", {})
    bits = [
        f"Sender name: {personal.get('full_name', '')}",
        f"Sender first name: {_sender_name(profile)}",
    ]
    li = (profile or {}).get("linkedin") or {}
    if li.get("about") or li.get("roles"):
        if li.get("headline"):
            bits.append(f"Sender headline: {li['headline']}")
        if li.get("about"):
            bits.append(f"Sender background (LinkedIn About): {li['about']}")
        roles = li.get("roles") or []
        if roles:
            recent = "; ".join(f"{r.get('title','')} at {r.get('company','')} ({r.get('dates','')})"
                               for r in roles[:4])
            bits.append(f"Recent roles: {recent}")
        if li.get("positioning"):
            bits.append(f"IMPORTANT framing (do not misstate): {li['positioning']}")
    else:
        # Fallback to the older fields only if no LinkedIn block is present.
        skills = (profile or {}).get("skills_boundary", {})
        bits += [
            f"Sender target role: {experience.get('target_role', '')}",
            f"Total years of experience: {experience.get('years_of_experience_total', '')}",
            f"Sender skills: {', '.join((skills.get('frameworks') or []))[:200]}",
        ]
    return bits


def draft_variant(*, warm: bool = False, noticed: bool = False, jd_chars: int = 0,
                  deck: bool = False, scheduling: bool = False, style: bool = False) -> str:
    """A compact signature of WHAT WENT INTO a draft, e.g. "cold+jd2k+deck+cal".

    Reply rate without this is a single number that can only go up or down for reasons nobody
    can name. After 77 emails and 2 replies there was no way to ask "did the personalised ones
    do better", so every improvement to the copy was unfalsifiable, which is the real ceiling
    on the whole outreach system.

    Records the INPUTS, not a version number. A version number goes stale the moment a prompt is
    edited and silently pools two different things under one label; a signature of the inputs
    stays true because it describes what actually happened for that message.

    `jd_chars` is bucketed rather than exact, every draft would otherwise be its own variant
    and nothing would ever accumulate an n worth reading.
    """
    bits = ["warm" if warm else "cold"]
    if jd_chars:
        bits.append(f"jd{min(9, max(1, round(jd_chars / 1000)))}k")
    if noticed:
        bits.append("noticed")
    if deck:
        bits.append("deck")
    if scheduling:
        bits.append("cal")
    if style:
        bits.append("style")
    return "+".join(bits)


def draft_email(profile: dict, job: dict, contact: dict, style: str = "", warm: bool = False) -> dict:
    """Return {"subject": str, "body": str} for one contact. Raises on LLM/parse failure.

    `style` is an optional free-text directive (e.g. "keep it super casual", "mention I'm a
    Longhorn", "make it a little witty") that steers the tone. Falls back to OUTREACH_STYLE env
    or profile["outreach_style"] via _resolve_style.

    `warm=True` = the HOT layer: this person is an EXISTING 1st-degree LinkedIn connection at the
    company. The copy should acknowledge the existing relationship (reconnect, not cold intro),
    and the LinkedIn note becomes a direct MESSAGE to a connection (not a connect request).
    """
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "your company"
    sender_bits = sender_background(profile)
    # The parts of the posting that say what the JOB IS, not the first 1200 characters, which
    # on a real description is the mission statement, the org chart, and then the role starting
    # exactly where the budget ran out. See domain/jobdesc.py for the measurement.
    from applypilot.domain.jobdesc import role_essentials
    jd = role_essentials(job.get("full_description"))
    noticed = (contact.get("noticed") or "").strip()[:400]

    directive = _resolve_style(profile, style)
    style_block = f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else ""

    if warm:
        relationship = ("EXISTING 1st-degree LinkedIn connection who currently works at the company "
                        "(you already know each other).")
        warm_block = (
            "WARM / HOT OUTREACH, you are ALREADY CONNECTED with this person on LinkedIn and they "
            "work at this company. Write it as reconnecting with someone you know, NOT a cold intro:\n"
            f"- The FIRST LINE must acknowledge the gap since you last spoke AND name where they "
            f"work, the shape of \"Hey <name>, long time without connecting, hope everything is "
            f"well at {company}\". Vary the wording, keep that content.\n"
            "- Never introduce yourself as a stranger and never explain who you are as though they "
            "don't know you, no 'I'm a technical PM with 10+ years'. They already know you.\n"
            "- Mention you just applied for the role at their company and would love their read on it, "
            "an internal referral, or just to reconnect.\n"
            "- The LinkedIn note is a DIRECT MESSAGE to an existing connection (NOT a connection "
            "request), it can be a bit longer/warmer and does not need the 300-char connect-note "
            "limit framing, though still keep it concise. Ignore the 'connection request note' "
            "framing in the rules above; you are already connected.\n"
            "- BOTH messages open the same reconnecting way. The LinkedIn DM needs that opening "
            "even more than the email does, because it lands in a chat thread where your last "
            "exchange is visible right above it.\n"
            "- The intro deck link belongs in the LinkedIn DM as well as the email. A link is "
            "fine in a DM; only connection-REQUEST notes penalise them.\n\n"
        )
    else:
        relationship = contact.get("match_reason", "works at the company")
        warm_block = ""

    link = _scheduling_link(profile)
    sched_block = (
        f"SCHEDULING LINK (include in the EMAIL CTA so they can book a call directly): {link}\n\n"
        if link else
        "SCHEDULING LINK: none provided, invite a quick call/chat without a link.\n\n"
    )
    deck = _intro_deck_url(profile, contact)
    deck_block = (
        f"INTRO DECK LINK (include in the EMAIL, not the LinkedIn note): {deck}\n"
        f'Offer it as "{INTRO_DECK_SENTENCE.format(url=deck)}", or the same idea in your own '
        "words, as long as the full URL appears verbatim. Put it near the call CTA, before the "
        "sign-off.\n\n"
        if deck else ""
    )

    user = (
        "SENDER:\n" + "\n".join(sender_bits) + "\n\n"
        "TARGET CONTACT:\n"
        f"Name: {contact.get('full_name', '')}\n"
        f"Title: {contact.get('title', '')}\n"
        f"Relationship: {relationship}\n\n"
        f"JOB APPLIED TO:\nRole: {role}\nCompany: {company}\n"
        f"WHAT THE ROLE ACTUALLY INVOLVES (from the posting, the specific thing to react to):\n"
        f"{jd}\n\n"
        # The operator saw something on their profile and wrote it down. This is the ONE piece
        # of genuinely person-specific input available, so it takes precedence over the posting
        #, but it must be used as a human would use it, not announced.
        + (f"WHAT THE SENDER NOTICED ABOUT THIS PERSON (verbatim, from looking at their "
           f"profile):\n{noticed}\n"
           "How to use it:\n"
           "- ENGAGE WITH THE SUBSTANCE. NEVER ANNOUNCE THE NOTICING. Any sentence whose job is "
           "to report that you looked, \"I noticed your…\", \"I saw your…\", \"I came across "
           "your…\", \"your recent post about…\", is the single most recognisable "
           "automated-outreach shape there is, and a recruiter reads several a week. It is the "
           "SHAPE that is banned, not a list of verbs: if the sentence could be deleted and the "
           "observation still stand on its own, delete it.\n"
           "  Wrong shape: \"I noticed your post about the ferry timetable problem.\"\n"
           "  Right shape: \"Nine different ferry timetables and no single source of truth is "
           "the kind of thing that never makes it into a job description.\"\n"
           "- It is an ADDITION, not a replacement. The email must still say what the role "
           "involves and what the sender has actually done. An email that is only the "
           "observation is a compliment, not an application.\n"
           "- If it does not fit this email naturally, leave it out entirely. A forced "
           "reference is worse than none.\n\n" if noticed else "")
        + sched_block + deck_block + warm_block + style_block +
        "Write the outreach email. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=400, temperature=0.8,  # a bit higher for warmth/variety
    )
    variant = draft_variant(warm=warm, noticed=bool(noticed), jd_chars=len(jd),
                            deck=bool(deck), scheduling=bool(link), style=bool(directive))
    data = extract_json(raw)
    subject = sanitize_text(str(data.get("subject", ""))).strip()
    body = sanitize_text(str(data.get("body", ""))).strip()
    note = sanitize_text(str(data.get("linkedin_note", ""))).strip()
    if not subject:
        subject = f"Question about the {role} role"
    if not body:
        raise ValueError("empty outreach body")
    # Not left to the prompt: the deck goes in EVERY outreach email.
    body = ensure_intro_deck(body, deck)
    if warm:
        # A DM to an existing connection: no connect-note cap, and links are fine in a chat
        # thread. Cap FIRST so the URL can never be the thing that gets truncated.
        note = ensure_intro_deck(_cap_linkedin(note, _LINKEDIN_DM_LIMIT), deck)
    else:
        # A cold CONNECTION REQUEST note. Still no deck: LinkedIn strips/penalises links in
        # invite notes, and a 41-char URL is 14% of a 300-char budget that is already tight.
        note = _cap_linkedin(note)
    return {"subject": subject, "body": body, "linkedin_note": note, "variant": variant}


_FOLLOWUP_SYSTEM = """You write short follow-up emails for a job seeker who already emailed
someone at a company they applied to and got no reply.

Hard rules, a bad follow-up costs more than no follow-up:
- SHORT. Two or three sentences. Shorter than the first email, always.
- Never guilt, never "just bumping this", never "per my last email", never imply they owe a reply.
- Do NOT restate the original email. They can scroll down; it is in the same thread.
- ALWAYS give them an easy out, an explicit line saying it's fine to say no or that you'll
  stop. This is what separates persistent from spammy.
- No buzzwords, no "I hope this finds you well", no corporate voice. Contractions, real person.
- Never invent facts about the sender. Never attach years to a specific tool or framework.

- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the clearest signal that text was pasted out of a chatbot, and a reader who spots one re-reads the whole message as machine-written. Use a comma, a full stop, or rewrite the sentence. A plain hyphen in a compound word ("large-scale") is fine.

Return ONLY JSON: {"subject": "...", "body": "..."}
The subject MUST be the original subject prefixed with "Re: " so it threads naturally."""

# What each touch is FOR. A follow-up that doesn't change with position reads as an autoresponder.
_TOUCH_INTENT = {
    1: ("Second touch (~2 days later). Brief, friendly nudge. Add ONE new, concrete thing, a "
        "detail about their work, or something the sender shipped that's relevant. Give them an out."),
    2: ("Third touch (~4 days later). Shorter still. Offer to make it easy: ask whether it's "
        "worth pursuing at all, or whether someone else there is the better person to talk to."),
    3: ("Final touch (~7 days later). This is the LAST message, say so plainly and warmly. "
        "Close the loop with no pressure, leave the door open, and do not ask a question that "
        "demands a reply."),
}


def draft_followup(profile: dict, job: dict, contact: dict, touch: int = 1,
                   style: str = "", touches: list | None = None) -> dict:
    """Draft follow-up #`touch` for a contact who was emailed and hasn't replied.

    `touch` is 1-based: 1 = the first follow-up (second message overall). Returns
    {"subject", "body"}. Raises on LLM/parse failure, like draft_email.

    `touches` is every follow-up ALREADY SENT on this channel. Without it this function saw
    only `contact.outreach_message`, the first email, so touch 2 did not know what touch 1
    said and touch 3 knew neither. The prompt has always instructed "do NOT repeat it" while
    being shown a third of what there was not to repeat, and the result was three messages
    making the same offer in slightly different words.

    `conversation_transcript` already assembles exactly this and `_draft_reply` already passes
    it; this path simply never did. Same shape as §Lessons 39, a function able to take the
    context, called without it.
    """
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "the company"
    original_subject = (contact.get("outreach_subject") or f"Question about the {role} role").strip()
    intent = _TOUCH_INTENT.get(max(1, min(touch, 3)), _TOUCH_INTENT[3])
    directive = _resolve_style(profile, style)
    link = _scheduling_link(profile)
    deck = _intro_deck_url(profile, contact)

    sent_on = (contact.get("submitted_at") or "")[:10]

    # EVERYTHING already said on this thread, not just the first email.
    transcript = conversation_transcript(contact, touches=touches)
    # Has the deck link already gone out? If so this follow-up must not re-pitch it.
    #
    # Compared against the BASE url, never the personalised one. Caught on live data: the
    # earlier emails went out as ".../intro/" and INTRO_DECK_PATHS now builds ".../intro/michael",
    # so matching the full link found nothing and cheerfully re-pitched the deck to a man who
    # had already been sent it twice. What matters is "have they been given the deck", and every
    # variant shares the base. Trailing slash and case normalised for the same reason.
    deck_base = _intro_deck_url(profile) or deck        # no contact → the un-personalised URL
    deck_sent = bool(deck_base) and deck_base.rstrip("/").lower() in transcript.lower()

    user = (
        f"SENDER: {_sender_name(profile)}\n"
        f"TARGET: {contact.get('full_name', '')}, {contact.get('title', '')} at {company}\n"
        f"ROLE APPLIED FOR: {role}\n"
        f"ORIGINAL SUBJECT (reuse it with a 'Re: ' prefix): {original_subject}\n"
        f"ORIGINAL EMAIL SENT: {sent_on or 'recently'}, no reply since.\n\n"
        f"EVERYTHING YOU HAVE ALREADY SENT THEM, do NOT repeat any of it, in any words:\n"
        f"{transcript or (contact.get('outreach_message') or '')[:700]}\n\n"
        f"THIS FOLLOW-UP: {intent}\n\n"
        + (f"SCHEDULING LINK (optional, only if it fits naturally): {link}\n\n" if link else "")
        # Offered ONCE. Re-pitching the same link in every touch is the single most automated-
        # sounding thing this sequence did: four messages, four times "here's a deck". If they
        # have it, the only honest move is a light reference, and even that is optional.
        + ("INTRO DECK: already sent, the link is in the thread above. Do NOT paste it again "
           "and do NOT re-pitch it. You may refer to it in passing at most once (\"the deck I "
           "sent\"), and only if it is genuinely relevant to this message.\n\n" if deck_sent
           else (f"INTRO DECK LINK (include it, they have NOT been sent it): {deck}\n"
                 f'Offer it as "{INTRO_DECK_SENTENCE.format(url=deck)}", or the same idea in '
                 "fewer words, the full URL must appear verbatim. This is the concrete thing "
                 "this follow-up offers, so lead with it rather than tacking it on.\n\n"
                 if deck else ""))
        + (f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else "")
        + "Write the follow-up. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system", "content": _FOLLOWUP_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=300, temperature=0.7,
    )
    data = extract_json(raw)
    subject = sanitize_text(str(data.get("subject", ""))).strip()
    body = sanitize_text(str(data.get("body", ""))).strip()
    if not subject:
        subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
    if not body:
        raise ValueError("empty follow-up body")
    # Force-append ONLY when they have never been sent it. `ensure_intro_deck` exists because a
    # prompt instruction is not a guarantee (§Lessons 9, 12), but applied unconditionally it
    # guaranteed the repetition instead, re-adding the link to touch 2 and 3 even when the model
    # had correctly left it out.
    if not deck_sent:
        body = ensure_intro_deck(body, deck)
    return {"subject": subject, "body": body}


_LI_FOLLOWUP_SYSTEM = """You write short LinkedIn follow-up messages for a job seeker.

The situation: they sent a connection request with a note, the person ACCEPTED, and then
never replied. Accepting is a small yes, treat it as mild interest, not as being ignored.

Hard rules:
- VERY short. LinkedIn is a chat window, not email. 2-4 sentences, no salutation block,
  no sign-off with a full name. Write like a DM to a colleague.
- They already read your connect note. Do NOT repeat it.
- Never say "just following up", "bumping this", "circling back", or "per my message".
- Ask ONE easy, specific question they can answer in a sentence. A question is better than
  a statement here, because a chat message with no question gets no reply.
- Give them an out. One short clause is enough.
- No buzzwords, no corporate voice. Contractions. Lowercase-casual is fine.
- Never invent facts about the sender. Never attach years to a specific tool or framework.

- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the clearest signal that text was pasted out of a chatbot, and a reader who spots one re-reads the whole message as machine-written. Use a comma, a full stop, or rewrite the sentence. A plain hyphen in a compound word ("large-scale") is fine.

Return ONLY JSON: {"message": "..."}"""

_LI_TOUCH_INTENT = {
    1: ("They accepted but never replied. Thank them briefly for connecting, then ask one "
        "specific question about the role or the team, something a recruiter can answer fast."),
    2: ("Second nudge. Shorter. Offer an easy redirect: ask whether they're the right person "
        "for this, or who is."),
    3: ("Final message. Say plainly it's the last one, keep the door open, no question that "
        "demands an answer."),
}


def draft_for_channel(channel: str, profile: dict, job: dict, contact: dict,
                      touch: int = 1, style: str = "", thread: list | None = None,
                      touches: list | None = None) -> dict:
    """One entry point per channel, returning ONE shape: {"subject", "body"}.

    The drafters below return different keys for historical reasons (email has a subject
    line, a LinkedIn DM does not, a text certainly does not). Normalising here is what lets
    the dashboard's follow-up handler stop branching on channel, adding SMS was adding a
    row to this map, not another `if` in the request handler.
    """
    if channel == "linkedin":
        return {"subject": "", "body": draft_linkedin_followup(
            profile, job, contact, touch=touch, style=style,
            messages=contact.get("interactions"))["message"]}
    if channel == "sms":
        return {"subject": "", "body": draft_sms(
            profile, job, contact, touch=touch, style=style, thread=thread)["message"]}
    return draft_followup(profile, job, contact, touch=touch, style=style, touches=touches)


_REPLY_SYSTEM = """You write a reply for a job seeker ANSWERING someone who just wrote to them
about a role they applied for.

This is not outreach and not a follow-up. They replied, the hard part already worked. The only
job here is to answer what they actually said, and to make the next step easy.

Hard rules:
- ANSWER THE MESSAGE. If they asked something, answer it first, in the first sentence.
- SHORT. Two to four sentences. They are reading it on a phone between meetings.
- Never re-pitch. They already know who the sender is and what they want; repeating the original
  email is the single fastest way to sound automated.
- Never thank them for "taking the time" or open with "I hope this finds you well".
- Match their register. A two-line reply gets a two-line answer, not a paragraph.
- Never invent facts about the sender, and never attach years to a specific tool or framework.
  If they asked about experience the ABOUT YOU block does not cover, do NOT manufacture a yes.
  Say what is actually true and adjacent, or offer to talk it through. A confident invented
  claim goes straight to the one person positioned to check it.
- NEVER INVENT AN IDENTIFIER. No job IDs, requisition numbers, dates, ticket numbers or URLs
  that are not given to you verbatim in THIS PROMPT. If they ask for one and it is not in the
  JOB block below, do not produce a number, link the posting, or say you will send it across.
  A recruiter can check a req ID in five seconds, and a wrong one is worse than no answer.
- If they introduced a colleague, acknowledge it and address the new person naturally.
- If they said no, be gracious and brief and do not argue or ask them to reconsider.

- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the clearest signal that text was pasted out of a chatbot, and a reader who spots one re-reads the whole message as machine-written. Use a comma, a full stop, or rewrite the sentence. A plain hyphen in a compound word ("large-scale") is fine.

Return ONLY JSON: {"subject": "...", "body": "..."}
The subject MUST keep the thread's existing subject with a "Re: " prefix."""


#: Requisition ids as ATS platforms actually mint them, Workday `JR349466`, Greenhouse/Lever
#: numeric ids, `REQ-1234`. Extracted from the posting URL because a recruiter asking "which
#: req?" is the single most common factual question a reply contains, and the answer is sitting
#: in a field we already hold.
_REQ_ID = re.compile(r"(?:^|[_/\-?&=])((?:JR|REQ|R)[-_]?\d{4,}|\d{6,})(?:[_/\-?&]|$)",
                     re.IGNORECASE)


def job_facts(job: dict) -> str:
    """The checkable details of the posting, verbatim, for a prompt that must not invent them.

    Written after a drafted reply answered "do you have the job ID?" with **7894521**, a number
    that exists nowhere, while the real `JR349466` sat in the job URL the drafter was never
    given. A fabricated identifier goes to the one person who can verify it in five seconds.
    """
    url = (job.get("url") or "").strip()
    bits = []
    if job.get("title"):
        bits.append(f"Job title (exact): {job['title']}")
    if url:
        bits.append(f"Posting URL (the only link you may send): {url}")
        m = _REQ_ID.search(url)
        if m:
            bits.append(f"Requisition ID from that URL: {m.group(1)}")
    if not bits:
        return "No posting details on file, do NOT invent a job ID, link or date."
    bits.append("These are the ONLY job identifiers you may state. Anything else, say you will "
                "send it across rather than guessing.")
    return "\n".join(bits)


def conversation_transcript(contact: dict, thread: list | None = None,
                            touches: list | None = None, their_reply: str = "") -> str:
    """The whole exchange, in order, as the model should read it.

    Assembled from three separate stores, which is exactly why it is worth having in one
    function: the first email lives on `contacts`, every follow-up lives in `touches`, and the
    reply lives in `messages`. A draft written from any one of them repeats what the other two
    already said, the specific way an automated-sounding reply gets written.
    """
    lines = []
    first = (contact.get("outreach_message") or "").strip()
    if first:
        subj = (contact.get("outreach_subject") or "").strip()
        when = (contact.get("submitted_at") or "")[:10]
        lines.append(f"[1] YOU wrote{f' on {when}' if when else ''}"
                     f"{f', subject: {subj}' if subj else ''}:\n{first[:900]}")
    n = len(lines) + 1
    for t in (touches or []):
        body = (t.get("body") or "").strip()
        if not body:
            continue
        when = (t.get("sent_at") or "")[:10]
        lines.append(f"[{n}] YOU followed up{f' on {when}' if when else ''}:\n{body[:600]}")
        n += 1
    if their_reply.strip():
        who = _last_inbound(thread).get("from_name") or "THEY"
        when = (_last_inbound(thread).get("sent_at") or "")[:10]
        lines.append(f"[{n}] {who.upper()} REPLIED{f' on {when}' if when else ''}:\n"
                     f"{their_reply.strip()}")
    return "\n\n".join(lines)


def _last_inbound(thread: list | None) -> dict:
    inbound = [m for m in (thread or [])
               if isinstance(m, dict) and (m.get("direction") or "") == "in"]
    return inbound[-1] if inbound else {}


def draft_reply(profile: dict, job: dict, contact: dict, thread: list | None = None,
                subject: str = "", style: str = "", their_reply: str = "",
                touches: list | None = None) -> dict:
    """Draft an answer to a live conversation, from the WHOLE sequence.

    `their_reply` is what the other person actually said. Two ways it gets here and the
    function does not care which: the stored snippet when `gmail.readonly` was granted
    (CRM-4b), or text the operator pasted in. It refuses without it, deliberately, a
    "contextual" reply written with no context is a generic follow-up wearing a `Re:` subject
    line, and it would look like a working feature until somebody read it.

    `style` is the same free-text vibe knob as cold outreach ("more casual", "shorter", "add a
    joke"), resolved through `_resolve_style` so OUTREACH_STYLE and the profile default apply
    here too, one tone control for the whole product, not a second one that drifts.
    """
    from applypilot.domain import conversations as cv, intent as _intent

    last = _last_inbound(thread)
    if not last:
        raise ValueError("nothing to reply to, no inbound message on this thread")
    said = (their_reply or last.get("snippet") or "").strip()
    if not said:
        raise ValueError("no reply text, paste what they wrote, or enable reply content")

    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "the company"
    who = last.get("from_name") or last.get("from_addr") or contact.get("full_name") or "them"
    label = _intent.suggestion(_intent.classify(said))
    directive = _resolve_style(profile, style)
    link = _scheduling_link(profile)

    # Who else is on the thread, so the draft can acknowledge an introduction by name rather
    # than writing to one person while two people are reading.
    others = [cv.display_name(x) or cv.addr(x)
              for x in (last.get("cc_addrs") or []) if cv.addr(x)]
    transcript = conversation_transcript(contact, thread, touches, said)

    user = (
        # The sender's REAL background. Without it the model answers "do you have experience
        # with X?" by inventing a confident yes, to the one person who can check it.
        "ABOUT YOU (use ONLY these facts; if they do not cover the question, say so plainly "
        "rather than inventing an answer):\n" + "\n".join(sender_background(profile)) + "\n\n"
        f"REPLYING TO: {who}, {contact.get('title', '')} at {company}\n"
        f"ROLE YOU APPLIED FOR: {role}\n"
        f"SUBJECT (reuse with 'Re: '): {subject or last.get('subject') or role}\n"
        f"\nJOB (verbatim facts, never state an identifier that is not here):\n"
        f"{job_facts(job)}\n"
        + (f"ALSO ON THE THREAD (they are reading too): {', '.join(others)}\n" if others else "")
        + (f"WHAT THEIR REPLY LOOKS LIKE: {label['label']}, {label['action']}\n"
           if label["label"] else "")
        + f"\nTHE CONVERSATION SO FAR, in order:\n{transcript}\n\n"
        + "Everything above marked YOU is already in their inbox. Do not repeat any of it.\n\n"
        + (f"SCHEDULING LINK (use it only if they want to talk): {link}\n\n" if link else "")
        + (f"STYLE DIRECTION (follow closely, it overrides the default voice):\n{directive}\n\n"
           if directive else "")
        + "Write the reply. Answer what they said. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system", "content": _REPLY_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=350, temperature=0.7,
    )
    data = extract_json(raw)
    out_subject = sanitize_text(str(data.get("subject", ""))).strip()
    body = sanitize_text(str(data.get("body", ""))).strip()
    if not body:
        raise ValueError("empty reply body")
    base = subject or last.get("subject") or role
    if not out_subject:
        out_subject = base if base.lower().startswith("re:") else f"Re: {base}"
    # Deliberately NO intro-deck sentence. That belongs to cold outreach and follow-ups, where
    # the goal is to earn a reply. Bolting it onto an answer inside a live conversation is the
    # marketing reflex that makes a real exchange read like a sequence.
    return {"subject": out_subject, "body": body, "intent": label["intent"]}


def _li_state(contact: dict, sent_on: str, messages: list | None) -> str:
    """What is actually true about this LinkedIn conversation, in one line for the prompt."""
    logged = [m for m in (messages or []) if m.get("kind") in ("linkedin_in", "linkedin_out")]
    if not logged:
        return (f"They accepted the invite{f' around {sent_on}' if sent_on else ''} "
                f"and have not replied.")
    lines = "\n".join(
        f"  {'THEM' if m.get('kind') == 'linkedin_in' else 'YOU'}: "
        f"{(m.get('detail') or '')[:400]}"
        for m in reversed(logged))
    return ("You have already exchanged messages on LinkedIn. Do NOT re-introduce yourself and "
            "do NOT repeat anything below:\n" + lines)


def draft_linkedin_followup(profile: dict, job: dict, contact: dict, touch: int = 1,
                            style: str = "", messages: list | None = None) -> dict:
    """Draft LinkedIn follow-up #`touch` for a contact who connected but went quiet.

    Returns {"message": str}. This is a DIRECT MESSAGE to an existing 1st-degree
    connection, so the 300-char connection-note cap does NOT apply, but brevity still
    matters far more than in email, because it lands in a chat window.

    `messages` is the operator-logged LinkedIn exchange (UX-2). Without it this prompt states
    "they have not replied" unconditionally — which is a claim, not an observation, and becomes
    false the moment anything is logged. Two instructions in one prompt disagreeing is a code
    bug, not a wording problem (§Lessons 40): the fix is to describe the actual state, not to
    say the other side louder.
    """
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "the company"
    intent = _LI_TOUCH_INTENT.get(max(1, min(touch, 3)), _LI_TOUCH_INTENT[3])
    directive = _resolve_style(profile, style)
    deck = _intro_deck_url(profile, contact)
    sent_on = (contact.get("dm_sent_at") or "")[:10]

    user = (
        f"SENDER: {_sender_name(profile)}\n"
        f"TARGET: {contact.get('full_name', '')}, {contact.get('title', '')} at {company}\n"
        f"ROLE APPLIED FOR: {role}\n"
        f"CONNECTION NOTE THEY ALREADY READ (do NOT repeat it):\n"
        f"{(contact.get('linkedin_message') or '')[:400]}\n"
        f"{_li_state(contact, sent_on, messages)}\n\n"
        f"THIS MESSAGE: {intent}\n\n"
        + (f"INTRO DECK LINK (include it): {deck}\n"
           "This is a DM to an existing connection, so a link is fine here, LinkedIn only "
           "penalises them in connection-request notes. Offer it in your own words; the full "
           "URL must appear verbatim.\n\n" if deck else "")
        + (f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else "")
        + "Write the LinkedIn follow-up. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system", "content": _LI_FOLLOWUP_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=250, temperature=0.75,
    )
    data = extract_json(raw)
    msg = sanitize_text(str(data.get("message", ""))).strip()
    if not msg:
        raise ValueError("empty LinkedIn follow-up")
    # Cap first, then add the link, so trimming can never produce a broken half-URL.
    msg = ensure_intro_deck(_cap_linkedin(msg, _LINKEDIN_DM_LIMIT), deck)
    return {"message": msg}


#: Two SMS segments. iMessage has no practical cap, but the number may not be an iPhone and a
#: text is read on a lock screen either way, length is the whole discipline of the channel.
_SMS_LIMIT = 320

_SMS_SYSTEM = """You write a SHORT TEXT MESSAGE for a job seeker reaching a recruiter or hiring
manager about a role.

A text is the most intrusive channel there is. It arrives on a lock screen, at whatever hour it
is sent, mixed in with messages from their family. Email waits to be opened; a text interrupts.
Everything below follows from that one fact.

**The sender was not given permission to text.** In most cases the number came from a data tool,
not from the recipient. That is the entire difficulty of this message, and pretending otherwise
is what makes a text like this land badly. The message has to earn the channel in its first
line, and the way it does that is by being SHORT, IDENTIFIED, and OBVIOUSLY EASY TO IGNORE.

Hard rules, in priority order:

1. SAY WHO YOU ARE FIRST. Full first name, in the opening clause. They do not have this number
   saved; an unidentified text is deleted unread. This is the rule that separates a text from
   every other channel.

2. GIVE THE CONNECTIVE TISSUE IMMEDIATELY. In the same sentence or the next, name the real prior
   touchpoint: applied for a specific role, emailed on a specific day, met somewhere, spoke
   before. "I applied for the Applied AI Engineer role last week" is the sentence that turns a
   stranger into a candidate they can place. Without it the message is a cold sales text.

3. ACKNOWLEDGE THE CHANNEL AND OFFER A RETREAT. One short clause conceding that a text is a
   liberty, and offering to continue somewhere less intrusive. This is what separates
   respectful from presumptuous and it is the single most-skipped move.

   PHRASE IT DIFFERENTLY EVERY TIME, IN YOUR OWN WORDS. Several people at the SAME company get
   texted, and a stock sentence repeated across them is worse than omitting it, it proves the
   message was generated. Do NOT use the wording "hope a text is okay" or "happy to move this
   back to email"; those are the phrasings this instruction keeps producing and they are now
   burned. Find your own, and vary the position, it does not have to be the second sentence.

4. NEVER SAY WHERE THE NUMBER CAME FROM. Naming a data provider is worse than saying nothing , 
   it tells them they were looked up. Do not mention it, and do not invent a story either.

5. ASK PERMISSION, NOT TIME. One yes/no a busy person can answer at a traffic light, whether
   this is an okay way to reach them, or whether they mind receiving something here. Never ask
   for a block of calendar time from someone who does not yet know who is asking. Vary this
   too; do not ask "is this a good number to reach you" every time, and never phrase it so it
   sounds like an automated number-verification check.

6. MAKE IT EASY TO IGNORE, EXPLICITLY. "No worries if not" / "totally fine to ignore this."
   Counter-intuitive and load-bearing: giving someone permission to say no is what makes them
   comfortable enough to say yes.

7. TWO TO FOUR SENTENCES. Under 320 characters TOTAL. One block, no line breaks, no paragraphs.

8. NO LINKS. A URL from an unrecognised number is the strongest spam signal that exists, and
   carriers filter on it. Anything worth linking is sent after they reply.

9. NO PRESSURE OF ANY KIND. No deadlines, no "just following up before I move on", no other-offer
   leverage, no urgency. Scarcity tactics from an unknown number read as a scam.

10. No greeting line, no sign-off, nobody signs a text. Contractions, plain words, how a person
    actually types. No emoji, no exclamation marks, no slang.

11. Never invent facts about the sender, never attach years to a specific tool or framework, and
    never invent a mutual contact or a prior meeting that is not stated below.

12. NAME THE ROLE LIKE A PERSON WOULD. The role title below is a raw database field and is often
    malformed, a scraper artifact, a tracking suffix, the word "uploaded", a requisition number.
    Read it, and if it is not something a human would say out loud, describe the role naturally
    instead ("the engineering role", "the role on your team") or lean on the company name. A
    text that says "the Betterup uploaded job" tells the reader a machine wrote it. Never repair
    a broken title by GUESSING what it was meant to say, drop it and stay vague.

- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the clearest signal that text was pasted out of a chatbot, and a reader who spots one re-reads the whole message as machine-written. Use a comma, a full stop, or rewrite the sentence. A plain hyphen in a compound word ("large-scale") is fine.

Return ONLY JSON: {"message": "..."}"""

#: What each text is FOR. Position 0 is the first one, the only one where the sender is a
#: stranger holding their number.
#:
#: The ladder is deliberately short and slow (3d / 7d, two touches). Chasing silence over text is
#: how a candidate becomes a nuisance: the same three-touch cadence that is normal in email reads
#: as harassment on a phone, because each one interrupts.
_SMS_TOUCH_INTENT = {
    0: ("FIRST text, the one that has to earn the channel. Identify the sender, give the real "
        "prior touchpoint, acknowledge that a text is a liberty and offer to move back to email, "
        "ask ONE yes/no question, and make it explicitly fine to ignore."),
    1: ("Second and SECOND-TO-LAST text (~3 days later). They saw the first and did not answer, "
        "which is information: assume busy, never assume rude. Shorter than the first. Add ONE "
        "new concrete thing, never a restatement. Do not express disappointment, do not say "
        "'just following up', and do not re-introduce yourself beyond a two-word reminder."),
    2: ("FINAL text (~7 days later). Say plainly and warmly that it is the last one, so they know "
        "the channel is closing and feel no obligation. Ask NOTHING that demands a reply. Leave "
        "the door genuinely open and thank them for their time without being effusive. This "
        "message should be readable as a kindness, not a guilt trip."),
}

#: Used INSTEAD of the touch ladder when they have replied. Not a variant of it, a different
#: message with a different job, which is why it replaces rather than appends.
_SMS_CONTINUATION_INTENT = (
    "This is a CONTINUATION of a live conversation, not outreach. They wrote back; the hard "
    "part already worked. Do not earn the channel, do not re-introduce the sender beyond their "
    "first name, do not name the role as though they might not know it, and above all NEVER ask "
    "whether an earlier message arrived, it did, they answered it, and asking implies they did "
    "not. Read what they last said and move exactly one thing forward: answer their question, "
    "give the availability they asked for, or ask where the process stands. Two sentences. If a "
    "text adds nothing over replying in the existing email thread, say the smaller thing.")

#: How much standing the sender actually has to be texting at all, worst to best. This is the
#: biggest single lever on the copy and it was previously reduced to "did we email them".
def _sms_permission(contact: dict) -> str:
    replied = bool((contact.get("replied_at") or "").strip())
    emailed = bool((contact.get("sent_message_id") or "").strip())
    invited = (contact.get("dm_status") or "") in ("sent", "manual")
    if replied:
        return ("STRONGEST FOOTING, AND A DIFFERENT MESSAGE ENTIRELY. They have already REPLIED. "
                "A live conversation exists, so this is NOT outreach and NOT a nudge on silence, "
                "writing it as one insults them by implying they never answered, which is the "
                "single worst thing this message can do. Do not say 'following up on my email', "
                "do not ask whether it arrived, and do not re-pitch. Pick up where the exchange "
                "left off: answer or advance the thing they last raised. Still identify yourself "
                "once, briefly, replying by email does not mean they saved this number.")
    if emailed and invited:
        return ("MODERATE. An email and a LinkedIn invite have both gone out unanswered. Two "
                "channels of silence is a real signal: this text must be noticeably shorter and "
                "gentler than either, and must NOT read as escalation. Do not enumerate the "
                "attempts, 'I emailed and connected on LinkedIn' sounds like a list of "
                "grievances. Name one, lightly.")
    if emailed:
        return ("MODERATE. An email went out and has not been answered. The text should reference "
                "it in one clause so it lands as a nudge on something real, not a new front.")
    if invited:
        return ("WEAK. Only a LinkedIn invite has gone out, and there is no reply. Mention it in "
                "one clause as the connective tissue, and lean harder on the out.")
    return ("WEAKEST, TREAT WITH CARE. There has been NO prior contact of any kind: no email, no "
            "LinkedIn, nothing. This person did not give out this number and has never heard from "
            "the sender. The ONLY defensible framing is the job application itself, which must be "
            "named specifically and early. Be the shortest of any version, apologise for the "
            "channel in a few words without grovelling, and make ignoring it the easiest possible "
            "response. Do not be charming. Do not sell. Ask one small yes/no question and stop.")


def draft_sms(profile: dict, job: dict, contact: dict, touch: int = 0,
              style: str = "", thread: list | None = None) -> dict:
    """Draft a text message. `touch` 0 is the first one; 1+ are follow-ups.

    Returns {"message": str}. NEVER sends, the operator copies this, opens Messages and
    pastes. Driving a messaging app from outside is the mistake this codebase already made
    twice with LinkedIn (§Lessons 3), and Apple gives no send API at all.

    Deliberately no intro-deck link: `_intro_deck_url` is not consulted here. A URL from a
    number you do not recognise is the single strongest spam signal, and unlike LinkedIn's
    penalty this one costs the whole conversation rather than some reach.
    """
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "the company"
    # The touch ladder describes COLD outreach, earn the channel, give the touchpoint, ask a
    # yes/no. For someone who has already replied every one of those is wrong, and because it
    # arrives under the heading "THIS MESSAGE:" it beat the permission block every time: the
    # draft for a contact who had answered still asked whether the email had arrived. A
    # contradiction in a prompt is not fixed by saying the other side louder.
    if (contact.get("replied_at") or "").strip():
        intent = _SMS_CONTINUATION_INTENT
    else:
        intent = _SMS_TOUCH_INTENT.get(max(0, min(touch, 2)), _SMS_TOUCH_INTENT[2])
    directive = _resolve_style(profile, style)
    # Two different dates that are easy to conflate: the JOB was applied to on job.applied_at,
    # and the outreach EMAIL went out on contact.submitted_at (which is what the email ladder
    # anchors on). Handing the model the email date labelled "applied" puts a checkable factual
    # error in a message to the one person positioned to check it.
    applied_on = (job.get("applied_at") or "")[:10]
    emailed_on = (contact.get("submitted_at") or "")[:10]
    replied = bool((contact.get("replied_at") or "").strip())
    # Only text we actually HOLD counts. `gmail.metadata` gives headers with no body, so a
    # thread can exist with every snippet empty, which is indistinguishable from no thread
    # for this purpose, and must be treated as such rather than as context.
    #
    # This is the REPLY TEXT, not a flag, because `conversation_transcript` uses the thread
    # only for the replier's name and date, the words have to be handed to it separately as
    # `their_reply`. Passing the thread alone renders the sender's own email and nothing else,
    # and the model correctly answered "only Alejandro's initial email is shown" rather than
    # inventing a continuation.
    said = ""
    if replied:
        said = next((m.get("snippet") or "" for m in reversed(thread or [])
                     if m.get("direction") == "in" and (m.get("snippet") or "").strip()), "")
        said = said.strip()

    user = (
        f"SENDER: {_sender_name(profile)}\n"
        f"TARGET: {contact.get('full_name', '')}, {contact.get('title', '')} at {company}\n"
        f"ROLE APPLIED FOR: {role}"
        + (f" (applied {applied_on})" if applied_on else "") + "\n\n"
        # How much standing there is to be texting at all. This drives the opening line, the
        # length, and how hard the message has to work to justify itself, it is a bigger lever
        # on the copy than anything else in this prompt.
        f"HOW MUCH STANDING THE SENDER HAS HERE:\n{_sms_permission(contact)}\n"
        + (f"\nThe email that went unanswered was sent {emailed_on} to "
           f"{contact.get('email') or 'them'}.\n"
           if (contact.get("sent_message_id") or "").strip()
           and not (contact.get("replied_at") or "").strip() else "")
        # A contact who replied needs the actual exchange or the message cannot advance
        # anything, and a "continuation" written with no context is just a follow-up on
        # silence, addressed to someone who did not go silent. Same principle `_draft_reply`
        # already enforces by refusing outright.
        + (f"\nTHE CONVERSATION SO FAR, continue THIS, do not restart it:\n"
           f"{conversation_transcript(contact, thread, their_reply=said)}\n" if said else "")
        + ("\nYou do NOT have the text of their reply. Do not pretend to reference it, do not "
           "guess what they said, and do not fall back to 'following up on my email', they "
           "answered. Acknowledge that you are already in touch, keep it to one or two lines, "
           "and offer one concrete next step the sender can state without knowing their words "
           "(availability to talk, or asking where the process stands).\n"
           if replied and not said else "")
        + f"\nTHIS MESSAGE: {intent}\n\n"
        + (f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else "")
        + f"Write the text message. Under {_SMS_LIMIT} characters. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system", "content": _SMS_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=200, temperature=0.75,
    )
    data = extract_json(raw)
    msg = sanitize_text(str(data.get("message", ""))).strip()
    if not msg:
        raise ValueError("empty SMS draft")
    # A text is one block. Models reach for an email shape (greeting, paragraph, sign-off) even
    # when told not to, and newlines are what make it look like one on a phone.
    msg = " ".join(msg.split())
    return {"message": _cap_linkedin(msg, _SMS_LIMIT)}


def _cap_linkedin(note: str, limit: int = _LINKEDIN_LIMIT) -> str:
    """Trim to `limit` chars at a word boundary. Default is the connection-note cap.

    Always applied BEFORE the deck link is added, never after: trimming a message that ends in
    a URL produces a broken half-link, which is worse than no link at all.
    """
    if len(note) <= limit:
        return note
    cut = note[:limit - 1]  # leave room for the ellipsis
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"
