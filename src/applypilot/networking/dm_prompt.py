"""Controller prompt for the LinkedIn-DM send loop.

Unlike the read-only people-search agent (prompt.py), this drives a *write* action —
it opens a message composer and sends a note. Safety is enforced by the loop, not the
prompt: the driver only executes the tiny action set below, always inserts the message
VERBATIM (the model never supplies the text), and refuses to send in dry-run.

The loop is: snapshot the page → ask the model for ONE next action → execute it →
repeat. The model sees the accessibility tree (with @refs) and picks a ref to click.
"""

from __future__ import annotations

import json

# The only actions the driver will execute. Anything else is treated as an abort.
#   snapshot     — (implicit each turn) re-read the page
#   click        — click an element by its @ref (to open the composer / focus the box)
#   type_message — focus is on the composer; insert the EXACT drafted note (driver-supplied)
#   send         — click the Send button (BLOCKED in dry-run: loop stops here as "ready")
#   abort        — bail out with a reason (login wall, wrong person, no composer, captcha)
#   done         — the message was sent and confirmed
ACTIONS = ("click", "type_message", "send", "abort", "done")


def build_system_prompt() -> str:
    return (
        "You are carefully driving a real LinkedIn browser session to deliver ONE short note "
        "to ONE person. You act one step at a time. Each turn you are given the page's "
        "accessibility snapshot (elements with @ref handles) and the history of what you've "
        "done. Respond with a SINGLE JSON object choosing the next action.\n\n"
        "GOAL: get the approved note into a note/message text field for the target person and "
        "send it. There are two possible paths — pick whichever this profile offers.\n\n"
        "BEFORE ANYTHING ELSE, each turn: if a blocking modal/overlay/popup is present (e.g. a "
        "Premium upsell like \"Job search with confidence\", a cookie banner, or any dialog with "
        "a close/X button that dims the page), DISMISS it first — click its X / \"No thanks\" / "
        "close (action=click) — then continue. Do not try to click page buttons underneath a modal.\n\n"
        "CHOOSING THE PATH: if the person is 2nd/3rd-degree or NOT connected, a \"Message\" button "
        "is usually paid InMail (Premium) — do NOT use it. Prefer PATH A (Connect + note). Only use "
        "PATH B (Message) when you're confident it's a free direct message (1st-degree connection).\n\n"
        "PATH A — Connection request with a note (use when NOT already connected; this is the "
        "common case):\n"
        "  1. Click the \"Connect\" button for THIS person (action=click). LinkedIn often HIDES "
        "Connect under the \"More\" button when only \"Message\" and \"Follow\" are shown — in that "
        "case click \"More\" first, then click \"Connect\" in the menu that appears.\n"
        "  2. A dialog appears offering \"Add a note\" — click it (action=click).\n"
        "  3. Click the note text field to focus it (action=click), then insert the note "
        "(action=type_message). The note is <=300 chars, which fits the invitation limit.\n"
        "  4. Click \"Send\" / \"Send invitation\" (action=send).\n\n"
        "PATH B — Direct message (use when a \"Message\" button is available, i.e. already "
        "connected or open-profile):\n"
        "  1. Click \"Message\" (action=click). 2. Click the message text box (action=click). "
        "3. Insert the note (action=type_message). 4. Click \"Send\" (action=send).\n\n"
        "You do NOT write or choose the note text — the system inserts the approved note "
        "verbatim when you choose \"type_message\". Never retype or paraphrase it.\n\n"
        "ACTIONS (respond with exactly one):\n"
        '  {"action":"click","ref":"<@ref>","why":"..."}\n'
        '  {"action":"type_message","why":"note field is focused"}\n'
        '  {"action":"send","ref":"<@ref of the Send / Send invitation button>","why":"..."}\n'
        '  {"action":"abort","reason":"..."}   — use if you see a sign-in/authwall, a '
        "checkpoint/captcha, a Premium/InMail-only paywall, NO Connect AND no Message option, "
        "or you are unsure you're on the RIGHT person.\n"
        '  {"action":"done","why":"sent — dialog closed / pending-invitation or sent state visible"}\n\n'
        "RULES:\n"
        "- One action per turn. Prefer clicking @refs from the snapshot over guessing.\n"
        "- ANTI-LOOP: check STEPS SO FAR. If your last 2 actions were the same click with no "
        "visible change, STOP repeating it — a modal is likely blocking it (dismiss it), or the "
        "target is elsewhere (open the \"More\" menu, or pick a different @ref).\n"
        "- Prefer PATH A (Connect + note); treat a Message button on a non-connection as InMail.\n"
        "- Only choose \"send\" once the note field clearly contains the note and you clicked the "
        "correct Send button for THIS dialog.\n"
        "- If the note requires payment (InMail/Premium) or you can neither Connect nor Message, "
        "choose \"abort\" — never pay, never send into doubt.\n"
        "- If anything looks wrong (wrong name, login wall, unexpected page), choose \"abort\".\n"
        "- Output ONLY the JSON object, no prose."
    )


def build_turn_prompt(target_name: str, target_url: str, message: str,
                      snapshot: str, history: list[str], dry_run: bool) -> str:
    hist = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(history)) or "  (none yet)"
    mode = ("DRY-RUN: composing only. When you would click Send, choose \"send\" anyway — "
            "the system will STOP before actually clicking it."
            if dry_run else
            "LIVE: choosing \"send\" will really send the message.")
    return (
        f"TARGET PERSON: {target_name}\n"
        f"TARGET PROFILE URL: {target_url}\n"
        f"MODE: {mode}\n\n"
        f"APPROVED NOTE (inserted verbatim on type_message; do not retype):\n"
        f"\"\"\"\n{message}\n\"\"\"\n\n"
        f"STEPS SO FAR:\n{hist}\n\n"
        f"CURRENT PAGE SNAPSHOT (accessibility tree with @refs, truncated):\n"
        f"{snapshot[:6000]}\n\n"
        f"Respond with ONE JSON action."
    )


def parse_action(raw: str) -> dict:
    """Extract the action JSON from the model output; fall back to abort on any garble."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {"action": "abort", "reason": "model returned no JSON action"}
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {"action": "abort", "reason": "unparseable action JSON"}
    if obj.get("action") not in ACTIONS:
        return {"action": "abort", "reason": f"unknown action: {obj.get('action')!r}"}
    return obj
