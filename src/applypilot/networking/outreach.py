"""LLM outreach drafting — a short, specific email per contact.

Reuses the multi-provider LLM client and the tailor JSON-extraction + sanitize
helpers. Produces {subject, body}; the user reviews/edits before any send (NET-4).
"""

from __future__ import annotations

import logging
import os

from applypilot.llm import get_client
from applypilot.scoring.tailor import extract_json
from applypilot.scoring.validator import sanitize_text

log = logging.getLogger(__name__)

_LINKEDIN_LIMIT = 300
#: A DM to someone you are ALREADY connected to is not a connection-request note: no 300-char
#: cap, and links are not penalised. Warm notes were being cut at 300 anyway, contradicting the
#: warm prompt that tells the model the cap does not apply — so they arrived truncated with an
#: ellipsis. Still bounded, because a DM should not be an essay.
_LINKEDIN_DM_LIMIT = 900

_SYSTEM = """You write short, casual networking messages for a job seeker reaching out to
someone at a company they just applied to. Think: a friendly, real message you'd actually
send another human — warm, a little personable, genuinely curious. NOT a cover letter, NOT a
hard sell, NOT corporate.

Voice:
- Casual and conversational. Use contractions ("I'm", "I'd", "it's"). Sound like a real person,
  not a template.
- A touch of genuine warmth or personality is great — keep it grounded, never cheesy or fake.
- Absolutely no buzzwords, no "I hope this finds you well", no "I am writing to", no
  "leverage/synergy/circle back". If it sounds like HR wrote it, rewrite it.
- Never invent facts about the sender, and don't flatter the recipient with made-up specifics.
- NEVER attach a number of years to a specific tool or framework unless the profile explicitly
  says so. A total career length is TOTAL experience — never "N years of PyTorch/LangChain/etc."
  Prefer honest framing like "the last few years focused on AI engineering" over false tenure.

Produce TWO things:

1. An EMAIL (subject + body):
   - 3–4 short sentences. Open warm and human, not stiff.
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
   - MUST be 300 characters or fewer (hard limit — count carefully, aim for ~230).
   - 1–2 warm sentences. Shorter and friendlier than the email; it's a connection request note.
   - Mention the role + a quick genuine hook, and that you'd love to connect and maybe find a
     time to chat. Do NOT paste the scheduling link here (LinkedIn connect notes strip/penalize
     links and space is tight) — just express interest in connecting/talking.
   - Sign with the first name.

If the user provides a STYLE DIRECTION below, follow it closely while keeping the messages
honest, casual, and concise.

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
#: but a prompt instruction is not a guarantee — so `ensure_intro_deck()` appends this line
#: when the model leaves it out. Every outreach email carries the deck; LinkedIn notes never do.
INTRO_DECK_SENTENCE = "Here's a good intro deck we could go over during the call: {url}"


def _intro_deck_url(profile: dict) -> str:
    """The intro-deck LINK offered in every outreach email.

    Distinct from `INTRO_DECK_PATH`, which ATTACHES a PDF. Priority mirrors
    `_scheduling_link`: INTRO_DECK_URL env → profile['personal']['intro_deck_url'] → ''.
    """
    return (os.environ.get("INTRO_DECK_URL", "").strip()
            or ((profile or {}).get("personal", {}).get("intro_deck_url") or "").strip())


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

    # Sit ABOVE the sign-off — a link under "Thanks, / Alejandro" reads as a footer and gets
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

    Length is the signal, not a keyword list — the drafts sign off as "Thanks", "Best",
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
    without the real background in the prompt and the model invents a plausible yes — a
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
    jd = (job.get("full_description") or "")[:1200]

    directive = _resolve_style(profile, style)
    style_block = f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else ""

    if warm:
        relationship = ("EXISTING 1st-degree LinkedIn connection who currently works at the company "
                        "(you already know each other).")
        warm_block = (
            "WARM / HOT OUTREACH — you are ALREADY CONNECTED with this person on LinkedIn and they "
            "work at this company. Write it as reconnecting with someone you know, NOT a cold intro:\n"
            f"- The FIRST LINE must acknowledge the gap since you last spoke AND name where they "
            f"work — the shape of \"Hey <name>, long time without connecting — hope everything is "
            f"well at {company}\". Vary the wording, keep that content.\n"
            "- Never introduce yourself as a stranger and never explain who you are as though they "
            "don't know you — no 'I'm a technical PM with 10+ years'. They already know you.\n"
            "- Mention you just applied for the role at their company and would love their read on it, "
            "an internal referral, or just to reconnect.\n"
            "- The LinkedIn note is a DIRECT MESSAGE to an existing connection (NOT a connection "
            "request) — it can be a bit longer/warmer and does not need the 300-char connect-note "
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
        "SCHEDULING LINK: none provided — invite a quick call/chat without a link.\n\n"
    )
    deck = _intro_deck_url(profile)
    deck_block = (
        f"INTRO DECK LINK (include in the EMAIL, not the LinkedIn note): {deck}\n"
        f'Offer it as "{INTRO_DECK_SENTENCE.format(url=deck)}" — or the same idea in your own '
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
        f"Description (excerpt):\n{jd}\n\n"
        + sched_block + deck_block + warm_block + style_block +
        "Write the outreach email. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=400, temperature=0.8,  # a bit higher for warmth/variety
    )
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
    return {"subject": subject, "body": body, "linkedin_note": note}


_FOLLOWUP_SYSTEM = """You write short follow-up emails for a job seeker who already emailed
someone at a company they applied to and got no reply.

Hard rules — a bad follow-up costs more than no follow-up:
- SHORT. Two or three sentences. Shorter than the first email, always.
- Never guilt, never "just bumping this", never "per my last email", never imply they owe a reply.
- Do NOT restate the original email. They can scroll down; it is in the same thread.
- ALWAYS give them an easy out — an explicit line saying it's fine to say no or that you'll
  stop. This is what separates persistent from spammy.
- No buzzwords, no "I hope this finds you well", no corporate voice. Contractions, real person.
- Never invent facts about the sender. Never attach years to a specific tool or framework.

Return ONLY JSON: {"subject": "...", "body": "..."}
The subject MUST be the original subject prefixed with "Re: " so it threads naturally."""

# What each touch is FOR. A follow-up that doesn't change with position reads as an autoresponder.
_TOUCH_INTENT = {
    1: ("Second touch (~2 days later). Brief, friendly nudge. Add ONE new, concrete thing — a "
        "detail about their work, or something the sender shipped that's relevant. Give them an out."),
    2: ("Third touch (~4 days later). Shorter still. Offer to make it easy: ask whether it's "
        "worth pursuing at all, or whether someone else there is the better person to talk to."),
    3: ("Final touch (~7 days later). This is the LAST message — say so plainly and warmly. "
        "Close the loop with no pressure, leave the door open, and do not ask a question that "
        "demands a reply."),
}


def draft_followup(profile: dict, job: dict, contact: dict, touch: int = 1,
                   style: str = "") -> dict:
    """Draft follow-up #`touch` for a contact who was emailed and hasn't replied.

    `touch` is 1-based: 1 = the first follow-up (second message overall). Returns
    {"subject", "body"}. Raises on LLM/parse failure, like draft_email.
    """
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "the company"
    original_subject = (contact.get("outreach_subject") or f"Question about the {role} role").strip()
    intent = _TOUCH_INTENT.get(max(1, min(touch, 3)), _TOUCH_INTENT[3])
    directive = _resolve_style(profile, style)
    link = _scheduling_link(profile)
    deck = _intro_deck_url(profile)

    sent_on = (contact.get("submitted_at") or "")[:10]
    user = (
        f"SENDER: {_sender_name(profile)}\n"
        f"TARGET: {contact.get('full_name', '')} — {contact.get('title', '')} at {company}\n"
        f"ROLE APPLIED FOR: {role}\n"
        f"ORIGINAL SUBJECT (reuse it with a 'Re: ' prefix): {original_subject}\n"
        f"ORIGINAL EMAIL SENT: {sent_on or 'recently'} — no reply since.\n"
        f"PREVIOUS MESSAGE BODY (do NOT repeat it):\n{(contact.get('outreach_message') or '')[:700]}\n\n"
        f"THIS FOLLOW-UP: {intent}\n\n"
        + (f"SCHEDULING LINK (optional, only if it fits naturally): {link}\n\n" if link else "")
        + (f"INTRO DECK LINK (include it): {deck}\n"
           f'Offer it as "{INTRO_DECK_SENTENCE.format(url=deck)}", or the same idea in fewer '
           "words — the full URL must appear verbatim. This is the concrete thing this "
           "follow-up offers, so lead with it rather than tacking it on.\n\n" if deck else "")
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
    body = ensure_intro_deck(body, deck)
    return {"subject": subject, "body": body}


_LI_FOLLOWUP_SYSTEM = """You write short LinkedIn follow-up messages for a job seeker.

The situation: they sent a connection request with a note, the person ACCEPTED, and then
never replied. Accepting is a small yes — treat it as mild interest, not as being ignored.

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

Return ONLY JSON: {"message": "..."}"""

_LI_TOUCH_INTENT = {
    1: ("They accepted but never replied. Thank them briefly for connecting, then ask one "
        "specific question about the role or the team — something a recruiter can answer fast."),
    2: ("Second nudge. Shorter. Offer an easy redirect: ask whether they're the right person "
        "for this, or who is."),
    3: ("Final message. Say plainly it's the last one, keep the door open, no question that "
        "demands an answer."),
}


def draft_for_channel(channel: str, profile: dict, job: dict, contact: dict,
                      touch: int = 1, style: str = "") -> dict:
    """One entry point per channel, returning ONE shape: {"subject", "body"}.

    The two drafters below return different keys for historical reasons (email has a
    subject line, a LinkedIn DM does not). Normalising here is what lets the dashboard's
    follow-up handler stop branching on channel — adding SMS means adding a row to this
    map, not another `if` in the request handler.
    """
    if channel == "linkedin":
        return {"subject": "", "body": draft_linkedin_followup(
            profile, job, contact, touch=touch, style=style)["message"]}
    return draft_followup(profile, job, contact, touch=touch, style=style)


_REPLY_SYSTEM = """You write a reply for a job seeker ANSWERING someone who just wrote to them
about a role they applied for.

This is not outreach and not a follow-up. They replied — the hard part already worked. The only
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
- If they introduced a colleague, acknowledge it and address the new person naturally.
- If they said no, be gracious and brief and do not argue or ask them to reconsider.

Return ONLY JSON: {"subject": "...", "body": "..."}
The subject MUST keep the thread's existing subject with a "Re: " prefix."""


def conversation_transcript(contact: dict, thread: list | None = None,
                            touches: list | None = None, their_reply: str = "") -> str:
    """The whole exchange, in order, as the model should read it.

    Assembled from three separate stores, which is exactly why it is worth having in one
    function: the first email lives on `contacts`, every follow-up lives in `touches`, and the
    reply lives in `messages`. A draft written from any one of them repeats what the other two
    already said — the specific way an automated-sounding reply gets written.
    """
    lines = []
    first = (contact.get("outreach_message") or "").strip()
    if first:
        subj = (contact.get("outreach_subject") or "").strip()
        when = (contact.get("submitted_at") or "")[:10]
        lines.append(f"[1] YOU wrote{f' on {when}' if when else ''}"
                     f"{f' — subject: {subj}' if subj else ''}:\n{first[:900]}")
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
    (CRM-4b), or text the operator pasted in. It refuses without it, deliberately — a
    "contextual" reply written with no context is a generic follow-up wearing a `Re:` subject
    line, and it would look like a working feature until somebody read it.

    `style` is the same free-text vibe knob as cold outreach ("more casual", "shorter", "add a
    joke"), resolved through `_resolve_style` so OUTREACH_STYLE and the profile default apply
    here too — one tone control for the whole product, not a second one that drifts.
    """
    from applypilot.domain import conversations as cv, intent as _intent

    last = _last_inbound(thread)
    if not last:
        raise ValueError("nothing to reply to — no inbound message on this thread")
    said = (their_reply or last.get("snippet") or "").strip()
    if not said:
        raise ValueError("no reply text — paste what they wrote, or enable reply content")

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
        # with X?" by inventing a confident yes — to the one person who can check it.
        "ABOUT YOU (use ONLY these facts; if they do not cover the question, say so plainly "
        "rather than inventing an answer):\n" + "\n".join(sender_background(profile)) + "\n\n"
        f"REPLYING TO: {who} — {contact.get('title', '')} at {company}\n"
        f"ROLE YOU APPLIED FOR: {role}\n"
        f"SUBJECT (reuse with 'Re: '): {subject or last.get('subject') or role}\n"
        + (f"ALSO ON THE THREAD (they are reading too): {', '.join(others)}\n" if others else "")
        + (f"WHAT THEIR REPLY LOOKS LIKE: {label['label']} — {label['action']}\n"
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


def draft_linkedin_followup(profile: dict, job: dict, contact: dict, touch: int = 1,
                            style: str = "") -> dict:
    """Draft LinkedIn follow-up #`touch` for a contact who connected but went quiet.

    Returns {"message": str}. This is a DIRECT MESSAGE to an existing 1st-degree
    connection, so the 300-char connection-note cap does NOT apply — but brevity still
    matters far more than in email, because it lands in a chat window.
    """
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "the company"
    intent = _LI_TOUCH_INTENT.get(max(1, min(touch, 3)), _LI_TOUCH_INTENT[3])
    directive = _resolve_style(profile, style)
    deck = _intro_deck_url(profile)
    sent_on = (contact.get("dm_sent_at") or "")[:10]

    user = (
        f"SENDER: {_sender_name(profile)}\n"
        f"TARGET: {contact.get('full_name', '')} — {contact.get('title', '')} at {company}\n"
        f"ROLE APPLIED FOR: {role}\n"
        f"CONNECTION NOTE THEY ALREADY READ (do NOT repeat it):\n"
        f"{(contact.get('linkedin_message') or '')[:400]}\n"
        f"They accepted the invite{f' around {sent_on}' if sent_on else ''} and have not replied.\n\n"
        f"THIS MESSAGE: {intent}\n\n"
        + (f"INTRO DECK LINK (include it): {deck}\n"
           "This is a DM to an existing connection, so a link is fine here — LinkedIn only "
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
