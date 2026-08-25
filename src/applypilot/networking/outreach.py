"""LLM outreach drafting, a short, specific email per contact.

Reuses the multi-provider LLM client and the tailor JSON-extraction + sanitize
helpers. Produces {subject, body}; the user reviews/edits before any send (NET-4).
"""

from __future__ import annotations

import logging
import os
import re

from applypilot.domain import transcript as _t_mod
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
- SEVERAL PEOPLE AT THE SAME COMPANY GET THESE, AND THEY SIT NEAR EACH OTHER. Assume two
  recipients will put your messages side by side. Anything identical across them, a subject
  line, an opening, a call-to-action sentence, a sign-off, proves a machine wrote both, and
  that costs the conversation far more than a slightly clumsier sentence would. Where the
  ALREADY USED block below lists wording that has gone to this company, none of it may appear
  again in any form close enough to recognise. Reach for a different sentence shape, not a
  synonym swap.
- These sign-offs are burned, never use them: "Looking forward to connecting", "Looking
  forward to hearing from you", "Thanks in advance", "Best regards".

Produce TWO things:

1. An EMAIL (subject + body):
   - SHORT. Under 120 words, a hard cap. Three or four short sentences, ONE paragraph. Count
     the words before you return; if it runs over, delete a sentence about the sender. A long
     first email from someone they have never met does not get read, it gets archived, and
     length reads as need.
   - WHEN THE RULES BELOW COMPETE FOR SPACE, this is the order to cut in: the sender's
     background goes first, then the detail about the role. The question and the out are the
     last things to go, because they are what earns a reply. Never solve a length problem by
     dropping them.
   - Open on the ROLE and on THEM, never on your own excitement or your CV. Name the specific
     role and the company in the first sentence (the block below says exactly how). The
     APPLICATION IS NOT THE NEWS: "I applied for X and wanted to reach out" tells the reader
     only that a form was submitted, and it is the opening every other candidate sends. Say
     something about the role or their team that would make no sense sent to a different
     company. These openers are burned, and so is any near-synonym of them, any tense of them,
     and any of them with a clause bolted on the front: "I'm really excited about", "I wanted
     to reach out", "I just applied and wanted to reach out", "I applied for the role and
     wanted to reach out".
   - Then ONE real, relevant thing about the sender, from their profile. ONE, and at most one
     sentence. Never a paragraph of career history: their background is the least interesting
     thing in this email to the person reading it.
   - Exactly ONE question, and it must be answerable in a single sentence. Two questions, or
     one that needs a paragraph back, is how a busy person defers replying forever.
   - CALL TO ACTION: invite them to a quick call to connect. If a SCHEDULING LINK is provided
     below, weave it in so they can book directly; the full URL must appear verbatim, but the
     sentence around it is YOURS TO WRITE and must be different every time. If no link is
     provided, just suggest a short call/chat. Keep it low-pressure, not pushy.
   - GIVE THEM AN EXPLICIT OUT. One clause saying it is genuinely fine to ignore this, to say
     no, or to point the sender at someone else. This is what separates persistent from
     pushy, it costs nothing, and to a stranger it is the most credible line in the message.
   - No urgency, no scarcity, no deadline the sender does not actually have, and no flattery
     that could be pasted into an email to anybody else.
   - Sign off casually with the sender's first name only. No signature block. The ONLY link
     allowed is the scheduling link (when provided).
   - SUBJECT: the most visible thing you write, and the one part several recipients at one
     company can compare in a forwarded message without opening anything.
     * NEVER "quick question", "quick q", "quick note", or any variation of them. It is the
       most overused cold-email subject there is and it tells the reader nothing.
     * NEVER a fixed frame with the role name dropped into a slot. The test is this: if
       swapping the role out would leave a subject that any other candidate applying to any
       other company could have sent, it is the wrong subject. Stated as a property and not
       as an example on purpose, because a specimen in a prompt comes back verbatim even when
       it is the thing being forbidden, and the last subject example in this prompt produced
       ten identical subject lines at one employer.
     * Short, lowercase-ish, and drawn from what THIS email actually says, the thing you
       asked or the thing you noticed, not from the fact that a job posting exists.

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


def ensure_sender_signoff(body: str, first_name: str) -> str:
    """Make the sign-off say the sender's actual name. Belt and braces, like the deck link.

    Found by GENERATING against real data rather than by reading the prompt (§Lessons 42): the
    third of three live pitch drafts was signed **"Alexander"**. The sender is Alejandro, and the
    prompt states his name and his first name on the first two lines. The model simply drifted on
    a proper noun.

    That is not a copy-quality problem, it is a factual error about the sender, sent to a
    stranger, in the one line they are most likely to read twice. And it is invisible in review:
    a wrong name looks exactly as fluent as a right one, which is the §Lessons 29 property —
    the dangerous half of a feature is the half that looks identical when it is wrong.

    Deliberately narrow. It rewrites the LAST line and only when that line is short enough to be
    a sign-off and does not already contain the first name, so "Thanks for reading, Sarah" from a
    body that ends mid-sentence is left alone. A prompt instruction is not a guarantee
    (§Lessons 9, 12) and this is the same shape as `ensure_intro_deck`.
    """
    name = (first_name or "").strip()
    if not name or not (body or "").strip():
        return body
    lines = body.rstrip().split("\n")
    last = lines[-1].strip()
    # A sign-off is short. Anything longer is a sentence, and rewriting a sentence would do more
    # damage than the wrong name does.
    if not last or len(last.split()) > 3:
        return body
    if name.lower() in last.lower():
        return body
    # Keep whatever punctuation shape the model chose ("Best,\nAlejandro" vs "Alejandro").
    lines[-1] = name
    return "\n".join(lines)


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
                  deck: bool = False, scheduling: bool = False, style: bool = False,
                  premise: bool = False, ctx: bool = False, ask: bool = False,
                  joblink: bool = False) -> str:
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
    # CTX-1. Constant within a Space once set, so it carries little information ACROSS a
    # campaign — but it is the only way to separate what was drafted before the premise existed
    # from what came after, which is the one comparison that says whether writing it was worth
    # anything. Untagged, that question is unanswerable for the same reason every copy change
    # was unfalsifiable before `draft_variant`.
    if premise:
        bits.append("premise")
    # CTX-2. Unlike `premise`, these VARY across the rows of one Space, so they are the first
    # inputs in this signature that can actually be compared within a campaign — some jobs get
    # context and some do not, and the reply rates are then two populations rather than a
    # before/after split.
    if ctx:
        bits.append("ctx")
    if ask:
        bits.append("ask")
    if deck:
        bits.append("deck")
    # Near-constant on a jobs Space today (32 of 32 rows yield a link), so it separates
    # before/after rather than comparing rows — same standing as `premise`. It earns its place
    # by being the only way to tell a draft written with the posting from one written without,
    # once both exist in the table.
    if joblink:
        bits.append("joblink")
    if scheduling:
        bits.append("cal")
    if style:
        bits.append("style")
    return "+".join(bits)


#: The pitch shape (SPACE-4). A SEPARATE system prompt, not the job-seeker one with caveats
#: appended: `_SYSTEM` opens "You write short, casual networking messages for a job seeker
#: reaching out to someone at a company they just applied to", and no amount of appended text
#: makes that describe a business proposal. §Lessons 40 — two instructions in one prompt
#: disagreeing is a code bug, and the heading wins every time.
#:
#: What makes this harder than the job version, and the reason it is written this defensively:
#: a job application is INVITED. The company posted the role. A pitch is not invited, arrives
#: from a stranger, and the recipient owes nothing. Every shortcut that reads as merely eager in
#: a job email reads as a sales sequence here, and there is no second impression.
_PITCH_SYSTEM = """You write short, direct first-contact emails from one working professional to
another, proposing a piece of work. Not a job application. Not a sales sequence. Not a pitch
deck in prose.

The person receiving this did not ask to hear from you. That is the whole difficulty, and
pretending otherwise is what makes outreach read as spam.

Hard rules:
- SHORT. Under 120 words. A long first email from a stranger does not get read, it gets
  archived, and length reads as need.
- Lead with THEM, not with you. The first sentence must be about their company, their work or
  their situation. An email that opens with who you are is a resume nobody requested.
- Say the concrete thing you would do. Not a capability list, not adjectives. One specific
  piece of work.
- Exactly ONE question, and it must be answerable in a sentence. "Are you the right person for
  this?" is a good question. "Would you be open to exploring a partnership?" is not, because
  it asks them to do the thinking.
- Give an explicit out. One clause saying it is fine to ignore this or to say no. This is what
  separates direct from pushy, and it costs nothing.
- Never claim a relationship, a referral or a shared connection that was not given to you.
  Never say you have been following their work unless you were told something specific.
- Never imply you applied to anything. There is no job here.
- No urgency, no scarcity, no "quick question" as a subject line to get an open, no flattery
  that could be pasted into any other email.
- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the
  clearest signal that text was pasted out of a chatbot, and a reader who spots one re-reads
  the whole message as machine-written. Use a comma, a full stop, or rewrite the sentence. A
  plain hyphen in a compound word ("large-scale") is fine.
- SEVERAL PEOPLE AT THE SAME COMPANY GET THESE, and they sit near each other. Anything
  recognisably similar across two of them proves a machine wrote both. Reach for a different
  sentence shape, not a synonym swap.

Return ONLY JSON: {"subject": "...", "body": "...", "linkedin_note": "..."}
- `subject`: lowercase-ish, specific, no "quick question", no company name alone.
- `body`: plain text, real line breaks, signed off with the sender's first name.
- `linkedin_note`: under 300 characters, a connection request note. NO LINKS, LinkedIn
  penalises them in invite notes."""


#: The premise shape (SHEET-1). A THIRD system prompt, and the reason it is third rather than a
#: flag on one of the other two is the same reason `_PITCH_SYSTEM` is not `_SYSTEM` with caveats:
#: the opening line decides what the email is, and appended text never wins against it
#: (§Lessons 40).
#:
#: What it is FOR: a Space whose rows are companies imported from a spreadsheet, with no posting
#: to apply to. `_SYSTEM` assumes "someone at a company they just applied to" — there is no
#: application. `_PITCH_SYSTEM` assumes "proposing a piece of work" — there is no proposal. The
#: campaign's own paragraph is the message, and the person's name, title and employer are the
#: only other facts available.
#:
#: Most of the hard rules below are `_PITCH_SYSTEM`'s verbatim, deliberately: they are about
#: writing to a stranger who did not ask to hear from you, which is equally true here. What
#: differs is the four lines saying what the email IS.
_PREMISE_SYSTEM = """You write short, plain first emails to someone at a company the sender wants
to work with. Not a job application — there is no posting and none is implied. Not a sales pitch
— nothing is being sold. Not a cover letter.

The sender will give you ONE paragraph about themselves and what they are looking for. That
paragraph is the substance of this email. Your job is to say it to THIS person, in your own
words, in a way that earns a reply.

The person receiving this did not ask to hear from you, and you know almost nothing about them
beyond their name, their title and where they work. Pretending otherwise is what makes outreach
read as spam.

Hard rules:
- SHORT. Under 120 words. Length reads as need.
- Say what the sender is looking for PLAINLY, in the first two sentences. Not a riddle, not a
  build-up, not "I'll keep this brief".
- Use the paragraph as FACTS, never as sentences. Every person in this campaign is written from
  that same paragraph, so reusing its wording means they all receive the identical claim.
- Aim it at THEM. Their title is usually the only thing you know about them, and it is enough to
  decide what part of the paragraph is worth saying to this particular person. What matters to a
  recruiter is not what matters to an engineer.
- Never invent anything about their company. No product, no funding round, no recent
  announcement, no problem they have. If you were told nothing about them, say less.
- Exactly ONE question, answerable in a sentence. "Is your team hiring?" and "Are you the right
  person to ask?" are good. "Would you be open to exploring opportunities?" is not, because it
  asks them to do the thinking.
- Give an explicit out. One clause saying it is fine to ignore this or to say no.
- Never claim you applied to anything, never reference a specific role unless the sender named
  one, and never claim a referral or a shared connection you were not given.
- No urgency, no scarcity, no flattery that could be pasted into any other email, no "quick
  question" as a subject line.
- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the clearest
  signal that text was pasted out of a chatbot, and a reader who spots one re-reads the whole
  message as machine-written. Use a comma, a full stop, or rewrite the sentence. A plain hyphen
  in a compound word ("large-scale") is fine.
- SEVERAL PEOPLE AT THE SAME COMPANY GET THESE, and they sit near each other. Anything
  recognisably similar across two of them proves a machine wrote both. Reach for a different
  sentence shape, not a synonym swap.

Return ONLY JSON: {"subject": "...", "body": "...", "linkedin_note": "..."}
- `subject`: lowercase-ish, specific, no "quick question", no company name alone.
- `body`: plain text, real line breaks, signed off with the sender's first name.
- `linkedin_note`: under 300 characters, a connection request note. NO LINKS, LinkedIn
  penalises them in invite notes."""


def _premise_led_block(space) -> str:
    """The premise as the SUBJECT of the email.

    Deliberately NOT `_premise_block`, which says the opposite in as many words — *"It is
    background, not the subject. A message that is only the premise is about the sender"*, and
    *"say it in your own words each time, or leave it out"*. Both are right where they are: in a
    jobs Space the posting is the subject and the premise explains why this role, so permission
    to omit it is correct.

    Here there is no posting. Handing the model a block that says "leave it out" and then
    expecting the email to be about it is §Lessons 40 — two instructions disagreeing is a code
    bug, and the heading wins. So this REPLACES it rather than being appended to it, and the old
    block is untouched for the two shapes that still want it as background.

    What survives from the original, because it is not about emphasis: facts-never-phrasing. It
    matters MORE here, not less. `offer` is per SPACE, so a parroted sentence lands in every
    inbox in the campaign — the largest repetition exposure in the app, larger than `job_context`
    (per row) and `noticed` (per person).
    """
    premise = (getattr(space, "offer", "") or "").strip()
    if not premise:
        return ""
    return (
        "WHAT THE SENDER IS LOOKING FOR (verbatim, from them — this is the SUBSTANCE of the "
        f"email):\n{premise}\n"
        "How to use it:\n"
        "- This is what the email is ABOUT. Say it plainly in the first two sentences.\n"
        "- FACTS, never sentences to reuse. Every person in this campaign is written from this "
        "same paragraph, so reusing its wording means every recipient gets the identical claim "
        "and any two of them can see a machine wrote both.\n"
        "- Choose the part of it that is worth saying to THIS person, given their title. You do "
        "not have to use all of it, and a shorter email that lands is better than a complete "
        "one that does not.\n\n")


def _premise_user_prompt(sender_bits, contact, company, about_them, noticed,
                         sched_block, deck_block, style_block, tone_block, previous, space=None,
                         known_block=""):
    """The premise-led prompt (SHEET-1).

    Structurally close to `_pitch_user_prompt` because the available FACTS are the same — a
    person, their title, their employer, and one constant paragraph. What differs is which of
    those is the subject, and that difference is carried by `_premise_led_block` and
    `_PREMISE_SYSTEM` rather than by rearranging the headings.

    `about_them` is whatever the operator typed into the card's Job tab and is usually empty.
    The prompt SAYS SO rather than leaving a blank heading: a model handed "WHAT THEY DO:"
    followed by nothing invents something, and an invented fact about the recipient's own
    company is the one error there is no recovering from.
    """
    from applypilot.domain.burned import burned_block
    them = (about_them or "").strip()
    return (
        "SENDER:\n" + "\n".join(sender_bits) + "\n\n"
        "WHO YOU ARE WRITING TO:\n"
        f"Name: {contact.get('full_name', '')}\n"
        f"Title: {contact.get('title', '')}\n"
        f"Company: {company}\n\n"
        + _premise_led_block(space)
        + (f"WHAT THIS COMPANY DOES (what the sender knows, react to THIS):\n{them}\n\n"
           if them else
           "WHAT THIS COMPANY DOES: not recorded. You know the company name and this person's "
           "title and NOTHING ELSE about them. Do not invent a product, a market, a funding "
           "round, a recent announcement or a problem they have. Write the opening from their "
           "TITLE instead, and keep it shorter because you have less to say.\n\n")
        # CTX-2's per-row field, which this prompt was not passing at all — it reached the jobs
        # prompt and nowhere else, so a Space whose rows are companies had an operator-typed
        # context box feeding nothing (§Lessons 49, 72). Distinct from the block above: that one
        # is what the company IS, this is what the OPERATOR knows and the public record does not.
        + (known_block or "")
        + (f"WHAT THE SENDER NOTICED ABOUT THIS PERSON (verbatim, from looking at their "
           f"profile):\n{noticed}\n"
           "ENGAGE WITH THE SUBSTANCE, NEVER ANNOUNCE THE NOTICING. Any sentence whose job is "
           "to report that you looked is the most recognisable automated-outreach shape there "
           "is. If the sentence could be deleted and the observation still stand, delete it.\n\n"
           if noticed else "")
        + sched_block + deck_block + style_block
        + _must_mention_block(space)
        + tone_block
        + burned_block(previous)
        + "Write the email. Return the JSON."
    )


def _pitch_user_prompt(sender_bits, contact, company, about_them, offer, noticed,
                       sched_block, deck_block, style_block, tone_block, previous, space=None):
    """The targets-shaped prompt (`spaces-prd.md` §7.1).

    The inversion is the whole point: in a job search the DESCRIPTION varies per row and the
    pitch is constant (your resume). Here the pitch is constant and their situation varies. So
    the offer arrives from the Space and what-they-do arrives from the row, which is the exact
    opposite of where those two things live in `_job_user_prompt`.

    `about_them` is whatever the operator pasted into the Job tab. It is often empty, and the
    prompt SAYS SO rather than leaving a blank heading: a model handed "WHAT THEY DO:" followed
    by nothing will invent something, and an invented fact about the recipient's own company is
    the one error that cannot be recovered from.
    """
    from applypilot.domain.burned import burned_block
    them = (about_them or "").strip()
    return (
        "SENDER:\n" + "\n".join(sender_bits) + "\n\n"
        "TARGET CONTACT:\n"
        f"Name: {contact.get('full_name', '')}\n"
        f"Title: {contact.get('title', '')}\n"
        f"Company: {company}\n\n"
        "WHAT YOU ARE PROPOSING (constant across everyone in this campaign, do NOT restate it "
        "verbatim, put it in your own words and aim it at THIS company):\n"
        f"{offer}\n\n"
        + (f"WHAT THIS COMPANY DOES (what the sender knows, react to THIS):\n{them}\n\n"
           if them else
           "WHAT THIS COMPANY DOES: not recorded. You know the company name and this person's "
           "title and NOTHING ELSE about them. Do not invent a product, a market, a funding "
           "round, a recent announcement or a problem they have. Write the opening from their "
           "TITLE and the offer instead, and keep it shorter because you have less to say.\n\n")
        + (f"WHAT THE SENDER NOTICED ABOUT THIS PERSON (verbatim, from looking at their "
           f"profile):\n{noticed}\n"
           "ENGAGE WITH THE SUBSTANCE, NEVER ANNOUNCE THE NOTICING. Any sentence whose job is "
           "to report that you looked is the most recognisable automated-outreach shape there "
           "is. If the sentence could be deleted and the observation still stand, delete it.\n\n"
           if noticed else "")
        + sched_block + deck_block + style_block
        + _must_mention_block(space)
        + tone_block
        + burned_block(previous)
        + "Write the email. Return the JSON."
    )


#: CTX-3. Everything the OPERATOR supplied — the campaign's voice, its premise, and what they
#: know about this one row — built in ONE place for every channel.
#:
#: Built as functions rather than inlined per prompt because the alternative is the failure this
#: codebase keeps paying for: `space.tone` reached `draft_email` and NOTHING else, so the
#: campaign voice applied to a cold email and stopped at the follow-up, the text and the reply.
#: Two of the six entry points could not even accept a manifest. Five separate constructions
#: would drift the same way (§Lessons 49 — a rule implemented at one of its call sites is not
#: implemented), and `followup.Channel` already showed what turning this into data buys.
#:
#: `brief` is for the short channels. It shortens the GUIDANCE and never drops a field: a
#: channel that silently loses a layer is the bug this exists to close, and a text has a
#: character limit that enforces brevity anyway.


def _voice_block(space) -> str:
    """The campaign's standing voice. Goes LAST in every prompt, immediately before the
    instruction to write — a constraint placed above the scheduling and deck blocks competes
    with them and loses (§Lessons 40)."""
    tone = (getattr(space, "tone", "") or "").strip()
    return f"VOICE FOR THIS CAMPAIGN (applies to every message in it):\n{tone}\n\n" if tone else ""


def must_mention_of(space) -> tuple:
    """Terms every message in this Space has to name. One reader, so nobody re-derives it."""
    return tuple(t for t in (getattr(space, "must_mention", ()) or ()) if str(t).strip())


def _must_mention_block(space, brief: bool = False) -> str:
    """A REQUIREMENT, worded as one. Deliberately unlike `_premise_block`.

    The premise hands over facts and says the model may put them in its own words or leave them
    out — which is right for a premise, and is why the live `gauntlet` Space, whose premise names
    GauntletAI twice, produced **zero of eight drafts mentioning it**. The model kept the decade
    at T-Mobile and Verizon and dropped the rest. Nothing was broken; it was told it could.

    So this says the opposite, and says it last, next to the instruction to write. It still does
    not dictate a SENTENCE: naming the required phrasing is how the SMS prompt's suggested wording
    came back in five of five drafts (§Lessons 42), and several people at one company reading the
    identical clause proves a machine wrote both.
    """
    terms = must_mention_of(space)
    if not terms:
        return ""
    listed = ", ".join(f'"{t}"' for t in terms)
    if brief:
        return (f"MUST MENTION: {listed}. Required even here — work it in naturally, in your own "
                "words, in a few.\n\n")
    return (
        f"REQUIRED IN THIS MESSAGE: {listed}\n"
        "This is a requirement of the campaign, not a fact to consider. Every message in this "
        "Space names it.\n"
        "- Work it in where it belongs in the argument, not bolted onto the end. A sentence "
        "whose only job is to satisfy this reads exactly like what it is.\n"
        "- YOUR OWN WORDS each time, and a different shape each time. Several people at one "
        "company receive these and sit near each other; the same clause in two of them proves a "
        "machine wrote both.\n"
        "- The term itself must appear. Do not paraphrase the NAME.\n\n"
    )


def missing_mentions(text: str, space) -> list:
    """Which required terms this draft failed to name. Empty means it complied.

    Case-insensitive and punctuation-tolerant on the term, because "GauntletAI", "Gauntlet AI"
    and "GauntletAI's" are the same mention and refusing two of them would send the model into a
    retry it cannot win.
    """
    flat = "".join(ch for ch in (text or "").lower() if ch.isalnum())
    out = []
    for term in must_mention_of(space):
        needle = "".join(ch for ch in str(term).lower() if ch.isalnum())
        if needle and needle not in flat:
            out.append(str(term))
    return out


def _premise_block(space, brief: bool = False) -> str:
    """What is true of every row in this Space. The most GENERAL fact available, so it reads
    furthest from the instruction to write."""
    premise = (getattr(space, "offer", "") or "").strip()
    if not premise:
        return ""
    if brief:
        return (f"THE PREMISE OF THIS CAMPAIGN (from the sender):\n{premise}\n"
                "Facts only, never this wording, and at most a clause of it — there is no room "
                "for more here.\n\n")
    return (f"THE PREMISE OF THIS CAMPAIGN (verbatim, from the sender — what is true of every "
            f"role they are pursuing here):\n{premise}\n"
            "How to use it:\n"
            "- These are FACTS, never sentences to reuse. Every message in this campaign is "
            "written from this same paragraph, so reusing its wording means every recipient "
            "receives the identical claim. Say it in your own words each time, or leave it "
            "out.\n"
            "- It answers WHY THIS ROLE, which the résumé cannot and the posting does not "
            "know.\n"
            "- It is background, not the subject. A message that is only the premise is about "
            "the sender.\n"
            "- If this particular role does not fit it, leave it out. A stretched connection "
            "reads worse than no connection.\n\n")


def _met_block(contact: dict, brief: bool = False) -> str:
    """What was said on a call with THIS PERSON. GRAN-1 phase 2, and CTX's tier 4.

    The strongest context there is: `noticed` is one line typed after glancing at a profile,
    `job_context` is a paragraph about the company, and this is what they actually said. It sits
    with the FACTS, early in the prompt — the voice goes last on every path.

    **Summaries only.** A body runs 20–50 KB and §Lessons 40 is that the loudest block in a
    prompt wins; dropped in whole it would swamp the posting, the premise and the voice at once.

    Loaded here when the caller did not already carry them, because the dashboard enriches the
    contact payload and the CLI does not. A read on a drafting path is affordable — that path
    ends in an LLM call — and the alternative is a fifth parameter through six prompt builders
    that four of them would forget (§Lessons 73).
    """
    rows = contact.get("transcripts")
    if rows is None:
        try:
            from applypilot.networking import transcripts as _tr
            rows = _tr.for_contact(contact.get("id") or "")
        except Exception:  # noqa: BLE001 — never block a draft on the meetings table
            rows = []
    return _t_mod.prompt_block(rows, limit=1 if brief else 2)


def _known_block(job: dict, brief: bool = False) -> str:
    """What the operator knows about THIS employer and role, against the scrape."""
    known = (job.get("job_context") or "").strip()[:1200]
    if not known:
        return ""
    if brief:
        return (f"WHAT THE SENDER KNOWS ABOUT THIS COMPANY AND ROLE (from the sender, true and "
                f"not in the posting):\n{known}\n"
                "Facts only, never this wording. Take the one part that fits this person and "
                "drop the rest.\n\n")
    return (f"WHAT THE SENDER KNOWS ABOUT THIS COMPANY AND ROLE (verbatim, from the sender — "
            f"true, and not in the posting):\n{known}\n"
            "How to use it:\n"
            "- FACTS, never phrasing. Several people at this company are being written to from "
            "this same paragraph, so reusing its wording sends them the identical sentence. Put "
            "it in your own words, differently each time.\n"
            "- It outranks the posting where they disagree. The operator has spoken to these "
            "people; the posting was written by whoever owned the requisition.\n"
            "- Use the part that is relevant to THIS person and drop the rest. A recruiter and "
            "an engineer do not need the same half of it.\n"
            "- Never present it as research. Naming how you came to know something, \"I saw "
            "that…\", \"I read that…\", \"I understand you…\", is the shape that reads as "
            "automated. Say the thing itself.\n"
            "- If none of it fits naturally, leave it out. A forced detail is worse than "
            "none.\n\n")


def posting_ref_for(job: dict, shape: str = "pipeline/jobs") -> dict:
    """How to NAME this job: {"title": …, "req": …}, either possibly "".

    Replaced the posting LINK, which was the first version of this and read badly. A Workday URL
    runs to 141 characters and inline in an opening sentence it turns a personal note into
    something a bot forwarded. What a human writes is the role's name, plus a requisition number
    when the reader is the sort who would look one up.

    ONE derivation per draft, and both consumers read this — the prompt block and nothing else,
    now that there is no string to force-append. See `domain/jobref.py` for why the TITLE is the
    hard half: eleven of thirty-three live rows carry one that must never be quoted.
    """
    from applypilot.domain.jobref import posting_reference
    return posting_reference(job, shape)


def _wants_requisition(contact: dict | None, ref: dict | None) -> bool:
    """Whether this recipient should be given the requisition number.

    ONE predicate, read by the prompt block and by the guarantee that enforces it. Two
    implementations of "is this person a recruiter" would let the prompt ask for a number the
    guarantee then refuses to insert, or worse the reverse — §Lessons 49, and the reason
    `rank.is_recruiter` is reused rather than re-written here.

    A requisition is how a recruiter finds the application in their own ATS. To a peer engineer
    it is a string of noise that reads as machine-generated, which is the exact quality this
    change exists to remove.
    """
    from applypilot.networking.rank import is_recruiter
    return bool((ref or {}).get("req")) and is_recruiter((contact or {}).get("title"))


def _posting_ref_block(ref: dict, contact: dict | None = None, brief: bool = False) -> str:
    """The prompt block naming the role, and the requisition when it is worth naming.

    The gap it closes: an email could say "the role I applied for" and name nothing. That is
    worst exactly where the scrape failed, and §Lessons 42 caught the failure mode reaching a
    live draft as *"the Betterup uploaded job"*.

    **An empty title says so, out loud.** A model handed "ROLE:" followed by nothing invents one,
    and an invented job title in the first sentence of an application email is unrecoverable. Six
    live rows have no sayable role, so this is the common case, not the edge.

    **The requisition is offered only to someone who would use it.** A req number is how a
    recruiter finds the application in their own ATS; to a peer engineer it is a string of noise
    that reads as machine-generated, which is the exact quality this change exists to fix.
    `rank.is_recruiter` already decides this elsewhere and is reused rather than re-implemented
    (§Lessons 49).
    """
    title, req = (ref or {}).get("title", ""), (ref or {}).get("req", "")
    wants_req = _wants_requisition(contact, ref)

    # ONE path for the no-title case. It was written twice — an early return for "nothing at all"
    # and an else-branch for "no title but a requisition" — and mutation showed the second was
    # untested, because every fixture that reached it took the first. Two copies of the same
    # instruction is one of them going stale (§Lessons 49, in a prompt instead of a call site).
    if brief:
        bits = (f"THE ROLE (name it in these words): {title}\n" if title else
                "THE ROLE: the stored title is unusable. If the posting names the role, use that "
                "name; otherwise stay general and invent nothing.\n")
        if wants_req:
            bits += f"Requisition {req} — include it, this person can look it up.\n"
        return bits + "\n"

    out = ""
    if title:
        out += (f"THE ROLE THIS IS ABOUT:\n{title}\n"
                "Name it in the first sentence, IN THESE WORDS. A large employer has many "
                "openings and the reader cannot be expected to guess which one. Do not "
                "paraphrase it, shorten it to something snappier, or add a level or a team it "
                "does not say.\n")
    else:
        out += ("THE ROLE THIS IS ABOUT: the stored title is unusable — it is a scraper "
                "placeholder or a careers-page heading rather than a role.\n"
                "- If the posting text above NAMES the role, use that name. Reading it from the "
                "description is not inventing it, and it is much better than staying vague.\n"
                "- If it does not, refer to the role in general terms and NEVER invent a title, "
                "a level or a team the posting does not state. An invented job title in the "
                "first sentence of an application email cannot be recovered from.\n")
    if wants_req:
        out += (f"REQUISITION: {req}\n"
                "This person recruits, so the number is genuinely useful to them — it is how "
                "they find the application. Put it in parentheses after the role name or in a "
                "short closing line. Never make it the subject of a sentence.\n")
    elif req:
        out += ("REQUISITION: one exists, but this person does not recruit. Do NOT mention it. "
                "A requisition number means nothing to a peer and reads as machine-generated.\n")
    return out + "\n"


def ensure_requisition(body: str, title: str, req: str) -> str:
    """Put the requisition in parentheses after the role's first mention, if it is missing.

    Measured rather than assumed: with the prompt asking for it, four drafts to a recruiter
    carried the number **twice**. A coin flip is not "include it", and §Lessons 9 and 12 are the
    standing answer — a prompt instruction is not a guarantee.

    What makes this different from the guarantees it is modelled on is that it will NOT append.
    `ensure_intro_deck` can add a sentence because a deck link is an offer that stands alone; a
    requisition number is a qualifier on a role, and a line reading "REQ-12289" under the
    sign-off is precisely the machine-assembled footer this whole change removed. So it inserts
    at the one place the number belongs and otherwise does nothing:

        "I just applied for the Emerging Technology Architect role at Q2"
                                                                 ^ here

    No role mention in the body means no insertion. A requisition floating free of the thing it
    identifies is worse than its absence.
    """
    if not req or not title or not (body or "").strip():
        return body
    if req.lower() in body.lower():
        return body
    # The title as the MODEL wrote it, which may differ in case. Matching case-insensitively and
    # splicing by index preserves whatever it actually typed.
    idx = body.lower().find(title.lower())
    if idx < 0:
        return body
    end = idx + len(title)
    return f"{body[:end]} ({req}){body[end:]}"


def _job_user_prompt(sender_bits, contact, relationship, role, company, jd, noticed,
                    context_block, premise_block, sched_block, deck_block, warm_block,
                    style_block, tone_block, previous, posting_ref_block="", must_mention_block=""):
    """The jobs-shaped prompt.

    Extracted from `draft_email` so it can be diffed: `test_a_default_space_changes_the_prompt_by_nothing`
    asserts that a Space with no overrides produces the byte-identical string this
    produced before Spaces existed. Every manifest field is additive, and the only way to
    know that is to compare the artifact rather than to reason about it (§Lessons 46).

    `premise` is `Space.offer` (CTX-1). The field is called `offer` because the targets shape
    named it — there it means "what I am selling" — and it is the PREMISE here: what is true of
    every role in this campaign. The name is worse than the thing; renaming it costs a manifest
    field and a config-blob path, so the better name is used locally and the mismatch is written
    down rather than carried silently.

    It was declared, documented for exactly this case, and read by nothing on this path for
    three days. `UNAPPLIED` was empty the whole time and its guard was honest — it asks "is this
    field read anywhere?", and the answer was yes, on one of two shapes (§Lessons 49).
    """
    from applypilot.domain.burned import burned_block
    return (
        "SENDER:\n" + "\n".join(sender_bits) + "\n\n"
        "TARGET CONTACT:\n"
        f"Name: {contact.get('full_name', '')}\n"
        f"Title: {contact.get('title', '')}\n"
        f"Relationship: {relationship}\n\n"
        f"JOB APPLIED TO:\nRole: {role}\nCompany: {company}\n"
        f"WHAT THE ROLE ACTUALLY INVOLVES (from the posting, the specific thing to react to):\n"
        f"{jd}\n\n"
        # Directly under the role it identifies, not down with the CTA blocks. Which job this
        # is, is a FACT to state rather than a thing to ask for, and the blocks below all
        # compete for the closing paragraph (§Lessons 40).
        + posting_ref_block
        # CTX-2. What the operator KNOWS, against the scrape directly above it, which is
        # everything else this prompt has ever had about a job. Placed above `noticed` because
        # it is the more general of the two operator inputs — person-specific reads closest to
        # the instruction to write — and above the CTA blocks because it is a fact to draw on
        # rather than a thing to ask for.
        + context_block
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
        # CTX-1. Why this role, which is the one thing a résumé cannot say and the posting does
        # not know. Placed below `noticed` because it is the more GENERAL of the two operator
        # inputs — person-specific reads closest to the instruction to write — and above the
        # CTA blocks because it is a fact to draw on, not a thing to ask for.
        #
        # The repetition warning is not boilerplate. `noticed` is per PERSON, so its worst case
        # is one repeated sentence to one reader. This is per SPACE: `job-search` holds 30 jobs
        # and 131 emailed contacts, so a premise that comes back verbatim is one paragraph
        # arriving in ~200 inboxes. §Lessons 42 fired on a single quoted phrasing appearing in
        # 5 of 5 drafts; the exposure here is two orders of magnitude larger.
        + premise_block
        + sched_block + deck_block + warm_block + style_block
        # LAST, immediately before the instruction to write. A constraint placed above the
        # scheduling and deck blocks competes with them and loses — §Lessons 40: two
        # instructions in one prompt disagreeing is a code bug, not a wording problem.
        + must_mention_block
        + tone_block
        + burned_block(previous)
        + "Write the outreach email. Return the JSON."
    )


#: The email body cap the three first-contact voices state, as a number the CODE can check.
#: Stated in the prompt AND enforced here for the reason §Lessons 9 and 12 keep recording: a
#: prompt instruction is not a guarantee. Measured against the live model with the cap written
#: into the prompt three separate ways (a hard cap, "count the words before you return", and an
#: explicit order to cut in): 2 of 5 drafts still came back at 131 and 160 words. Models do not
#: count reliably, and no amount of restating it changes that.
#:
#: `test_the_prompt_and_the_code_agree_on_the_cap` pins this against the prompt text, because a
#: bound written in two places is two bounds — that is how the intro-deck PDF rode along on 34
#: real emails while `doctor --config` reported it off.
_BODY_WORD_CAP = 120


def _too_long(raw: str) -> int:
    """Word count of the drafted body if it exceeds the cap, else 0.

    Counted on the MODEL's body, before `ensure_intro_deck` appends its sentence: the cap is a
    rule about what the model writes, and a guarantee the code adds afterwards is not the
    model's overrun to fix. Unparseable output returns 0 — the caller raises on that later with
    a better message than a length retry would give.
    """
    try:
        body = str(extract_json(raw).get("body", ""))
    except Exception:
        return 0
    n = len(body.split())
    return n if n > _BODY_WORD_CAP else 0


def _chat_meeting_requirements(client, system: str, user: str, space,
                               *, max_tokens: int, temperature: float, tries: int = 2) -> str:
    """Generate, and regenerate ONCE if a required term is missing.

    `Space.must_mention` is a requirement, and a prompt instruction is not a guarantee
    (§Lessons 9, 12). Measured before this existed: the `gauntlet` premise names GauntletAI twice
    and none of eight drafts mentioned it.

    A retry rather than an append, and that is the load-bearing choice. `ensure_intro_deck` can
    append because a deck link is a URL — there is one correct string and repeating it costs
    nothing. A required MENTION has to be a sentence, and a canned sentence lands identically in
    every inbox at one company, which is the failure §Lessons 42 recorded when the SMS prompt's
    own suggested wording came back in five of five drafts. Asking again gets a different
    sentence; appending gets the same one forever.

    The last attempt is returned even if it still falls short. Refusing to draft would leave the
    operator with nothing to edit, and `draft_variant` records what happened either way — a draft
    that misses the term is visible and fixable; no draft is neither.
    """
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    raw = ""
    for attempt in range(max(1, tries)):
        raw = client.chat(messages, max_tokens=max_tokens, temperature=temperature)
        missing = missing_mentions(raw, space)
        overrun = _too_long(raw)
        if not missing and not overrun:
            return raw
        if attempt + 1 >= tries:
            if missing:
                log.warning("draft still missing required mention(s): %s", ", ".join(missing))
            if overrun:
                # Returned long rather than cut. Truncating an email to a word count leaves it
                # ending mid-sentence, and a message that stops mid-thought reads as broken to
                # the recipient in a way that a slightly long one does not. The operator edits
                # it; `ensure_intro_deck` can append a URL safely because a URL is one correct
                # string, and prose is not (§Lessons 87).
                log.warning("draft still over the %d-word cap: %d words", _BODY_WORD_CAP, overrun)
            break
        # Named in the RETRY rather than louder in the original prompt: two instructions
        # disagreeing is not fixed by volume (§Lessons 40), and this one is a correction to a
        # specific attempt rather than a standing rule.
        faults = []
        if missing:
            faults.append(
                "It never mentions " + ", ".join(f'"{m}"' for m in missing) +
                ", which is required in every message in this campaign. Rewrite so the term "
                "appears, worked into the argument rather than bolted on, and change the "
                "surrounding sentence rather than inserting a stock one.")
        if overrun:
            # The count is given back because the model cannot measure it, and the cut order
            # repeats the prompt's own so the retry cannot contradict the standing rule.
            faults.append(
                f"The body is {overrun} words, over the {_BODY_WORD_CAP}-word cap. Cut it to "
                f"under {_BODY_WORD_CAP}. Remove the sentences about the sender's background "
                "first; keep the question and keep the line giving them an out. Do not solve "
                "it by deleting the scheduling link.")
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "That draft has a problem. " + " ".join(faults) +
                                        " Return the JSON."},
        ]
    return raw


def draft_email(profile: dict, job: dict, contact: dict, style: str = "", warm: bool = False,
                previous: list[dict] | None = None, space=None) -> dict:
    """Return {"subject": str, "body": str} for one contact. Raises on LLM/parse failure.

    `style` is an optional free-text directive (e.g. "keep it super casual", "mention I'm a
    Longhorn", "make it a little witty") that steers the tone. Falls back to OUTREACH_STYLE env
    or profile["outreach_style"] via _resolve_style.

    `warm=True` = the HOT layer: this person is an EXISTING 1st-degree LinkedIn connection at the
    company. The copy should acknowledge the existing relationship (reconnect, not cold intro),
    and the LinkedIn note becomes a direct MESSAGE to a connection (not a connect request).

    `previous` is what this employer has ALREADY been sent — `[{subject, body}]`. Without it the
    model converges: measured across 189 live drafts, ten people at Google received the identical
    subject line and one CTA sentence appeared 48 times. The caller passes it because the store
    is where that lives; the prompt block is built in `domain/burned.py`, which explains why
    deleting the worked examples was necessary and not sufficient.
    """
    # SPACE-4. `space` is the Space's manifest; None means the behaviour that existed before
    # Spaces did, which is what every caller that has not been taught about them means.
    shape = getattr(space, "shape", "pipeline/jobs")
    wants_deck = getattr(space, "offer_deck", True)
    # A voice directive from the Space, distinct from `style`: `style` is a one-off the operator
    # types for a single run, this is the campaign's standing voice. Both are honoured, and the
    # Space's goes LAST so a per-run instruction cannot be silently overridden by a stored one.
    # Shared with every other channel since CTX-3 — it used to be built here and nowhere else.
    tone_block = _voice_block(space)

    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "your company"
    sender_bits = sender_background(profile)
    # The parts of the posting that say what the JOB IS, not the first 1200 characters, which
    # on a real description is the mission statement, the org chart, and then the role starting
    # exactly where the budget ran out. See domain/jobdesc.py for the measurement.
    from applypilot.domain.jobdesc import role_essentials
    jd = role_essentials(job.get("full_description"))
    noticed = (contact.get("noticed") or "").strip()[:400]
    # CTX-2. Per ROW, against `noticed`'s per PERSON. Capped here as well as at the write,
    # because a job dict can reach this function from a caller that never went through
    # `repo.set_context` — a CLI path, a test, a future importer.
    context = (job.get("job_context") or "").strip()[:1200]

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
    # CTX-2. The operator's own ask for this row REPLACES the default CTA framing; it is not
    # appended to it. A prompt that says "invite them to book a call" AND "ask who owns the
    # intake rebuild" produces an email that does both, badly — §Lessons 40, where the SMS
    # touch ladder beat the standing block every time because appending never resolves a
    # contradiction. The LINK survives either way: what the operator overrides is what to ask
    # for, not whether a calendar exists.
    ask = (job.get("job_ask") or "").strip()[:200]
    if ask:
        sched_block = (
            f"WHAT THE SENDER WANTS FROM THIS PERSON (verbatim, from the sender):\n{ask}\n"
            "This REPLACES the usual call-booking ask. Do not also invite them to book a call, "
            "and do not ask for two things — one email, one request.\n"
            + (f"If a call is the natural way to give it, this link books one directly: {link}\n\n"
               if link else "\n")
        )
    else:
        sched_block = (
            f"SCHEDULING LINK (include in the EMAIL CTA so they can book a call directly): {link}\n\n"
            if link else
            "SCHEDULING LINK: none provided, invite a quick call/chat without a link.\n\n"
        )
    # `offer_deck=False` turns the deck off for a whole Space. Resolved here rather than in
    # `_intro_deck_url` so the OTHER guarantee still holds: `ensure_intro_deck` appends the link
    # when the model drops it, and it must not append a link the Space said not to send.
    deck = _intro_deck_url(profile, contact) if wants_deck else ""
    deck_block = (
        f"INTRO DECK LINK (include in the EMAIL, not the LinkedIn note): {deck}\n"
        "Offer it in a sentence of YOUR OWN. The full URL must appear verbatim; the wording "
        "around it must not. This line was previously quoted here as a model sentence and came "
        "back in six of eight drafts at one company, which is the exact failure the ALREADY "
        "USED block exists to stop. Put it near the call CTA, before the sign-off.\n\n"
        if deck else ""
    )

    # The Space's constant paragraph. BOTH shapes read it — the targets prompt as the offer,
    # the jobs prompt as the premise. It reached only the first for three days (CTX-1).
    offer = (getattr(space, "offer", "") or "").strip()

    # How to NAME the role, derived ONCE. No URL: a 141-character Workday link inline in an
    # opening sentence reads as machine-forwarded, which is what this replaced.
    posting_ref = posting_ref_for(job, shape)

    # SHEET-1. What a ROW is (`shape`) and what the EMAIL is (`voice`) were one decision, so a
    # company-shaped Space was forced into the pitch. `voice_or_default()` returns exactly what
    # this branch used to hardcode, which is why no existing Space moves and the golden file
    # does not budge.
    voice = space.voice_or_default() if hasattr(space, "voice_or_default") else (
        "pitch" if shape == "pipeline/targets" else "jobseeker")

    if voice == "premise":
        user = _premise_user_prompt(sender_bits, contact, company,
                                    job.get("full_description"), noticed,
                                    sched_block, deck_block, style_block,
                                    tone_block, previous, space=space,
                                    known_block=_known_block(job) + _met_block(contact))
        system = _PREMISE_SYSTEM
    elif voice == "pitch":
        # `full_description` is what the operator pasted about the company, and the offer comes
        # from the Space. In the jobs prompt those two slots are filled the other way round.
        user = _pitch_user_prompt(sender_bits, contact, company,
                                  job.get("full_description"), offer,
                                  noticed, sched_block, deck_block, style_block,
                                  tone_block, previous, space=space)
        system = _PITCH_SYSTEM
    else:
        user = _job_user_prompt(sender_bits, contact, relationship, role, company, jd,
                                noticed, _known_block(job) + _met_block(contact),
                                _premise_block(space),
                                sched_block, deck_block, warm_block,
                                style_block, tone_block, previous,
                                posting_ref_block=_posting_ref_block(posting_ref, contact),
                                must_mention_block=_must_mention_block(space))
        system = _SYSTEM

    client = get_client("light")
    raw = _chat_meeting_requirements(client, system, user, space,
                                     max_tokens=400, temperature=0.8)
    variant = draft_variant(warm=warm, noticed=bool(noticed), jd_chars=len(jd),
                            deck=bool(deck), scheduling=bool(link), style=bool(directive),
                            premise=bool(offer), ctx=bool(context), ask=bool(ask),
                            joblink=bool(posting_ref.get("title") or posting_ref.get("req")))
    data = extract_json(raw)
    subject = sanitize_text(str(data.get("subject", ""))).strip()
    body = sanitize_text(str(data.get("body", ""))).strip()
    note = sanitize_text(str(data.get("linkedin_note", ""))).strip()
    if not subject:
        subject = (f"{company}" if shape == "pipeline/targets"
                   else f"Question about the {role} role")
    if not body:
        raise ValueError("empty outreach body")
    # Not left to the prompt: the deck goes in EVERY outreach email.
    body = ensure_intro_deck(body, deck)
    # Nor the requisition, for the recipients it is meant for. The prompt asks and the model
    # obliged in two of four live drafts, which is a coin flip rather than a rule.
    if _wants_requisition(contact, posting_ref):
        body = ensure_requisition(body, posting_ref.get("title", ""), posting_ref.get("req", ""))
    # Nor is the sender's own name. See ensure_sender_signoff — a live draft came back signed
    # "Alexander" with "Sender first name: Alejandro" two lines into the prompt.
    # `_sender_name` — the SAME function the prompt used two hundred lines up. The first version
    # of this line re-derived the name from `full_name`, which is "Jorge Alejandro Diez", so the
    # guard against a wrong sign-off would have rewritten every correct "Alejandro" to "Jorge".
    # A rare hallucination turned into a systematic error, by a fix. §Lessons 49: one rule, one
    # implementation — and it was caught by regenerating against the live model, not by the unit
    # test, which hardcoded the name it expected.
    body = ensure_sender_signoff(body, _sender_name(profile))
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

#: Used INSTEAD OF the ladder above when they have opened the intro deck since our last message.
#:
#: Replacing rather than appending is the whole design, and §Lessons 40 is why: the ladder
#: arrives under the heading `THIS FOLLOW-UP:` and describes chasing someone who has done
#: nothing. Adding "and ask about the deck" underneath leaves two instructions disagreeing, and
#: the heading wins every time — that is exactly how SMS drafts kept asking whether the first
#: email had arrived, of people who had already answered it. A contradiction in a prompt is a
#: code bug, not a wording problem.
#:
#: The hard rule is the second paragraph. Knowing they clicked comes from a beacon on our own
#: site, and saying so tells a stranger their reading was watched — it converts a warm signal
#: into a creepy one, and there is no recovering the conversation after that. The codebase
#: already bans this exact shape for `noticed` ("NEVER ANNOUNCE THE NOTICING"), and the stakes
#: are higher here because a LinkedIn post is public and this is not. The saving grace is that
#: the honest version of the question is indistinguishable from an ordinary follow-up: "did
#: anything in the deck raise questions" is a normal thing to ask someone you sent a deck to.
_DECK_OPENED_INTENT = (
    "They have looked at the intro deck since your last message and still have not replied. "
    "Ask what they made of it.\n"
    "\n"
    "THE DECK IS THE SENDER'S OWN. They wrote it and sent it. NEVER write as though the sender "
    "read it, discovered something in it, or was struck by it: \"one thing that stuck with me "
    "from the deck\", \"what I found interesting in the deck\" and anything like them are "
    "nonsense, because it is their material. The sender is asking for the RECIPIENT'S reaction "
    "to something they sent.\n"
    "\n"
    "NEVER SAY, HINT OR IMPLY THAT YOU KNOW THEY OPENED IT. No \"I saw you had a look\", no "
    "\"since you checked out the deck\", no \"I noticed you opened\". The sender knows because "
    "of a tracking beacon, and telling a stranger their reading was watched turns the best "
    "signal in this whole sequence into the reason they never reply.\n"
    "This costs nothing, because the natural question is identical either way: asking what "
    "somebody made of a deck you sent them reads the same to a person who read it and a person "
    "who did not. Phrase it so it would make sense to BOTH.\n"
    "\n"
    "Write it short and direct:\n"
    "- Ask for their reaction to the deck. Plainly. \"Curious what you made of it\", \"did "
    "anything in it land\", \"any questions it raised\" are all fine, in your own words.\n"
    "- You MAY point at one part of it the sender wants their view on, but as the AUTHOR "
    "offering it, not as a reader reporting on it: \"the part on X\" rather than \"what "
    "struck me about X\".\n"
    "- ONE question, answerable in a line. Not an essay prompt about the future of their "
    "industry, which is homework and gets no reply.\n"
    "- Do NOT paste the deck link again. They have it.\n"
    "- Two or three sentences, and an easy out."
)


#: Follow-ups in a targets Space (SPACE-4b). `_FOLLOWUP_SYSTEM` opens "for a job seeker who
#: already emailed someone at a company they applied to" — so touch 1 would read correctly and
#: touch 2 would claim an application that does not exist. The first email got its own prompt
#: and every message after it was left on the job-seeker one, which is §Lessons 49: a rule
#: implemented at one of its call sites is not implemented.
#:
#: Shorter and more cautious than the job version on purpose. A follow-up to a recruiter is
#: expected; a second unsolicited email to a stranger is the message most likely to convert a
#: neutral non-reply into an annoyed one, and there is nothing to recover it with.
_PITCH_FOLLOWUP_SYSTEM = """You write short follow-up emails from one working professional to
another, after a first email proposing some work went unanswered.

Nobody asked for the first email and nobody owes a reply to this one. A follow-up that forgets
that is the message that gets the sender blocked.

Hard rules, a bad follow-up costs more than no follow-up:
- SHORTER than the first email. Two or three sentences, never more.
- Never guilt, never "just bumping this", never "per my last email", never "I wanted to
  circle back", never imply they owe a reply or that you are surprised by silence.
- Do NOT restate the proposal. It is in the same thread, they can scroll.
- ALWAYS give an explicit out, a line saying it is fine to say no or that you will stop. On an
  uninvited thread this is the whole difference between persistent and spam.
- Never claim new information you do not have. Do not invent a recent announcement, a funding
  round, a hire or a problem at their company as a reason for writing again.
- Never imply an application, an interview or a hiring process. There is no job here.
- No urgency, no scarcity, no "just following up one last time" as a pressure move, even on the
  last touch. If it is the last one, say so plainly and mean it.
- No buzzwords, no "I hope this finds you well", no corporate voice. Contractions, real person.
- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere. It is the
  clearest signal that text was pasted out of a chatbot. Use a comma, a full stop, or rewrite.

Return ONLY JSON: {"subject": "...", "body": "..."}
The subject MUST be the original subject prefixed with "Re: " so it threads naturally."""


#: The LinkedIn equivalent. `_LI_FOLLOWUP_SYSTEM` also opens "for a job seeker".
_PITCH_LI_FOLLOWUP_SYSTEM = """You write very short LinkedIn follow-up messages from one working
professional to another, after proposing some work and getting no reply.

This lands in a chat window where your previous message is visible directly above it. Brevity
matters more than in email, and repeating yourself is more obvious.

Hard rules:
- ONE or TWO sentences. Never a paragraph.
- Never guilt, never "just following up", never imply they owe a reply.
- Do not restate the proposal. It is right there above.
- Give an explicit out in the same breath, and mean it.
- Never imply an application, an interview or a hiring process. There is no job here.
- No links. Ask one thing that can be answered with a word.
- NEVER use an em dash (—), en dash (–), or any long dash. Not one, anywhere.

Return ONLY JSON: {"message": "..."}"""


def draft_followup(profile: dict, job: dict, contact: dict, touch: int = 1,
                   style: str = "", touches: list | None = None, space=None) -> dict:
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
    # Did they open the deck since we last wrote? If so that REPLACES the ladder's intent — see
    # `_DECK_OPENED_INTENT`, and §Lessons 40 for why appending it would change nothing.
    from applypilot.domain.interactions import deck_opened_since_we_wrote
    deck_opened = deck_opened_since_we_wrote(contact, touches)
    intent = (_DECK_OPENED_INTENT if deck_opened
              else _TOUCH_INTENT.get(max(1, min(touch, 3)), _TOUCH_INTENT[3]))
    directive = _resolve_style(profile, style)
    link = _scheduling_link(profile)
    deck = _intro_deck_url(profile, contact)

    sent_on = (contact.get("submitted_at") or "")[:10]
    # Jobs shape only, derived once for the block below.
    posting_ref = posting_ref_for(job, getattr(space, "shape", "pipeline/jobs"))

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
    # `deck_opened` implies it was sent, whatever the transcript says. Opening a link is proof of
    # receipt that no string match can override, and the two must not disagree — a prompt telling
    # the model to pitch a deck they have already read, while the intent above asks what they
    # made of it, is §Lessons 40 rebuilt inside the same function.
    deck_sent = deck_opened or (bool(deck_base) and deck_base.rstrip("/").lower() in transcript.lower())

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
                 "Offer it in a sentence of your own, in few words; the full URL must appear "
                 "verbatim and the wording around it must not repeat anything already sent. "
                 "This is the concrete thing this follow-up offers, so lead with it rather "
                 "than tacking it on.\n\n"
                 if deck else ""))
        # Unlike the deck, the posting link is carried by EVERY touch, and the difference is not
        # a judgement call — it was measured. `send_followup` sends `draft_body` BARE: no quoted
        # original is appended, so threading is the only thing tying the message to the first
        # email. That is enough for Gmail on a desktop and nothing at all on a phone, where a
        # follow-up otherwise reads as "just following up" about a role it never names.
        #
        # It also does not carry the deck's cost. Re-pitching an OFFER four times is what read as
        # automated; a reference saying which job this is about is what every human chasing an
        # application writes, and it is one clause.
        + _posting_ref_block(posting_ref, contact, brief=True)
        + _must_mention_block(space, brief=True)
        + _premise_block(space) + _known_block(job) + _met_block(contact)
        + (f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else "")
        + _voice_block(space)
        + "Write the follow-up. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system",
          "content": (_PITCH_FOLLOWUP_SYSTEM
                      if getattr(space, "shape", "") == "pipeline/targets"
                      else _FOLLOWUP_SYSTEM)},
         {"role": "user", "content": user}],
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
    if _wants_requisition(contact, posting_ref):
        body = ensure_requisition(body, posting_ref.get("title", ""), posting_ref.get("req", ""))
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
                      touches: list | None = None, space=None) -> dict:
    """One entry point per channel, returning ONE shape: {"subject", "body"}.

    The drafters below return different keys for historical reasons (email has a subject
    line, a LinkedIn DM does not, a text certainly does not). Normalising here is what lets
    the dashboard's follow-up handler stop branching on channel, adding SMS was adding a
    row to this map, not another `if` in the request handler.
    """
    if channel == "linkedin":
        return {"subject": "", "body": draft_linkedin_followup(
            profile, job, contact, touch=touch, style=style,
            messages=contact.get("interactions"), space=space)["message"]}
    if channel == "sms":
        # SMS deliberately NOT given a pitch variant here. Texting a stranger you are pitching
        # is a product decision, not a wording one, and the outreach template should probably
        # drop the channel rather than have this file invent copy for it. Flagged, not guessed.
        return {"subject": "", "body": draft_sms(
            profile, job, contact, touch=touch, style=style, thread=thread,
            space=space)["message"]}
    return draft_followup(profile, job, contact, touch=touch, style=style, touches=touches,
                          space=space)


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


#: How much conversation the prompt may carry. A real thread here runs to 87 messages; handing
#: all of it over would blow the context and bury the part being answered. The FIRST message is
#: always kept (it says what this is about) and the rest is filled from the most recent
#: backwards — the same shape the panel uses when it collapses a long thread.
_TRANSCRIPT_CHARS = 7000


def conversation_transcript(contact: dict, thread: list | None = None,
                            touches: list | None = None, their_reply: str = "") -> str:
    """The whole exchange, in order, as the model should read it.

    Assembled from three stores, which is exactly why it is worth having in one function: the
    first email lives on `contacts`, every follow-up lives in `touches`, and the conversation
    itself lives in `messages`. A draft written from any one of them repeats what the other two
    already said, the specific way an automated-sounding reply gets written.

    **It used to read the `thread` argument for two fields and nothing else** — the last inbound
    message's sender name and date — so the transcript was our first email, our follow-ups, and
    ONE reply. §Lessons 39 recorded that exact shape for this exact function ("a function that
    takes a thread may not read the thread") and it was still true of everything except the
    final message.

    Measured on the live database when this was fixed:

        Lee Ackerley       87 messages (45 inbound)  -> transcript showed 2 entries
        Kevin Parakkattu   61 messages (36 inbound)  -> 2
        Diego Bodart       18 messages (12 inbound)  -> 2

    So a reply "written from the whole sequence" was written from the opening email and the
    newest message, with everything in between invisible — including every answer WE had already
    given, which is how a drafter re-asks a question that was settled four messages ago.

    Our own messages come from the FULLER copy where one exists: `contacts.outreach_message` and
    `touches.body` hold the text as typed, while a synced `messages.snippet` is capped at 200
    characters. The thread copy of a message we already hold in full is dropped rather than
    shown twice.
    """
    events = _transcript_events(contact, thread, touches, their_reply)
    if not events:
        return ""
    kept, skipped = _fit(events, _TRANSCRIPT_CHARS)
    lines = []
    for i, e in enumerate(kept, start=1):
        if e is _GAP:
            lines.append(f"[…{skipped} earlier message{'s' if skipped != 1 else ''} omitted…]")
            continue
        when = f" on {e['at'][:10]}" if e.get("at") else ""
        subj = f", subject: {e['subject']}" if e.get("subject") else ""
        # The marker goes INSIDE the message, on the line where the text stops, because that is
        # the only place it can be read as being about this message. A note in the standing
        # instructions is a rule the model applies to the transcript in general; this is a fact
        # about the sentence it is looking at.
        clip = ("\n[TRUNCATED BY APPLYPILOT — this message continues. The cut above is OURS, not "
                "the sender's. Do NOT mention it, ask what they were about to say, or treat the "
                "last sentence as unfinished.]") if e.get("clipped") else ""
        lines.append(f"[{i}] {e['who']}{when}{subj}:\n{e['body']}{clip}")
    return "\n\n".join(lines)


#: Marker for the elided middle. An object rather than a string so it cannot collide with a real
#: message whose body happens to look like the marker.
_GAP = object()


#: Characters a complete message is allowed to end on. Anything else, at preview length, is a
#: sentence that stops mid-air — which is what our own truncation looks like.
_ENDS_CLEANLY = ('.', '!', '?', '"', "'", ')', ']', ':', ';', '”', '’', '…')

#: A SIGN-OFF is a complete ending even with no full stop. Live on the Sentilink card, "Best, Liz"
#: and a signature line ending "| NVIDIA" were both marked as continuing — and a marker that says
#: "this message continues" about a message that does not is a false statement in a prompt, which
#: is the thing §Lessons 40 and 42 are both about. Over-marking is still the safe DIRECTION; that
#: is an argument for erring toward it on genuinely ambiguous text, not for stating something
#: known to be untrue.
_SIGNOFF_RE = re.compile(
    r"(?i)\b(best|thanks|thank you|regards|best regards|kind regards|cheers|sincerely|warmly|"
    r"all the best|talk soon|speak soon)\b[,!.]*\s*[\w.'-]*\s*$")


def _is_clipped(body: str) -> bool:
    """Did WE cut this message short?

    **This is the single most damaging thing the drafter can be lied to about.** Live, on the Miro
    card: Yukiko's reply was stored as Gmail's preview, ending *"Could you kindly let me know"*,
    and the transcript presented that as her complete message. The model then wrote back *"your
    message got cut off at the end — what were you about to ask?"* — a sentence about OUR storage,
    addressed to a recruiter whose email was perfectly intact, and one click from being sent.
    Measured at the time: 85 of 136 inbound messages were previews.

    The first version of this compared length to the two declared bounds, and that is wrong:
    **Gmail's snippet ends on a WORD boundary, not at exactly 200 characters.** On the live
    database 236 rows sat between 150 and 195 characters, so `len == SNIPPET_MAX` missed most of
    them — including our own message in the very transcript used to check the fix, which ended
    "On a" at 194. A test for the cap cannot see a cut that stops short of it.

    So the question asked is whether the text ENDS like a finished message. At preview length,
    stopping anywhere other than on terminal punctuation means the sentence was cut. `PASTED_MAX`
    is still checked outright, because a deliberate fetch that fills its bound is by definition
    holding more.

    **Over-reporting is the safe direction and this rule leans into it.** A false positive only
    tells the model not to lean on the final sentence, which is never harmful; a false negative
    invites it to answer a cut as if the sender wrote it. That asymmetry is the whole design, and
    it is why no attempt is made to be clever about signatures or sign-offs.

    ONE implementation, in `domain/conversations`. It used to live only here, so the contact
    CARD grew its own version in JavaScript comparing `len >= SNIPPET_MAX` — the rule this
    docstring spends three paragraphs explaining is wrong — and a 194-character message
    rendered on screen with no marker while this path correctly reported it cut.
    """
    from applypilot.domain import conversations as _cv
    from applypilot.networking.messages import PASTED_MAX, SNIPPET_MAX
    return _cv.is_clipped(body, SNIPPET_MAX, PASTED_MAX)


def _transcript_events(contact: dict, thread, touches, their_reply: str) -> list[dict]:
    """Every message in the conversation, oldest first, deduplicated across the three stores."""
    thread = [m for m in (thread or []) if isinstance(m, dict)]
    events: list[dict] = []

    # Our own messages, from the copies that hold the FULL text.
    first = (contact.get("outreach_message") or "").strip()
    first_at = (contact.get("submitted_at") or "").strip()
    if first:
        events.append({"who": "YOU wrote", "at": first_at, "body": first[:900],
                       "subject": (contact.get("outreach_subject") or "").strip()})
    sent_ats = {first_at[:16]} if first_at else set()
    for t in (touches or []):
        body = (t.get("body") or "").strip()
        if not body:
            continue
        at = (t.get("sent_at") or "").strip()
        events.append({"who": "YOU followed up", "at": at, "body": body[:600]})
        if at:
            sent_ats.add(at[:16])

    # ...and everything in the thread that those do not already cover.
    #
    # `contacts.sent_message_id` identifies the first email exactly. Touches carry no message id
    # (the gap CLAUDE.md records under §Lessons 77), so an outbound thread message is matched to
    # one by MINUTE — deterministic, and the failure mode is showing a 200-char snippet beside
    # the full text rather than losing anything.
    first_id = (contact.get("sent_message_id") or "").strip()
    for m in thread:
        body = (m.get("snippet") or "").strip()
        if not body:
            continue
        at = (m.get("sent_at") or "").strip()
        inbound = (m.get("direction") or "") == "in"
        if not inbound:
            if first_id and m.get("message_id") == first_id:
                continue
            if at and at[:16] in sent_ats:
                continue
        events.append({
            "who": (f"{(m.get('from_name') or 'THEY').upper()} REPLIED" if inbound
                    else "YOU wrote"),
            "at": at, "body": body, "subject": (m.get("subject") or "").strip(),
            # WE cut this, and the model has to be told so.
            "clipped": _is_clipped(body),
        })

    events.sort(key=lambda e: e.get("at") or "")

    # A pasted reply is the operator's own transcription of the newest inbound message and is
    # usually fuller than the stored snippet, so it REPLACES that message rather than being
    # appended after it — appended, the model reads the same message twice and treats the
    # repetition as emphasis.
    said = (their_reply or "").strip()
    if said:
        for e in reversed(events):
            if "REPLIED" in e["who"]:
                e["body"] = said
                break
        else:
            last = _last_inbound(thread)
            events.append({"who": f"{(last.get('from_name') or 'THEY').upper()} REPLIED",
                           "at": (last.get("sent_at") or ""), "body": said})
    return events


def _fit(events: list, budget: int) -> tuple[list, int]:
    """Trim to a character budget: keep the FIRST message and as many of the most recent as fit.

    The opening message says what the conversation is about and is what a late reply is still
    implicitly answering, so it is never the thing dropped. Everything else is filled in from the
    newest backwards, because the near end is what the reply has to engage with.
    """
    total = sum(len(e["body"]) for e in events)
    if total <= budget or len(events) <= 2:
        return events, 0
    head, rest = events[:1], events[1:]
    used = len(head[0]["body"])
    tail: list = []
    for e in reversed(rest):
        if used + len(e["body"]) > budget and tail:
            break
        tail.insert(0, e)
        used += len(e["body"])
    skipped = len(rest) - len(tail)
    return (head + ([_GAP] if skipped else []) + tail), skipped


def _last_inbound(thread: list | None) -> dict:
    inbound = [m for m in (thread or [])
               if isinstance(m, dict) and (m.get("direction") or "") == "in"]
    return inbound[-1] if inbound else {}


def _our_last_in_thread(thread) -> dict | None:
    """The last message WE sent AFTER they last spoke, or None.

    Reset on every inbound message rather than simply taking the newest outbound: what matters is
    the message they have not answered, and an outbound one from before their reply was answered
    — by the reply itself.
    """
    ours = None
    for m in (thread or []):
        if not isinstance(m, dict):
            continue
        if (m.get("direction") or "") == "in":
            ours = None
        elif (m.get("snippet") or "").strip():
            ours = m
    return ours


def _turn_block(thread) -> str:
    """WHOSE TURN IT IS, stated as a fact before the task is named.

    The reply drafter knew what was said and not who owed whom a reply. On the live Sentilink
    card that produced the report *"this follow up is repeating myself"*, and it was exact: Liz
    acknowledged the application, we answered *"Looking forward to hearing from your team. Let me
    know if you need anything else from me in the meantime"* — and the next draft came back
    *"look forward to hearing back from your team soon. If there's anything else you need from my
    side, just let me know."* A paraphrase of our own message, three days later.

    Nothing was broken in the transcript: our reply WAS in it, under a line reading "Everything
    above marked YOU is already in their inbox. Do not repeat any of it." It lost to the task,
    which said *"Answer what they said"* — and answering a message we had already answered can
    only produce the answer again. §Lessons 40: two instructions disagreeing is a code bug, and
    the one naming the task wins.

    So the state is asserted here, and the TASK is replaced in `_turn_task` rather than caveated.
    `conversation_state` has always known this — `awaiting_them`, `stalled`, 91 hours on that card
    — and no drafter had ever been told (§Lessons 21, a value one layer computes that the other
    cannot see).
    """
    from applypilot.domain import conversations as cv
    state = cv.conversation_state(thread or [])
    if not state or state.get("state") != cv.AWAITING_THEM:
        return ""
    days = state.get("days")
    ours = _our_last_in_thread(thread)
    when = f" {days} day{'s' if days != 1 else ''} ago" if days else ""
    out = ("WHOSE TURN IT IS: **yours was already taken.** You answered them"
           f"{when} and they have not replied since. There is nothing left to answer — "
           "they owe YOU a response.\n\n")
    if ours:
        # Quoted immediately beside the task, because the general "do not repeat" line one
        # paragraph up is what the model already ignored. This is the sentence it must not
        # rewrite, shown as the thing not to rewrite.
        out += ("YOUR LAST MESSAGE, WHICH THEY ALREADY HAVE — do not restate it, do not "
                "paraphrase it, and do not make the same offer again:\n"
                f"\"{(ours.get('snippet') or '').strip()[:400]}\"\n\n")
    return out


def _turn_task(thread) -> str:
    """The closing instruction. REPLACED when they owe us, never caveated.

    Appending "…and do not repeat yourself" to "Answer what they said" is the fix that does not
    work — it was already there, one paragraph above, and lost.
    """
    from applypilot.domain import conversations as cv
    state = cv.conversation_state(thread or [])
    if not state or state.get("state") != cv.AWAITING_THEM:
        return "Write the reply. Answer what they said. Return the JSON."
    return (
        "Write a NUDGE, not a reply. They have gone quiet on you, so this message has one job: "
        "to move it forward.\n"
        "  - Open by acknowledging where things stand in your own words. If they said they would "
        "get back to you, say that you know that.\n"
        "  - Then say what you actually want NOW — to get moving on it, to know the timeline, to "
        "talk to whoever decides. Be direct about it; that is the entire point of writing.\n"
        "  - BANNED, because you have already said all of it and saying it again is what makes "
        "this read as automated: thanking them again; saying you look forward to hearing from "
        "them or from their team; offering to send anything else; and ASKING WHETHER THEY NEED "
        "ANYTHING FROM YOU in any wording. Ask for something instead.\n"
        "  - Short. Two or three sentences.\n"
        "Return the JSON."
    )


def draft_reply(profile: dict, job: dict, contact: dict, thread: list | None = None,
                subject: str = "", style: str = "", their_reply: str = "",
                touches: list | None = None, space=None) -> dict:
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
        + _turn_block(thread)
        + (f"SCHEDULING LINK (use it only if they want to talk): {link}\n\n" if link else "")
        + _premise_block(space, brief=True) + _known_block(job, brief=True)
        + _met_block(contact, brief=True)
        + (f"STYLE DIRECTION (follow closely, it overrides the default voice):\n{directive}\n\n"
           if directive else "")
        + _voice_block(space)
        + _turn_task(thread)
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
                            style: str = "", messages: list | None = None, space=None) -> dict:
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
        + _premise_block(space, brief=True) + _known_block(job, brief=True)
        + _met_block(contact, brief=True)
        + (f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else "")
        + _voice_block(space)
        + "Write the LinkedIn follow-up. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system",
          "content": (_PITCH_LI_FOLLOWUP_SYSTEM
                      if getattr(space, "shape", "") == "pipeline/targets"
                      else _LI_FOLLOWUP_SYSTEM)},
         {"role": "user", "content": user}],
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
              style: str = "", thread: list | None = None, space=None) -> dict:
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
        + _premise_block(space, brief=True) + _known_block(job, brief=True)
        + _met_block(contact, brief=True)
        + (f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else "")
        + _voice_block(space)
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
