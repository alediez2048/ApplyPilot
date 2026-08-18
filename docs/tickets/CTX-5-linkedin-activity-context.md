# CTX-5 - Pasted LinkedIn activity becomes contact context

**Size:** M · **Depends on:** NET-2, NET-3, CTX-2, CTX-3 · **Status:** BUILT 2026-08-18
**PRD:** Contact-level context synthesis · **Gate:** LLM provider only
**Intent:** Turn operator-pasted LinkedIn activity into concise, person-specific context that every outreach channel can use.

## Summary
Add a per-contact workflow in the People tab that lets the operator paste recent LinkedIn
activity, posts, comments, profile updates, or copied profile snippets for a contact. ApplyPilot
then summarizes that raw text into a short, factual "what this person appears to be working on
or talking about" note and stores it on the contact.

That synthesized note feeds the existing drafting path for email, LinkedIn DM, SMS, and replies,
so regenerated outreach can reference the person with more specificity than title/company alone.

This is deliberately manual. ApplyPilot does not scrape LinkedIn, open profiles automatically,
read activity feeds, or infer private information. The operator chooses what text to paste.

## Why
Today the pipeline can:

1. Apply to a job.
2. Find contacts.
3. Draft email, LinkedIn messages, and SMS.

But most contacts have only name, title, company, email, and maybe LinkedIn URL. That produces
decent role/company outreach, but not genuinely customized outreach. The best personalization
often comes from what the person recently posted, shipped, hired for, spoke about, or commented
on. LinkedIn activity is a useful source, but automated scraping is risky and brittle.

The product shape should be:

- human gathers the LinkedIn activity;
- ApplyPilot compresses it into clean context;
- the drafting system uses that context without parroting pasted wording.

## Product Flow
1. The operator opens a company/job card.
2. They open the **People** tab.
3. On an individual contact row, they click **Enrich**.
4. A small panel opens under that contact.
5. The operator pastes copied LinkedIn activity/posts/profile snippets.
6. They click **Enrich**.
7. ApplyPilot calls the configured LLM and produces a short contact-context summary.
8. The summary is shown in the panel and saved on the contact.
9. The contact row shows a clear indicator such as `context in draft`.
10. When the operator regenerates email, LinkedIn DM, SMS, or reply drafts, the new summary is
    included in the prompt through the existing person-specific context layer.

## UX
The control belongs on each contact row, near the existing per-contact actions. It should not sit
at the company-card level because the source text is specific to one person.

Closed state:

`Enrich` button, with the saved context visible inside the expanded panel.

Open state:

- textarea: pasted LinkedIn activity
- primary button: `Enrich`
- secondary button: `Cancel`
- status/error line
- saved summary preview

The panel should survive dashboard refreshes while the operator is typing, using the same
client-side buffer pattern as add-contact/job-context forms.

## Data Contract
Preferred minimal path:

Use the existing `contacts.noticed` column as the synthesized contact-context summary. It already
feeds outreach prompts as "what the sender noticed about this person" and is already wired into
email drafting.

Potential additive columns if auditability matters:

```text
linkedin_activity_raw       TEXT      -- operator-pasted source text, capped
linkedin_activity_summary   TEXT      -- synthesized outreach context
linkedin_activity_updated_at TEXT
```

Recommendation:

Start with `contacts.noticed` only unless we decide the raw paste must be recoverable. If raw
storage is added, never feed the raw text directly into drafting. Drafting should read only the
summary to avoid long prompts, accidental quoting, and stale/noisy context.

## Backend Scope
- Add `POST /api/contact/enrich`.
- Body:

```json
{
  "contact_id": "abc123",
  "text": "pasted LinkedIn activity...",
  "replace": false
}
```

- Validate `contact_id` exists.
- Cap pasted text, likely 8k-12k chars.
- Reject empty text with a useful message.
- Summarize through the existing LLM abstraction.
- Store the summary on `contacts.noticed` or the chosen summary column.
- Log an interaction/activity event on the job.
- Return `{ok, contact_id, summary, message}`.

## Synthesis Prompt Requirements
The summarizer should produce facts, not copy. It should be short enough to fit naturally into
outreach prompts.

Output target:

- 2-4 bullets or a 40-90 word paragraph.
- Focus on recent work, public interests, hiring signals, talks, product launches, technical
  themes, and repeated topics.
- Avoid private/sensitive traits, protected-class inference, health/family/politics unless the
  operator explicitly pasted a professional-public context and the summary can ignore it.
- Avoid invented dates, employers, claims, or certainty.
- Use careful language: "appears to", "recently posted about", "seems focused on".
- Do not include direct quotes unless the quote is short and clearly useful.
- Do not preserve LinkedIn boilerplate like "reactions", "comments", "followers", "connect",
  "message", or mutual-connection text.

## Drafting Integration
The current outreach system already has a person-specific `noticed` block. CTX-5 should use that
same tier.

Required behavior:

- Email drafts receive the synthesized summary.
- LinkedIn message drafts receive the synthesized summary.
- SMS drafts receive the synthesized summary.
- Reply drafts can receive it where contact context is available.
- `draft_variant` should include a clear bit, likely `noticed` if already used or `activity` if
  we need to distinguish LLM-synthesized activity from manually typed observations.
- Existing drafts are not automatically regenerated on save. The operator chooses when to
  regenerate, because drafting spends LLM tokens and may change outreach copy.

## Safeguards
- No LinkedIn scraping or browser automation.
- No background polling of LinkedIn.
- The operator owns the pasted source text.
- Pasted text is never sent to Apollo.
- Summary is stored as unverified operator-provided context, not as a factual verifier result.
- The model must be instructed not to quote or mimic the source phrasing.
- Do not auto-send anything after summary creation.
- Do not overwrite a manually edited `noticed` summary without confirmation if one already
  exists.
- If raw text is stored, cap it and make clearing it possible.

## Acceptance Criteria
- Each contact row exposes an **Enrich** action.
- Clicking it opens a stable paste panel for that contact.
- Pasted LinkedIn activity can be summarized and saved.
- The saved summary is visible from the contact row/panel.
- Existing contact context is preserved unless the operator explicitly replaces it.
- Regenerating outreach after saving activity uses the saved summary.
- The feature works for contacts from Apollo, manual LinkedIn import, sheet import, and manual
  add.
- Empty paste returns a clear error.
- Missing LLM configuration returns a clear error without losing typed text.
- No LinkedIn automation is introduced.

## Tests
- Service test: empty paste is rejected.
- Service test: pasted activity is summarized and stored on the intended contact.
- Service test: existing `noticed` is not overwritten without explicit replace.
- Prompt test: summarizer prompt asks for facts, not phrasing, and avoids sensitive/private
  inference.
- Drafting test: saved contact activity appears in email prompt.
- Drafting test: saved contact activity appears in LinkedIn/SMS prompt paths.
- Dashboard render test: each contact row exposes the action.
- Dashboard JS test: typed paste survives refresh/re-render.
- Endpoint test: invalid contact id returns 404 or `{ok:false}`.
- Endpoint test: missing LLM provider returns a clear non-destructive error.

## Manual Verification
1. Open a contact with no saved activity context.
2. Copy recent LinkedIn activity text manually.
3. Paste it into the contact's **Enrich** panel.
4. Click **Enrich**.
5. Confirm the summary is short, factual, and does not copy LinkedIn boilerplate.
6. Regenerate that contact's email.
7. Confirm the draft references the person-specific activity naturally.
8. Regenerate LinkedIn/SMS drafts if available and confirm they also use the context.
9. Try an empty paste and confirm the typed buffer is not lost.
10. Try a contact that already has context and confirm replacement is explicit.

## Out Of Scope
- Automated LinkedIn scraping.
- Ranking contacts by activity.
- Sending LinkedIn messages.
- Auto-regenerating all drafts at a company.
- Verifying that the pasted activity is true.
- Storing or analyzing private LinkedIn pages beyond what the operator manually pastes.

## Open Questions
1. Should raw pasted LinkedIn activity be stored, or should only the synthesized summary survive?
   Leaning: summary only for v1.
2. Should the button be named **Activity context**, **Add LinkedIn activity**, or **Personalize**?
   Decision: **Enrich**, because it is short enough to fit on every contact row and the source
   may be LinkedIn activity, launch notes, conference text, profile blurbs, or other public
   professional context.
3. Should the summary overwrite `contacts.noticed`, or should `noticed` remain manually typed
   while a new `linkedin_activity_summary` column feeds drafts alongside it?
   Leaning: use `noticed` first because it is already the person-specific drafting tier.
4. Should saving the summary offer a one-click **Regenerate email** action in the panel?
   Leaning: yes, as an explicit second action, not automatic.
