# ID-1 — Per-identity sender: wire the `identities` registry into the send path

**Size:** L (~3–4 days, 13 commits) · **Depends on:** SPACE-1a…4b (shipped), migration 003 · **Status:** TODO

Verified against branch `context` @ `d12474d`. Line numbers below were re-measured; several in the draft plan were stale and are corrected here.

---

## Why

**The `identities` table exists and nothing in `src/` reads it.** `migrations/m003_spaces.py:44-58` creates it with 11 columns; the only caller of `repo/spaces.py:166 identities()` anywhere is `tests/test_spaces_registry.py:186`. Live: exactly one row, every field NULL —

```
personal|Personal|||||||||2026-08-04T21:16:57.101201+00:00
```

**The sender is rebuilt from process-global env at seven sites across three entry points**, none of which takes a Space or an identity:

| Fact | Where it is derived, today |
|---|---|
| From address | `gmail_send.py:67-78 _from_address()`, called at `:329`, `:406`, `:478` |
| From name | `os.environ["OUTREACH_FROM_NAME"]` at `:266`, `:322`, `:401`, `:474` |
| Token | `gmail_oauth.py:46 TOKEN_PATH`, a module constant read by 5 functions and *implied* by 7 more |
| Signature | `gmail_send.py:147-150 signature_path()` → one global file `~/.applypilot/signature.html`, read first and unconditionally at `:167-169` |
| Deck base | `outreach.py:104-138 _intro_deck_url()` → `INTRO_DECK_URL` / profile |
| Limits | `gmail_send.py:41,47,48` — module constants read from `os.environ` at **import**, so no per-identity value can be injected into them |

**The documented `identity_id` freeze does not exist.** `domain/space.py:240` freezes `("id", "shape")` only; `repo/spaces.py:210` writes `identity_id` straight into `UPDATE spaces SET name=?, template=?, identity_id=?, config=? WHERE id=?`. A Space with 133 sent emails can be repointed at another mailbox today with no error.

**`UNAPPLIED` is empty and the Space docstring is wrong.** `domain/space.py:114` is `UNAPPLIED: tuple[str, ...] = ()`; `identity_id` is read only inside the two `owners` files the guard excludes (`domain/space.py:130,192,224`; `repo/spaces.py:177,188,193,195,210,211`). The docstring at `:118-124` opens *"Every field is read by something as of SPACE-4"* — the enumeration that follows is accurate, the universal quantifier is not.

**Three things become data corruption the instant a second sending address exists**, all latent today because `OUTREACH_FROM_ADDRESS == GMAIL_ADDRESS == connected account`:

1. `domain/replies.is_inbound(message, connected_email)` (`domain/replies.py:67-83`) takes ONE address, and `replies.py:352` feeds it `me = connected_email()` (`:323`). A hit reaches `mark_replied` → writes `contacts.replied_at` **and** `set_sequence_status(cid,'email','replied')`. `replies.py:348` then skips any contact that already has `replied_at`, so **this never self-heals.** Our own outbound from a second alias, arriving without a SENT label, classifies as a reply, kills the ladder, and inflates `metrics.by_variant`.
2. `domain/conversations.timeline(messages, me: str)` (`:160`) takes one address and is the write path for `messages.direction` (`replies.py:87`, `:149`); `fetch_thread_text` is worse — `replies.py:202` filters our own mail with a single-address `!=` and `:206` hardcodes `"direction": "in"`. This one *does* self-heal (`upsert_messages` is `INSERT OR REPLACE`), which is why it is the second priority, not the first.
3. `_adopt_threads_by_address` (`replies.py:237-286`) sweeps the **personal** mailbox for the addresses of contacts in **every** Space and permanently stamps a `thread_id` onto whichever waiting contact it finds (`:281-283`). Its `by_addr = {email: c}` at `:257` collapses two contacts sharing one address, last-writer-wins.

**Two §49 gaps land in lines this ticket is already editing.** `can_autosend` is enforced at one of four doors (`web_dashboard.py:3064`; ungated: `/api/outreach/send`, the bulk runner at `:270`, `_send_reply`). `offer_deck` is read at one of three deck-emitting drafters (`outreach.py:580` → `:656`); `draft_followup` calls `_intro_deck_url` unconditionally at `:830` and force-appends at `:895`, `draft_linkedin_followup` at `:1164` / `:1199` — both **accept** `space` and never read the field (§39/§73).

**Live freeze window, measured:** `partnerships` and `gauntlet` have **zero contacts**, so zero sends of any kind. `job-search` has 133 `sent_message_id`, 152 `dm_sent_at`, 0 `sms_sent_at`, 118 sent touches, 253 outbound `messages`. Both other Spaces are freely reassignable **right now**; that window closes on their first send.

---

## Design

**1. A NULL column means "inherit the globals" for `personal` and NOTHING ELSE.**
This is the correction that reshapes the whole ticket. `m003_spaces.py:80-86` defines NULL as *"resolve this from the globals that are in force today"* — and it is a statement about **the row it inserts**, not about the column. Every one of those globals is the personal mailbox's: `TOKEN_PATH`, `OUTREACH_FROM_ADDRESS`, the one `signature.html` file, `INTRO_DECK_URL`. A resolver that falls back unconditionally means an operator who creates `business`, fills in a name, and has not yet completed its OAuth consent (a separate manual act — see Blockers) sends 40 pitches authenticating as, From, and signed as the personal job-search identity. Nothing raises: the token exists and the send succeeds.

So resolution is split by what the field IS:

| Field | `personal`, NULL | any other identity, NULL |
|---|---|---|
| `token_path` | `gmail_oauth.TOKEN_PATH` | **hard refusal** — surfaced by `transport()` and `can_send()` |
| `from_address` | `OUTREACH_FROM_ADDRESS or GMAIL_ADDRESS or connected_email()` | `connected_email(identity)` — derived from **its own** token, never a global |
| `from_name` | `OUTREACH_FROM_NAME` | `""` (bare address). An unset name is honest; the personal name is impersonation |
| `signature_html` | the file, then `fetch_signature()` | `fetch_signature(identity)` on its own account. **Never** the global `signature.html` file |
| `deck_base_url` | `INTRO_DECK_URL` / profile | `""` → the drafter emits no deck link. Emitting the personal deck from a business identity leaks it |
| `daily_limit`, `cooldown` | env | env. These are policy numbers, not identity |
| `deck_collector_*` | env | env (see the ID-2 precondition) |

The rule to remember: **credentials and voice refuse or derive from the identity's own token; policy numbers inherit.**

**2. `token_path` is resolved under `APP_DIR` and validated, not trusted.** It becomes an unvalidated filesystem path read out of SQLite and handed to `write_text()` (`gmail_oauth.py:153`, `:114`). `.gitignore` contains **no token pattern at all** (read in full: `*.env`, `*.db`, `profile.json`, `resume.txt`, build noise), and `.githooks/pre-commit:25` matches `*gmail_token*|*oauth_client*|*_token|*token.json` — none of which catches `tokens/business.json`. A relative path resolves against the repo root, because the Dev workflow starts the dashboard from there. On a **public fork** that push cannot be un-published. Second failure from the same missing check: a mistyped path of `~/.applypilot/applypilot.db` gets `write_text`-overwritten by `connect()` and json-parsed by `granted_scopes()`.

**3. Record the From AND the authenticating account — they are different questions.** `_from_address()`'s own docstring (`gmail_send.py:70-77`) says the env var *"lets you send from a different verified alias … while authenticating as another Gmail account"*. Given that a second OAuth consent needs the human at a browser, the likeliest first "second identity" is a **verified send-as alias on the existing account** — sharing one Gmail quota. So two columns:
- `sent_from` — the From header. What the recipient sees; the unit the **company cap** is about.
- `sent_account` — `connected_email(identity)`. The Gmail ceiling; the unit the **daily limit** is about.

Both are measured at send time, so neither is an inference. This answers blocker 3 of the draft: the quota reading of `OUTREACH_DAILY_LIMIT` is correct, but the column the draft picked was not the quota key.

**4. The daily limit scopes to the account; the company cap stays global.** `domain/space.py:16-20` already writes the arithmetic down: two senders each capped at 8 is 16 emails to one employer, and the recipient does not know what a Space is. `emails_sent_to_company` (`store.py:233-265`) already unions the `contacts` and `touches` legs and is accidentally the safe direction — do **not** add an identity dimension to it, only change its refusal sentence (`gmail_send.py:124-125`, *"a company sees one sender, not seven threads"*), which stops being true.

**5. `sent_today()` is separately, independently wrong and must be fixed in the same step.** `store.py:219-230` counts only `contacts.outreach_status='submitted'` — it misses every follow-up (`touches`) and every reply (`messages`). Measured on this machine's own history that is a number wrong by roughly 2.5×. A per-identity limit built on it inherits the error, and all three caps are `0` in the live `.env`, so **the per-identity limit will be the first cap ever to fire here.**

**6. `has_sent()` has FIVE legs, and the one the draft missed is `send_reply`.** `send_reply` (`gmail_send.py:427-503`) never calls `store.mark_sent`, never sets `sent_message_id`, and its own docstring says *"sending one must not consume a touch"* — its only record is `record_outbound` → `messages` (`:497`). A `partnerships`-shaped Space whose conversation was inbound-led would be freely reassignable mid-thread. Deliberately **not** `submitted_at` / `outreach_status='submitted'` alone: `claim_for_send` writes those *before* the send and `mark_send_failed` clears them, so that predicate would freeze a Space on a send that failed.

**7. The identity payload is PROJECTED, never a row.** `repo/spaces.py:171` is `SELECT * FROM identities`, and that table holds `deck_collector_token` (declared `secret=True` in `settings.py` in its env form), `token_path`, and after migration 004 a `config` blob. The apply agent is allowlisted the whole playwright server (`apply/launcher.py:147 _ALLOWED_TOOLS = "mcp__playwright,mcp__gmail__send_email"`) on attacker-controlled careers pages under `bypassPermissions`; a top-level navigation to `http://127.0.0.1:8765/api/status` sends no `Origin` and `Host: 127.0.0.1`, so both dashboard guards pass, and `mcp__gmail__send_email` is a live exfiltration channel. `spaces` already projects deliberately (`web_dashboard.py:1876` ships `{id, name, shape}`); identities gets the same treatment. Keep `SELECT *` **inside the data layer** — the bug is shipping the row, not the query.

**8. The freeze is enforced at both ends,** the argument `save()`'s own docstring (`repo/spaces.py:202-208`) already makes for `shape`: `Space.with_()` refuses a *changed* `identity_id`, and `save()` takes a `conn` and refuses when `has_sent(space.id)` and the stored value differs. Leaving the column out of the UPDATE entirely is wrong here — unlike `id`/`shape` it must stay settable while a Space is unsent.

**9. Deck links: identity value wins, then env, then profile.** That inverts today's order (`env` beats `profile`) only for identities that deliberately set the field, which is the point. Measured: `INTRO_DECK_URL` and `SCHEDULING_LINK` are **not** in the live `.env`; both resolve through `profile.json`, so the `personal` chain is unchanged. `INTRO_DECK_PATHS` moves into the identity config blob — it is one global boolean standing in for *"this site can serve `/intro/<name>`"*, which is a claim about **one site**, and turning it on for the ready identity turns it on for the unready one (§Lessons 32's exact failure, four 404'd recruiter links).

---

## Scope / tasks

Ordered. Each numbered block is a safe commit boundary.

### C0 — Housekeeping (zero behaviour change)
- [ ] Rename `repo/spaces.py:166 identities()` → `list_identities()`; update `__all__`. It collides with `networking/store.py:801 identities()` (a 4-column projection over `contacts`, live at `web_dashboard.py:3242`). `from applypilot.networking.store import identities` compiles and returns contacts; every field lookup then yields nothing, silently. Update `tests/test_spaces_registry.py:186`.
- [ ] `domain/space.py:114`: `UNAPPLIED = ("identity_id",)`. Fix the docstring at `:118-124` — keep the enumeration, drop the false universal ("Every field is read by something").

### C1 — The freeze, before anything can choose an identity
- [ ] `repo/spaces.py::has_sent(space_id, conn) -> bool`, five legs, ORed:
  - `contacts.sent_message_id`, `contacts.dm_sent_at`, `contacts.sms_sent_at` (each `IS NOT NULL AND != ''`)
  - `touches t JOIN contacts c ON c.id=t.contact_id WHERE c.space_id=? AND t.sent_at IS NOT NULL AND t.sent_at != ''` (the join is required — `touches` has no space column)
  - `messages m JOIN contacts c ON c.id=m.contact_id WHERE c.space_id=? AND m.direction='out'` (the `send_reply` leg)
- [ ] `domain/space.py:240`: add `identity_id` to the frozen tuple **only when it differs**.
- [ ] `repo/spaces.py:200 save()` takes `conn` and refuses when `has_sent(space.id)` and the stored `identity_id` differs.

### C2 — `networking/identity.py` (new)
- [ ] Frozen dataclass `Identity`: `id, name, token_path (Path), from_name, from_address, signature_html, deck_base_url, deck_collector_url, deck_collector_token, daily_limit, scheduling_link, config: dict`.
- [ ] Per-field resolution exactly as the Design table above. **The fallback-to-globals branch is gated on `id == m003_spaces.DEFAULT_IDENTITY_ID`.**
- [ ] `token_path` resolution: `(config.APP_DIR / (row["token_path"] or "gmail_token.json")).resolve()`; refuse anything not under `config.APP_DIR.resolve()` (rejects absolute paths and `..`) and anything that exists but is not a token file.
- [ ] Field-level secret flags declared beside the dataclass. `Identity.public()` returns `{id, name}`; `__repr__` redacts `deck_collector_token` and the config blob.
- [ ] `resolve(identity_id, conn)`, `for_space(space, conn)`, `for_contact(contact_id, conn)` (contact → `contacts.space_id` → `spaces.identity_id` → row; every link exists, no schema change). `resolve()` never raises on a missing registry — it returns the default, the way `_space_of_contact` (`web_dashboard.py:1444`) already chooses.
- [ ] Memoise the resolved `Identity` on the connection object per `(conn, identity_id)`, following `_registered()` (`repo/spaces.py:56-77`) — `get_connection()` returns a subclass that carries attributes. A miss costs at most one SELECT for a whole render, never one per contact.
- [ ] Add `gmail_token*`, `*token*.json` to `.gitignore`; widen `.githooks/pre-commit:25` to `*token*`. One line each, independent of the resolver being right.

### C3 — `gmail_oauth.py`: per-identity token, both caches, both guards
- [ ] Add `identity: Identity | None = None` to **all twelve** functions that authenticate, not only the five naming `TOKEN_PATH`: `granted_scopes:76`, `missing_scopes:87`, `has_scope:93`, `_load_creds:97`, `available:122`, `can_read_content:127`, `connect:136`, `probe:166`, `fetch_signature:186`, `message_thread_info:215`, `connected_email:248`, `send:278`. `send()` and `fetch_signature()` never mention `TOKEN_PATH` and authenticate as whatever `_load_creds()` returns — threading half of these is §49 with the read side working and the mail going out as the wrong person.
- [ ] `identity=None` MUST keep meaning the module constant. Keep `TOKEN_PATH` exported: `tests/test_query_budget.py:185` and `tests/test_reply_content.py:66` monkeypatch it.
- [ ] `_EMAIL_CACHE` (`:243-275`): key `(str(path), mtime)`. Delete `_EMAIL_CACHE.clear()  # only ever one account connected at a time` at `:273` and evict stale keys for **that path only**. Left as-is, two identities alternating on the render path evict each other every call and restore the §Lessons 26 measurement (2.4s → 0.043s) with nothing in the suite noticing.
- [ ] `granted_scopes()` (`:76-84`) reads and json-parses the token from disk on **every** call, with no memo, and is on the render path via `web_dashboard.py:2492 _content_scope()`. Add the same `(path, mtime)` memo.
- [ ] Test `_lock_down` (`:67-73`) — it is `except Exception: pass` and `grep -rn 'chmod\|0o600' tests/` returns nothing today.
- [ ] Extend `tests/test_gmail_send.py:402-413` in this commit: assert every name in `_DANGEROUS_BUILTINS` (`apply/launcher.py:123`) is in `_DISALLOWED_TOOLS` and none is in `_ALLOWED_TOOLS`, and that `_ALLOWED_TOOLS` is exactly `mcp__playwright,mcp__gmail__send_email`. That deny-list is what keeps N token files unreadable; it is tool-scoped, so it scales without enumerating paths — but nothing pins it.

### C4 — `gmail_send.py`: one sender resolution, both transports
- [ ] `_sender(contact, conn) -> Identity` via `identity.for_contact`. No new query: each entry point already calls `store.get_contact` (`:296`, `:360`, `:451`) and `contacts.space_id` is a real column.
- [ ] `_send_as(identity)` returns `(from_addr, from_name, signature)`. Replace all seven sites.
- [ ] `_smtp_send` (`:257-287`) builds **From** (`:268`), **Reply-To** (`:272`) and the signature (`:279`) from `_creds()[0]`, and `send_outreach:336` / `send_followup:413` / `send_reply:486` build the `make_msgid` domain the same way. Four derivations, none of which calls `_from_address()`. Wire the identity into all four or one transport silently reverts — and a wrong Reply-To is §Lessons 29's shape: both outcomes render the same screen.
- [ ] `transport(identity)` (`:56-64`): **refuse** rather than falling back to the global `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` for any non-default identity. Today an absent OAuth token falls through and reports success. (Policy call — see Blockers; refusing is the safe default and can be relaxed.)
- [ ] `signature_html(from_addr)` (`:153-179`) reads one global file first and unconditionally; it looks per-sender and is not (§39/§73). Key `_SIG_CACHE` on `(identity_id, address)`; `identities.signature_html` wins when set; the file is `personal`-only.
- [ ] `claim_for_send` needs **no** change — keyed on contact id, guards double-send, orthogonal.

### C5 — Provenance, before a second mailbox exists
- [ ] Add `"sent_from": "TEXT"` and `"sent_account": "TEXT"` to `_CONTACT_COLUMNS` (`store.py:112` region) and `_TOUCH_COLUMNS` (`touches.py:38-51`). **Additive dicts, deliberately not a migration** — `get_connection()` does not call `init_db`, so a migration touching a declared column races: duplicate-column one way, NOT-NULL-without-default the other.
- [ ] Write them at `store.mark_sent` (`:454-475`) and, for follow-ups, pass them **explicitly down from `send_followup`**. Do **not** write them inside `touches.record_sent` (`touches.py:268`): three of its five callers are not a transport — `web_dashboard.py:3020` (the operator's "✓ I sent it" for LinkedIn/SMS), `store.py:654 mark_followed_up` (a manual checklist touch, docstring says so), `store.py:703 mark_followup_sent`. Attributing a hand-sent DM to a mailbox that sent nothing is a false provenance claim that C7 then enforces a quota with.
- [ ] Operator-asserted touches stay NULL. So do the 133 historical rows — there is no backfill source (`backfill_thread_ids` can only ask the one connected account), and NULL reads honestly as "sent before provenance existed".

### C6 — "Who is us" becomes a set, unrecoverable path first
- [ ] `domain/replies.is_inbound` / `replies_in` / `bounces_in` take `me: str | list[str]`. Pass the address set at `replies.py:352-353`. **This is the priority**: a hit here writes `contacts.replied_at` and terminates the email sequence, and `replies.py:348` skips already-replied contacts so a later poll never repairs it.
- [ ] `domain/conversations.timeline(messages, me: str | list[str])` (`:160`), same union `reply_target` (`:224`) already takes, with the SENT-label check `is_inbound` (`:67-83`) already does. Apply at `replies.py:87`, `:149`, `:202`, `:206`. Lower priority: `upsert_messages` is `INSERT OR REPLACE`, so `direction` self-heals.
- [ ] `_adopt_threads_by_address` (`:237-286`): `by_addr` becomes `{email: [contacts]}` — a shared address currently discards one row silently, and `contact_id` is `sha1(job_url|linkedin_url|name)`, so one human contacted from two Spaces is two rows with one email.
- [ ] **Scope the sweep and the poll to the polling identity's Spaces.** Without it, the sweep runs against mailbox A, finds A's thread, and permanently stamps it on a Space-B contact emailed from mailbox B; every subsequent inbound in A's conversation then `mark_replied`s the wrong contact. Full per-identity polling (a watermark per token, N mailboxes) is ID-2 — this is the one-filter guard that makes deferring it safe, and **C13 must not ship without it.**

### C7 — `can_autosend` and `offer_deck` at every call site
- [ ] Move `can_autosend` into `gmail_send`'s three entry points (which now resolve the Space anyway) and **delete** the dashboard copy at `web_dashboard.py:3064`. Its own comment at `:3058-3062` already argues it belongs on the send path *"rather than by hiding a button — this endpoint is reachable without one"*. Three doors are ungated today.
- [ ] `draft_followup` (`outreach.py:830`) and `draft_linkedin_followup` (`:1164`) read `space.offer_deck` at the `_intro_deck_url` call, the way `draft_email` does at `:580`/`:656`. Note `:843` is deliberately contact-less (the un-personalised base) and must be gated too.

### C8 — Limits become functions of the identity
- [ ] **Delete** `_DAILY_LIMIT`, `_COMPANY_CAP`, `_COOLDOWN_DAYS` (`gmail_send.py:41,47,48`). Replace with `_daily_limit(identity)`, `_company_cap()`, `_cooldown_days()`. Deleting rather than shadowing is load-bearing: five existing tests monkeypatch those constants, and a surviving constant makes them pass while setting a value nothing reads.
- [ ] Apply at **all five guard sites**, not the one with coverage: `can_send:115,117,121`; `send_followup:383,388`; `send_reply:467`. Follow-up touches are the majority of volume (118 of 251 sends here).
- [ ] Preserve §Lessons 50 exactly: daily and company guarded `> 0`, a zero-day cooldown matching nothing.
- [ ] Fix `sent_today()` (`store.py:219-230`): union the `touches` leg the way `emails_sent_to_company` (`:233-265`) already does, and scope by `sent_account`. Legacy NULL rows count for the default identity only — or accept the under-count and say so in the refusal.
- [ ] The refusal must name the identity and its limit. This will be the first cap ever to fire on this machine.
- [ ] Change `emails_sent_to_company`'s refusal sentence at `:124-125`. Do **not** add an identity dimension to the query.

### C9 — Migration 004
- [ ] `migrations/m004_identity_config.py`: PRAGMA-guarded `ALTER TABLE identities ADD COLUMN scheduling_link TEXT` and `config TEXT`. A **new numbered migration**, not an edit to 003 — that file's docstring (`:42-44`) forbids rebuilding the table with today's columns. `identities` has no additive-column dict, so the §Spaces race does not apply.
- [ ] The `config` blob is the higher-leverage half: it is exactly what made "a new field is never a schema change" true for `spaces`, and identities is the registry that did not get it.
- [ ] Bump the schema version; verify `applypilot migrate --status` reports 4.

### C10 — Link resolvers
- [ ] `_scheduling_link(profile, identity)` (`outreach.py:83-91`) and `_intro_deck_url(profile, contact, identity)` (`:104-138`), all eight call sites: `:631`, `:656`, `:829`, `:830`, `:843`, `:1082`, `:1164`, and `cli.py:616` (deck-relink).
- [ ] Precedence: identity SET → env → profile → `''`.
- [ ] `INTRO_DECK_PATHS` (`outreach.py:100-101`, `:128`) moves into the identity config blob, env as fallback.
- [ ] Fix the false comment at `outreach.py:127` — *"applypilot doctor checks the live URL"*. `grep INTRO_DECK src/applypilot/cli.py` returns zero hits. Either build the check (per identity) or delete the sentence; do not leave a documented guard that does not exist.
- [ ] Remove `identity_id` from `UNAPPLIED`, **and** add the reverse guard (T14 below) in the same commit.

### C11 — Dashboard
- [ ] `/api/status`: add `"identities": [i.public() for i in …]` beside `space_templates` (`web_dashboard.py:1890`). **Projected, never the row** (Design 7). Reuse the C2 connection memo — `list_identities()` costs two statements per call (a `sqlite_master` probe plus the SELECT) with no memo, against 74/80 measured headroom (`tests/test_query_budget.py:26`).
- [ ] One `<select id="newSpaceIdentity">` inside the **existing static** `#newSpaceForm` (`index.html:46-55`), rendered on open like `renderTemplatePicker()` (`dashboard.js:86-93`), posted by `createSpace` (`:107-111`). That container is safe by construction — static markup outside `#jobs`, never replaced by `refresh()`. Do **not** put it inside `#jobs` or `#accountsBar`: `renderAccounts()` (`dashboard.js:2070-2113`) rewrites that node every 2.5s, and `isEditingJobs()` (`:1880-1883`) only matches `INPUT`/`TEXTAREA` inside `#jobs`, so a focused `<select>` is protected nowhere.
- [ ] `_create_space` (`web_dashboard.py:2344-2383`) validates against `list_identities()` ids and **refuses** an unknown one, naming it — filing a Space under a row that does not exist is §Lessons 70's shape. Pass `identity_id=` to the kwarg `repo.create_space` already declares (`spaces.py:177`) and no production caller has ever used.
- [ ] Render the select only when `len(identities) > 1`.

### C12 — `doctor`, `connect --identity`, secret masking
- [ ] Make the four singular rows per identity and labelled: `cli.py:1018` (auth probe), `:1031` (scopes, with `fetch_signature()` called with no address), `:1042-1051` (reply detection), `:1058-1067` (reply content). Also `cli.py:354`. `doctor --config` is where a half-wired identity gets caught, and a half-wired one is exactly the case `transport()` would otherwise resolve by silently using the global SMTP account.
- [ ] `network --gmail-connect --identity <id>` (`cli.py:279-346`), defaulting to `personal`, writing to that identity's validated `token_path`. `SCOPES`/`CONTENT_SCOPE` stay module constants — the three pinning assertions are over the SCOPES list, not the token file, and are unaffected.
- [ ] Route every per-identity render through the C2 secret flags. `settings.source()` (`settings.py:245-250`) returns one of three strings and `describe()` resolves from env only, so a per-identity override makes the global report confidently wrong — worse than not reporting it. Print the effective per-identity value with the global shown as a fallback (chosen over a fourth `identity:<id>` label: the operator needs the number that is enforced, not its provenance). Treat the C9 config blob as **secret-by-default in output** until a key is explicitly declared non-secret.

### C13 — Unlock `business` (separate commit, after a real second mailbox)
- [ ] Add `"business"` to `OFFERED_TEMPLATES` (`domain/space.py:45`). It is enforced at one place (`web_dashboard.py:2362`) and the picker is built from the same tuple (`:2394-2396`), pinned together by an existing test — so it really is one entry.
- [ ] Add the gate to `repo.create_space` (`spaces.py:176-198`), which **writes**. Do **not** gate `from_template()` — see Rejected findings.
- [ ] **Replace** `test_business_is_refused_and_says_why` (`tests/test_spaces_dashboard.py:370-376`); do not delete it. New assertions: creating a business Space assigns a non-`personal` identity, **and** a business template is still refused when only one identity row exists.
- [ ] Precondition: a second `identities` row with a real `token_path` whose OAuth consent the human has completed, **and** C6's poll scoping shipped.

---

## Not in scope

- **ID-2: per-identity reply polling.** `gmail_read.WATERMARK_PATH` is one file, read at `replies.py:313`. A Gmail history id is meaningless in another mailbox, and only a NON-empty `touched` set narrows the work (`:315-320`) — so a wrong-but-valid id wins silently and the poll returns ok having examined nothing. C6 ships the guard (poll scoped to the polling identity's Spaces) that makes deferring this safe; do not connect a second mailbox to polling until ID-2.
- **ID-2: per-identity deck collector.** `deck_hits.fetch()` reads one `DECK_HITS_URL`/`DECK_HITS_TOKEN` (`:69-96`) and `poll()` matches hits against every contact via `all_contacts_for_metrics(conn)` with no `space_id` (`:132`), though that function accepts one. **ID-1 ships with a PRECONDITION:** every identity's `deck_base_url` must be on the host the single collector serves. Violate it and you get one of two silent failures — a second host with no collector returns `{ok: True, note: "no clicks recorded"}` (§58's exact signature), and a second host posting into the *same* collector cross-attributes, which `dismiss()` cannot durably undo (§64).
- **ID-2: per-identity `can_read_content()`.** C3 adds the parameter; making all eight callers (`gmail_read.py:145`, `web_dashboard.py:2492`, `tick.py:259`, `replies.py:120/184/247`, `bookings.py:57/104`) pass the right one is ID-2. Until then the answer comes from the default identity.
- **`LINKEDIN_DM_DAILY_LIMIT` / `NETWORKING_LINKEDIN_DAILY_LIMIT`** declared 15 and 20 in `settings.py:168-170`, enforced as 5 and 5 in `linkedin_dm.py:191` / `linkedin_agent.py:67`, where zero also means SEND NOTHING. Real, verified, and **not ID-1** — it is a default-in-two-places bug on a channel this ticket does not touch. Its own ticket.
- **`job_url` → `anchor`** (SPACE-1b; 169 refs in 18 source files).
- **SPACE-6** — proving `business` differs from `outreach` by `identity_id` alone. C13 unlocks the template; falsifying the PRD's claim is separate.
- **The apply agent's Gmail MCP** (`@gongrzhe/server-gmail-autoauth-mcp`, `apply/launcher.py:101-104`) has its own credential store and has never read `TOKEN_PATH`. A per-identity ApplyPilot does not make the apply agent send as that identity. Write that down in the launcher — nothing currently records that it is true.
- **`domain/deck.py`'s hardcoded `/intro/`** (scanner regex `:35`, `_ANY_DECK_LINK` `:113`, `_RESERVED` `:122-124`). A second **host** works (`relink` scopes by host, `:141`); a second **path** silently breaks deck-relink, `slugs_in` and the collector's plain-text fallback. ID-1 constrains identities to the same path.

---

## Risks

| Risk | Mitigation |
|---|---|
| A half-configured identity sends as `personal` — right From on screen, wrong mailbox in reality | Design 1: the globals fallback is gated on `id == DEFAULT_IDENTITY_ID`. NULL `token_path`/`from_address` on any other identity is a refusal surfaced by `transport()` and `can_send()`, not an inherited value. T2 is the mirror test |
| A second token file is written into the repo and pushed to a **public** fork | `token_path` resolved under `APP_DIR` and refused otherwise (C2); `.gitignore` + hook widened independently of the resolver |
| A bearer token reaches the apply agent through `/api/status` | Payload projection `Identity.public()` + redacting `__repr__` + T13; the deny-list pinned by the C3 test |
| `identity_id` repointed on a live Space mid-conversation | Five-leg `has_sent()` incl. the `messages`-out leg; refused at both `with_()` and `save()` |
| Own outbound stored as a reply → ladder killed, `replied_at` written, `by_variant` inflated, never self-heals | C6, unrecoverable path first (`is_inbound`/`replies_in`), and T9 asserts on `replied_at` and the `sequences` row, not on `direction` |
| Two aliases on one Google account each get a full daily quota | `sent_account` (the authenticating mailbox), not `sent_from`, is the daily-limit key |
| A `<select>` eaten by the 2.5s refresh | It lives in the static `#newSpaceForm`, outside `#jobs`; `isEditingJobs()` covers neither `#accountsBar` nor `<select>` |
| `/api/status` regresses past 80 statements or re-acquires a per-render HTTP call | Connection-scoped memo (`_registered()` pattern); T12 keeps the two-size comparison and counts Gmail round-trips |
| The two `_EMAIL_CACHE`/`granted_scopes` fixes are deferred "until there is a second identity" | They are in C3 with the parameter. Two identities alternating on the render path restores the §26 2.4s measurement with nothing in the suite noticing |
| Deck links from a second host record nothing, or cross-attribute | Stated precondition, enforced by refusing a NULL `deck_base_url` on a non-default identity rather than inheriting the personal one |

---

## Tests

**T1 — `test_the_personal_identity_resolves_to_todays_globals_field_by_field`**
*Proves:* for every field of `Identity`, `resolve("personal")` against the NULL-everything seed equals the expression that produced it before ID-1. Iterates `dataclasses.fields(Identity)`, so a new field with no fallback FAILS rather than silently becoming `''`.
*Vacuous if:* written as a literal field list; or both sides computed by the new resolver (§60 — the right-hand side must be the **original** expression inlined, e.g. `os.environ.get("OUTREACH_FROM_ADDRESS","") or os.environ.get("GMAIL_ADDRESS","")`); or the env has none of the vars set so every comparison is `'' == ''` — set them and assert non-empty first.

**T2 — `test_a_non_default_identity_never_inherits_the_personal_mailbox`** *(the mirror of T1; the single most important test here)*
*Proves:* `resolve("business")` with an all-NULL row does **not** equal `resolve("personal")` — no shared `token_path`, no shared `from_address`, no shared signature, no shared deck base — and that a send attempt on it refuses with a message naming the missing field.
*Vacuous if:* the fixture row has any of those columns populated. Seed it as m003 seeds `personal` — id and name only.

**T3 — `test_the_identity_token_file_is_what_authenticates`**
*Proves:* the central claim of ID-1. Two real token files on disk with distinguishable payloads; `_load_creds(identity=B)` returns B's credentials, and `connect(identity=B)` creates B's `token_path` while leaving the default `TOKEN_PATH` **unmodified** (compare mtime before/after).
*Vacuous if:* omitted — and every other test survives `_token_path(identity) -> return TOKEN_PATH`. T1 reads the identities ROW; T7 asserts a From header that `gmail_oauth.send` takes as an argument (`:298`), never from the credentials; T5 asserts the mode of whatever file was written; T4's own bound holds when both paths collapse to one. Fold this into T5 rather than adding a file.

**T4 — `test_two_identities_do_not_evict_each_other_from_the_address_cache`**
*Proves:* `connected_email()` alternating between two token paths costs at most one Gmail round-trip per `(path, mtime)`. Counts `execute()` on the fake service, as `tests/test_query_budget.py:125-161` already does.
*Vacuous if:* phrased `calls["n"] <= 1` (the current assertion at `:160`) — with two identities the honest bound is `<= distinct_tokens`, and `<= 1` fails correct code while `<= 99` passes the eviction bug. Also vacuous if both fixtures resolve to the same path; assert the paths differ first. Rewrite the existing assertion in identity terms **before** touching the cache.

**T5 — `test_a_written_token_is_not_world_readable_and_stays_inside_APP_DIR`**
*Proves:* both token writers — `connect()` (`:153`) and the refresh branch of `_load_creds()` (`:114`) — leave mode 0600; and `token_path` values of `../escape.json`, `/etc/x.json` and `tokens/b.json` are refused.
*Vacuous if:* only `connect()` is driven (the refresh writer is the one a second identity hits daily); or the file was created by the test with a umask that happened to yield 0600 — write it 0644 first, then run the code, then assert.

**T6 — `test_identity_id_cannot_change_once_a_space_has_sent_anything`** *(parametrised over five proofs)*
*Proves:* each of `sent_message_id`, `dm_sent_at`, `sms_sent_at`, a `touches` row with non-empty `sent_at`, and a `messages` row with `direction='out'`, **seeded alone**, freezes the Space — at `with_()` **and** at `save()` on a hand-built manifest.
*Vacuous if:* only `sent_message_id` is seeded (an implementation of one leg passes all of it); or the proof is `submitted_at`/`outreach_status='submitted'`, written by `claim_for_send` (`store.py:328`) **before** the send and cleared by `mark_send_failed` (`:481`) — that predicate would freeze on a failed send; or the assertion is `pytest.raises(Exception)`, which passes on a typo.

**T7 — `test_an_unsent_space_can_still_be_reassigned`**
*Proves:* the freeze is a freeze, not a wall. Without it, `has_sent` could be `return True`.
*Vacuous if:* the Space has no contacts at all — seed a `drafted` contact, a `touches` row with `sent_at IS NULL`, **and one with `sent_at = ''`** (these columns hold both, which is why the predicate tests `!= ''`), plus a `messages` row with `direction='in'`.

**T8 — `test_every_send_entry_point_resolves_the_same_sender_on_both_transports`**
*Proves:* `send_outreach`, `send_followup` and `send_reply` all send as the identity resolved from the contact's Space — asserting **From, Reply-To, the Message-ID domain and the attached signature**, per entry point, on **both** transports. Capture the whole `EmailMessage` in the fake transport.
*Vacuous if:* only From is asserted (a wrong Reply-To sends every answer to the personal mailbox and renders identically — §29); or only the OAuth path is driven, since `_smtp_send` never calls `_from_address()`; or the fixture identity has a NULL `from_address`, so the resolved value equals the global regardless of whether the identity was consulted.

**T9 — `test_our_own_outbound_is_never_a_reply_when_we_send_from_two_addresses`**
*Proves:* `replies_in`/`is_inbound` **and** `timeline`/`fetch_thread_text` classify a message from any of our addresses as outbound, consulting the SENT label first. Drives a full `poll()`.
*Vacuous if:* the fixture uses one sending address — which is the live configuration, so a single-address fixture passes against the broken code unchanged. Supply at least two. Also vacuous if it asserts only `messages.direction`: that path self-heals via `INSERT OR REPLACE`. **Assert `contacts.replied_at` was not written and the `sequences` row did not go terminal `replied`** — those do not self-heal (`replies.py:348`).

**T10 — `test_the_send_path_records_who_sent_it`**
*Proves:* driving `send_outreach` and `send_followup` through a fake transport persists `contacts.sent_from`/`sent_account` and `touches.sent_from`/`sent_account` equal to the resolved identity. The **write**, not the read.
*Vacuous if:* the rows are seeded by the test (`store.upsert_contact({... "sent_from": "a@x"})` and a hand-built touch, the `_person()` pattern at `tests/test_company_cap_and_variant.py:40-60`) — then `mark_sent` writing it while `record_sent` does not is green while every follow-up lands NULL. Also assert the operator-asserted touch (`web_dashboard.py:3020`) leaves them NULL, and state what legacy NULL rows count as.

**T11 — `test_the_daily_limit_counts_the_sending_account_and_the_follow_ups`**
*Proves:* mailbox B is not blocked by mailbox A's sends; a follow-up touch increments the count; two aliases on **one** account share one count.
*Vacuous if:* it inherits the real environment, where all three caps are 0 (`.env` lines 45, 48, 49) and every guard short-circuits — set a non-zero limit. Also vacuous if it asserts the count and not the refusal: assert the (N+1)th send is refused and the message names the identity.

**T12 — `test_zero_still_means_unlimited_at_all_five_guard_sites`**
*Proves:* §Lessons 50 survives the conversion. Drives `can_send:115,117,121`, `send_followup:383,388` and `send_reply:467` with every limit 0.
*Vacuous if:* written in this repo's existing idiom — `monkeypatch.setattr(gmail_send, "_DAILY_LIMIT", 0)` (`tests/test_company_cap_and_variant.py:100,112,193,209`; `tests/test_gmail_send.py:59`). After C8 those constants are **deleted**, so the five existing monkeypatches raise `AttributeError` instead of silently setting a value nothing reads; the test must set the limit through the identity row and through env. Also vacuous if only `can_send` is driven — the current state of coverage, and exactly what a partial conversion leaves behind. Do not copy the existing zero-semantics test's pattern of stubbing `already_contacted_email` to `None`, which leaves the cooldown leg of its own docstring unexercised.

**T13 — `test_no_identity_secret_reaches_the_wire_or_the_terminal`**
*Proves:* no `/api/status` body, no `doctor --config` output and no log line contains a seeded `deck_collector_token`, `token_path` or config-blob value; `_status_payload`'s identities entries have exactly the keys `{id, name}`.
*Vacuous if:* asserted as `token not in html` with an empty seeded token — `"" in s` is True for every string (§71). Seed a distinctive non-empty value and assert it appears in the DB first.

**T14 — `test_identity_id_is_applied_for_a_real_reason`**
*Proves:* after C10, `identity_id` is not in `UNAPPLIED` **and** at least one attribute read of `<holder>.identity_id` exists outside `domain/space.py` / `repo/spaces.py` — the same AST walk, the same closed `HOLDERS` set (`tests/test_space_manifest.py:281`: `{"space","manifest","spc","sp_space","the_space"}`).
*Vacuous if:* omitted. `test_unapplied_fields_are_really_unapplied` only walks for names still **in** `UNAPPLIED`, so removing the entry stops it looking. `identity.for_space(space_id: str, conn)` re-querying `SELECT identity_id FROM spaces` satisfies the empty tuple with nothing — §Lessons 61 inside the guard written to stop it. **Consequence for the implementer: `for_space`'s parameter must be named from the HOLDERS set.**

**T15 — `test_a_space_with_autosend_off_cannot_be_sent_from_any_door`**
*Proves:* all four doors refuse — `/api/outreach/send`, the bulk `send-all-emails` runner, the follow-up channel action, `_send_reply`.
*Vacuous if:* asserted as `res["ok"] is False` (a dozen unrelated reasons return False) — assert the specific refusal AND zero recorded sends, AND that the same fixture DOES send with `can_autosend=True`. **And:** the bulk door is a `daemon=True` thread (`web_dashboard.py:270`), so asserting `transport.sends == []` right after the POST passes on the race, not the guard. Drive `_BulkSend._run` synchronously or poll its status dict to `running: False`.

**T16 — `test_offer_deck_false_removes_the_link_from_follow_ups_and_linkedin_too`**
*Proves:* `draft_followup` and `draft_linkedin_followup` honour `space.offer_deck`.
*Vacuous if:* asserted on the **prompt** — which is what the file's only helper does (`tests/test_space_applied.py:38`, `:194`). `draft_followup` ends `body = ensure_intro_deck(body, deck)` (`outreach.py:895`) and the LinkedIn one at `:1199`, so suppressing the prompt block while leaving `deck` populated passes a prompt-level test and force-appends the link anyway. Assert on the returned **body/message**, parametrised over touches the way `tests/test_intro_deck_link.py:134,290` already does for the positive case, with the positive control in the same test and a real non-empty deck URL (`"" in body` is True for every string).

**T17 — `test_link_precedence_over_all_four_levels`**
*Proves:* for `_intro_deck_url` and `_scheduling_link`: identity SET beats env; identity NULL falls to env, then profile, then `''`. Plus: identity A with `INTRO_DECK_PATHS` on and B with it off produce a named link and a base link from the same drafter call.
*Vacuous if:* the identity value equals the env value, so neither side can be distinguished. The existing `test_env_beats_profile_and_profile_is_the_fallback` (`tests/test_intro_deck_link.py:146`) pins the opposite direction and will keep passing because it never sets an identity value — that is not coverage.

**T18 — `test_create_space_refuses_an_unknown_identity`**
*Proves:* `_create_space` with a bogus `identity_id` returns `ok: False`, names the id, and writes no `spaces` row.
*Vacuous if:* only the return value is checked — assert the row count.

**T19 — `test_status_costs_no_more_statements_with_several_identities`**
*Proves:* the identities list and per-contact identity resolution scale with neither. Keep the existing **two-size comparison** (`many <= few + 2`, `tests/test_query_budget.py:120`), 8 and 32 contacts, spread across at least three Spaces pointing at three different identities **in both sizes**, and count Gmail round-trips as well as SQL.
*Vacuous if:* a flat `< MAX_STATEMENTS` threshold — a per-`(conn, contact_id)` memo re-probing `sqlite_master` each call scales at one statement per contact and still fits under 80 at small N. Also vacuous if the seed omits `strategy` or `space_id` and the payload comes back empty (§13, this exact failure already shipped) — assert the job/contact counts **before** the statement count; and if all contacts sit in the default Space, the identity dimension is never exercised.

---

## Blockers

1. **OAuth consent for a second Google account needs the human at a browser.** `connect()` calls `flow.run_local_server(port=0)` (`gmail_oauth.py:152`). C0–C12 can be built and tested with fake token files; nothing proves a second identity actually sends until a real mailbox is authorised. **C13 must not ship before that**, or the button produces exactly the permanently-personal Space the current refusal exists to prevent.
2. **Second HOST or second PATH for the deck?** A second host costs nothing (`relink` scopes by host, `domain/deck.py:141`). A second path is a rewrite of `deck.py` (`/intro/` is hardcoded at `:35`, `:113`, `:122-124`) and silently breaks deck-relink — 0 matches, exit 0, "no drafts updated". The code cannot pick.
3. **Should `transport()` fall back to global SMTP for a non-default identity?** C4 refuses by default. Whether SMTP should ever become identity-aware, or a non-personal identity is OAuth-only, is a policy call. Related and undocumented: it is **unknown** whether `_smtp_send` ignoring `OUTREACH_FROM_ADDRESS` is deliberate (envelope must match the authenticated account) or an unnoticed §49 gap.
4. **Does a LinkedIn invite or an SMS freeze the identity?** `spaces-prd.md` §13.2 says "zero sent messages" and justifies it with threads and reply polling, which are email-only — but `job-search` carries 152 `dm_sent_at` rows sent under a personal LinkedIn profile, arguably the same sender. C1's predicate includes all three, stricter than the stated reason. **Measured: `partnerships` and `gauntlet` have zero contacts, so this decision is free today and unrecoverable later.**
5. **Confirm the quota reading of `OUTREACH_DAILY_LIMIT`.** `settings.py:119` says "Max emails sent per 24h", which is the Gmail-quota reading; the implementation is neither (first contacts only, globally). C8 assumes the quota reading and keys it on `sent_account`. If the real question is "do not look like a robot", the split is backwards and C8 should not ship.

---

## Rejected review findings

**Gate `from_template()` on `OFFERED_TEMPLATES` (draft step 14).** Rejected. `OFFERED_TEMPLATES` is about what the **picker** offers; `TEMPLATE_DEFAULTS` is about what is **expressible**. Gating the domain constructor conflates them and makes a business manifest unbuildable in a test or from the CLI — including the tests C13 needs. The write path is what must be gated, so the check goes on `repo.create_space` (which INSERTs) and stays on the endpoint. The underlying observation — that the tuple is enforced at one call site while two other paths can create a business Space — is real and is fixed, just one layer lower than proposed.

**`_our_addresses()` on `/api/status` is a query-budget hazard (draft step 3).** Rejected as framed. `_our_addresses()` (`gmail_send.py:81-94`) executes **no SQL** — it reads two env vars and calls `connected_email()`, which is already cached. It is a §26 HTTP concern, already mitigated. The real budget hazards in this ticket are `list_identities()` (two statements, no memo, on a 2.5s path) and any per-contact identity resolution; point the memo at those.

**Record `identity_id` rather than an address because `identity_id` is mutable (draft step 6).** Rejected as reasoning, accepted as conclusion. Once C1 lands, `identity_id` is immutable from the first send onward, so recording it would be a fact, not an inference. The columns are addresses for a different and better reason: they answer the two questions the two caps ask (`sent_from` = what the recipient sees; `sent_account` = the Gmail ceiling), and an identity id answers neither without a join through a registry that can be edited before the freeze bites.

**Drop `SELECT *` from `list_identities()` (security review, implied).** Rejected. `SELECT *` inside the data layer is correct and is what makes migration 004's new columns free. The bug is shipping the **row** to a client. Fixed by projecting at the payload (`Identity.public()`), which is where `spaces` already does it.

**Make `poll()` fully per-identity in ID-1 (corruption review, issue 5).** Rejected in that form — a per-mailbox watermark, N Gmail clients and per-identity `can_read_content()` is ID-2 and is a larger change than everything else here combined. **Accepted in the narrowed form**: C6 scopes the poll and the address sweep to the polling identity's Spaces and fixes the `by_addr` collapse, and C13 is gated on it. The reviewer's core point stands and is why the guard is in ID-1 at all: a sentence in "not in scope" is not a guard.

**Fix `LINKEDIN_DM_DAILY_LIMIT` / `NETWORKING_LINKEDIN_DAILY_LIMIT` while you are here (draft step 13).** Rejected for this ticket, not on the merits — it is verified real (declared 15/20 in `settings.py:168-170`, enforced 5/5 in `linkedin_dm.py:191` / `linkedin_agent.py:67`, with zero meaning SEND NOTHING). It is a §Lessons 50 remnant on a channel ID-1 does not touch, and ID-1 is already thirteen commits. Its own ticket, and §Lessons 33: ship the urgent fix alone.

**"The Space docstring at `domain/space.py:118-124` is measured false" (draft step 1b).** Half-rejected. The enumeration it gives is accurate — every field it names really is read. Only the opening universal ("Every field is read by something as of SPACE-4") is false, because `identity_id` is a field it does not name. Fix the sentence; do not rewrite the list.