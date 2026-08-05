"""What a Space IS — a manifest, not a fork. Pure; no SQL, no HTTP, no networking imports.

Shaped deliberately after `domain/followup.Channel`, because that pattern already paid for
itself here: turning per-channel branching into data is what made a third follow-up channel
cost one column, one registry row and one prompt. A Space is the same bet one level up — the
pipeline is shared, and what differs per Space is declarative.

The bet is falsifiable and `test_adding_a_space_needs_no_schema_change` is where it gets
falsified: it defines a Space that exists nowhere in this codebase and drives it end to end.
The channel version of that test found the one line where the claim was false (`followup_panel`
spelled out `buckets[EMAIL.name], buckets[LINKEDIN.name]` by hand, so a third channel passed
through the whole engine correctly and vanished at the return statement). Expect the same here.

**What a Space does NOT carry, and why:**

`company_cap` is on the identity, not here — `spaces-prd.md` §7 puts it on the Space and that
is the same mistake §Headline 4 already corrected for the daily send limit. The cap exists
because a company sees one sender, not seven threads, and *the recipient does not know what a
Space is*. Two Spaces on one mailbox each capped at 8 would send 16 emails to one employer,
which is exactly the arithmetic §Headline 4 rejected.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace

#: A shape decides what a ROW is. There is no third shape — a flat people list was in v1 of the
#: PRD for a relationship Space, and the prospects-only decision removed the need for it.
JOBS_SHAPE = "pipeline/jobs"
TARGETS_SHAPE = "pipeline/targets"
SHAPES = (JOBS_SHAPE, TARGETS_SHAPE)

#: A template decides what the copy SOUNDS like. Two of the three share a shape, which is the
#: central claim of `spaces-prd.md`: business outreach and a business CRM are the same machine
#: pointed at the same kind of row, and the difference between them is who is sending.
TEMPLATES = ("jobs", "outreach", "business")

#: The templates the UI offers TODAY. `business` is deliberately not among them: it differs
#: from `outreach` by `identity_id` and wording alone, `identity_id` is frozen once a Space has
#: sent anything (§13.2), and ID-1 has not shipped — so a business Space created now would send
#: from the personal mailbox permanently. That is not the business Space anyone wanted, and a
#: button that produces it is worse than a missing button. Unlocked by ID-1, which is one entry
#: in this tuple.
OFFERED_TEMPLATES = ("jobs", "outreach")

#: What each template is FOR, in the operator's words. Lives here rather than in the HTML so the
#: picker cannot describe a template differently from what `TEMPLATE_DEFAULTS` actually builds.
TEMPLATE_BLURB = {
    "jobs": ("Job search",
             "Rows are job postings. Tailored résumé and cover letter per row, "
             "the apply agent, follow-ups at 2/4/7 days. Success is an interview."),
    "outreach": ("Partnerships",
                 "Rows are companies you want to work with. You write one offer, "
                 "used in every draft. Slower follow-ups at 5/12 days. "
                 "Success is a booked call."),
    "business": ("Business",
                 "The same machine as Partnerships, sending as the business. "
                 "Needs a separate mailbox (ID-1), so it is not offered yet."),
}

#: What success MEANS. The only two outcomes in this system that mean stop — everything else
#: counts effort. `interview` for a job search, `booked` for a pitch.
TERMINALS = ("interview", "booked")

#: Manifest fields declared but read by nothing yet. EMPTY as of SPACE-4 — every field is now
#: wired — and kept as a mechanism rather than deleted, because the next field will be declared
#: before it is used and this is what stops that being invisible.
#:
#: A field that is accepted but never read is a lie the caller cannot see (§Lessons 39, where
#: `conversation_transcript` took a `thread` it barely used and the resulting draft could
#: restate but never continue). `test_unapplied_fields_are_really_unapplied` parses attribute
#: access to hold the line — its first version grepped for the literal `space.<field>` and
#: survived a mutation reading `manifest.tone`, which is §Lessons 48 inside the guard itself.
UNAPPLIED: tuple[str, ...] = ()


@dataclass(frozen=True)
class Space:
    """One row of configuration over one shared engine.

    Every field is read by something as of SPACE-4: `shape` and `tailor_docs` gate the pipeline
    queues, `tone`/`offer` reach the drafting prompts, `schedules`/`channels` drive the ladders,
    `offer_deck` gates the intro-deck link and `can_autosend` gates the send path itself.
    """

    id: str
    name: str
    template: str
    shape: str
    identity_id: str = "personal"

    #: Résumé + cover-letter generation and attachment. False for every targets Space: the
    #: pitch there is the Space's `offer`, written once, not a document regenerated per row.
    #: Gates `queue_for_tailor` / `queue_for_cover`, so this is checked on every prepare run.
    tailor_docs: bool = True

    #: The campaign's standing voice, injected into every draft in the Space (SPACE-4).
    #: Distinct from the per-run `style` an operator types for one regeneration.
    tone: str = ""
    #: The constant pitch (§7.1). In a job search the DESCRIPTION varies per row and the pitch
    #: is constant; in an outreach Space that inverts, so this lives here and not on a row.
    offer: str = ""
    channels: tuple[str, ...] = ("email", "linkedin", "sms")
    schedules: dict = field(default_factory=dict)
    offer_deck: bool = True
    can_autosend: bool = True
    terminal: str = "interview"

    def __post_init__(self) -> None:
        """Refuse a manifest that cannot mean anything, at construction.

        A Space with an unknown shape is not a degraded Space — `jobs_shaped_ids()` would
        silently exclude it from every pipeline queue, and its rows would sit in a panel that
        never processes them. That is the §Lessons 44 shape: a state that renders like a healthy
        one. Better to fail where the bad value was written.
        """
        if not self.id:
            raise ValueError("a Space needs an id; it is hashed into every targets contact_id")
        if self.shape not in SHAPES:
            raise ValueError(f"unknown shape {self.shape!r}; expected one of {SHAPES}")
        if self.template not in TEMPLATES:
            raise ValueError(f"unknown template {self.template!r}; expected one of {TEMPLATES}")
        if self.terminal not in TERMINALS:
            raise ValueError(f"unknown terminal {self.terminal!r}; expected one of {TERMINALS}")

    # ── what the engine asks of a Space ──

    @property
    def runs_job_pipeline(self) -> bool:
        """Whether discover → enrich → score → apply apply to this Space's rows at all.

        Read off the shape rather than the template, because the template is about wording and
        two different templates share the targets shape.
        """
        return self.shape == JOBS_SHAPE

    @property
    def makes_documents(self) -> bool:
        """Whether the tailor and cover stages should ever see this Space's rows.

        Separate from `runs_job_pipeline` on purpose: a jobs-shaped Space may legitimately turn
        document generation off (applying to postings with a fixed résumé), and collapsing the
        two would make that unexpressible.
        """
        return self.runs_job_pipeline and self.tailor_docs

    # ── persistence, without the repo knowing the field list ──

    #: Columns of the `spaces` table. Everything else rides in the `config` JSON blob, so
    #: adding a manifest field is a code change and never a schema change — which is the whole
    #: claim, and what `test_adding_a_space_needs_no_schema_change` checks.
    COLUMNS = ("id", "name", "template", "shape", "identity_id")

    @classmethod
    def from_row(cls, row: dict) -> Space:
        """Build a manifest from a `spaces` row, merging its `config` JSON.

        A malformed or absent blob yields the DEFAULTS rather than an error. The columns are
        what identify a Space; the blob is preference. Refusing to load a Space because someone
        hand-edited its JSON would take the whole panel down for a recoverable problem, and the
        defaults are the documented behaviour anyway.
        """
        known = {f.name for f in fields(cls)}
        data = {k: row[k] for k in cls.COLUMNS if row.get(k) is not None}
        try:
            blob = json.loads(row.get("config") or "{}")
        except (TypeError, ValueError):
            blob = {}
        if isinstance(blob, dict):
            data.update({k: v for k, v in blob.items() if k in known and k not in cls.COLUMNS})
        if "channels" in data:
            data["channels"] = tuple(data["channels"] or ())
        return cls(**data)

    def config_json(self) -> str:
        """The half of this manifest that is NOT a column, ready to store.

        Only non-default values are written. A blob holding every field would freeze today's
        defaults into every Space ever created, so changing a default later would change nothing
        for anyone — the same trap as a `draft_variant` recorded as a version number instead of
        its inputs.
        """
        blank = Space(id=self.id, name=self.name, template=self.template,
                      shape=self.shape, identity_id=self.identity_id)
        mine, base = asdict(self), asdict(blank)
        out = {k: v for k, v in mine.items()
               if k not in self.COLUMNS and v != base[k]}
        if "channels" in out:
            out["channels"] = list(out["channels"])
        return json.dumps(out, sort_keys=True)

    def with_(self, **changes) -> Space:
        """A copy with fields changed — validated, because `__post_init__` runs again.

        `id` and `shape` are frozen after creation (`spaces-prd.md` §13.2): the id is hashed
        into every targets `contact_id`, and a shape flip has no meaning for rows already
        stored. Refused here rather than only in the UI, so a future endpoint cannot reintroduce
        it by forgetting.
        """
        frozen = [k for k in ("id", "shape") if k in changes and changes[k] != getattr(self, k)]
        if frozen:
            raise ValueError(
                f"{', '.join(frozen)} cannot change after a Space exists — the id is hashed "
                f"into every contact_id in a targets Space, and a shape flip has no meaning for "
                f"rows already stored. Create a new Space instead.")
        return replace(self, **changes)


#: Template prefills (`spaces-prd.md` §7). A template is a starting manifest, not a type — every
#: field stays editable afterwards per §13.2, which is why these are plain kwargs rather than
#: subclasses.
#:
#: `business` differs from `outreach` by `identity_id` and wording ALONE. That is the whole
#: claim of the PRD, and SPACE-6 exists to falsify it: if the business Space ever needs code,
#: say so rather than absorbing it.
TEMPLATE_DEFAULTS: dict[str, dict] = {
    "jobs": {
        "shape": JOBS_SHAPE,
        "terminal": "interview",
        "tailor_docs": True,
    },
    "outreach": {
        "shape": TARGETS_SHAPE,
        "terminal": "booked",
        "tailor_docs": False,
        # Slower than a job search on purpose: 5d then 12d. A C-suite pitch nudged at 48h reads
        # as pressure from a stranger, where the same cadence to a recruiter is normal.
        "schedules": {"email": [120, 288]},
        "tone": "A peer proposing work, not an applicant.",
    },
    "business": {
        "shape": TARGETS_SHAPE,
        "terminal": "booked",
        "tailor_docs": False,
        "schedules": {"email": [120, 288]},
        "tone": "Writing as the business, not personally.",
    },
}


def slug_id(name: str | None) -> str:
    """A Space name as a permanent id: "Partnerships" -> "partnerships".

    Deliberately NOT `domain/target.slug`, which strips company suffixes — that function would
    turn a Space called "Design Co" into "design", and unlike a company name a Space name has no
    legal form to remove. Two functions because they answer two questions, and sharing one would
    make a rule about companies silently apply to campaign names.

    This is PERMANENT once a Space exists: for a targets Space it is hashed into every
    `contact_id` via the anchor (§13.2). The UI derives it from the name and then shows it,
    because a value the operator can never change should not be invisible when it is chosen.
    """
    import re
    import unicodedata
    folded = unicodedata.normalize("NFKD", (name or "").strip()).encode("ascii", "ignore").decode()
    return "-".join(p for p in re.split(r"[^a-z0-9]+", folded.lower()) if p)[:48].strip("-")


def from_template(space_id: str, name: str, template: str, **overrides) -> Space:
    """A new Space prefilled from its template. Overrides win over the prefill."""
    if template not in TEMPLATE_DEFAULTS:
        raise ValueError(f"unknown template {template!r}; expected one of {TEMPLATES}")
    data = dict(TEMPLATE_DEFAULTS[template])
    data.update(overrides)
    return Space(id=space_id, name=name, template=template, **data)
