"""Networking orchestrator: job → contacts.

find_contacts_for_job derives the employer/domain, searches Apollo (masked), ranks,
reveals contact info for the selected few, and persists them. LinkedIn fallback (NET-5)
is a no-op here (use_linkedin is accepted but not yet wired).
"""

from __future__ import annotations

import logging

from applypilot.domain import linkedin_thread as _lt
from applypilot.networking import derive, providers, rank, store, verify

log = logging.getLogger(__name__)

# How many batches of `per_job` people we are willing to enrich before giving up. Verification
# runs after enrichment, so a whole batch can be rejected; without a top-up an ambiguous
# employer name returns nobody while real colleagues sit unexamined further down the pool.
# 3 is a credits/coverage tradeoff: per_job=5 enriches at most 15 of the ~25 candidates.
_TOPUP_ROUNDS = 3


def _draft_and_store(profile: dict, job: dict, contact: dict, warm: bool = False) -> None:
    """Best-effort outreach draft for one contact; failures are non-fatal.

    warm=True → the hot layer (existing connection): warmer email + a DM to a known connection.
    """
    from applypilot.networking import outreach
    try:
        # What this employer has ALREADY been sent, so the next one differs. Passed at BOTH
        # call sites — a rule implemented at one of its two is not implemented (§Lessons 49),
        # and this is the batch path, where a whole company gets drafted minutes apart.
        previous = store.copy_already_sent_to_company(
            contact.get("company") or job.get("company") or job.get("site") or "",
            exclude_id=contact.get("id"))
        draft = outreach.draft_email(profile, job, contact, warm=warm, previous=previous)
        store.upsert_contact({
            "id": contact.get("id"),
            "job_url": contact["job_url"],
            "linkedin_url": contact.get("linkedin_url"),
            "full_name": contact.get("full_name"),
            "outreach_subject": draft["subject"],
            "outreach_message": draft["body"],
            "linkedin_message": draft.get("linkedin_note", ""),
            "outreach_status": "drafted",
            "outreach_channel": "email",
            "draft_variant": draft.get("variant", ""),
        })
    except Exception as e:  # noqa: BLE001
        log.debug("Outreach draft failed for %s: %s", contact.get("full_name"), e)


def draft_for_contact(contact_id: str, style: str = "") -> dict | None:
    """Regenerate the outreach draft for a stored contact. Returns the new draft or None.

    `style` is an optional free-text tone directive passed through to outreach.draft_email.
    """
    from applypilot.config import load_profile
    from applypilot.database import get_connection
    from applypilot.networking import outreach

    conn = get_connection()
    store.init_contacts(conn)
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return None
    contact = dict(zip(row.keys(), row))
    jrow = conn.execute(
        "SELECT url, title, company, site, full_description FROM jobs WHERE url = ?",
        (contact["job_url"],),
    ).fetchone()
    job = dict(zip(jrow.keys(), jrow)) if jrow else {"title": contact.get("title")}
    try:
        profile = load_profile()
    except Exception:  # noqa: BLE001
        profile = {}
    try:
        previous = store.copy_already_sent_to_company(
            contact.get("company") or job.get("company") or job.get("site") or "",
            exclude_id=contact_id, conn=conn)
        draft = outreach.draft_email(profile, job, contact, style=style, previous=previous)
    except Exception as e:  # noqa: BLE001
        log.warning("Regenerate draft failed for %s: %s", contact_id, e)
        return None
    store.upsert_contact({
        "id": contact_id, "job_url": contact["job_url"],
        "linkedin_url": contact.get("linkedin_url"), "full_name": contact.get("full_name"),
        "outreach_subject": draft["subject"], "outreach_message": draft["body"],
        "linkedin_message": draft.get("linkedin_note", ""),
        "outreach_status": "drafted", "outreach_channel": "email",
        "draft_variant": draft.get("variant", ""),
    })
    return draft


def _augment_with_linkedin(selected: list[dict], company: str | None,
                           role: str | None, per_job: int, result: dict) -> list[dict]:
    """Fill the gap with LinkedIn-found people (read-only), Apollo-enriched by URL."""
    from applypilot.networking import linkedin_agent
    need = per_job - len(selected)
    people = linkedin_agent.find_people(company or "", role, n=need)
    if not people:
        return selected
    have_urls = {(c.get("linkedin_url") or "").lower() for c in selected}
    added = 0
    for p in people:
        url = (p.get("linkedin_url") or "").lower()
        if not url or url in have_urls:
            continue
        p = dict(p)
        p["key"] = url  # linkedin_url as the dedupe/enrich key
        p["match_reason"] = "same team"
        p["source"] = "linkedin"
        p.setdefault("company", company)
        selected.append(p)
        have_urls.add(url)
        added += 1
        if len(selected) >= per_job:
            break
    if added:
        result["note"] = f"{added} via LinkedIn fallback"
    return selected


def find_contacts_for_job(
    job: dict,
    per_job: int = 5,
    use_linkedin: bool = False,
    dry_run: bool = False,
    draft: bool = True,
    skip_known: bool = False,
) -> dict:
    """Find + persist up to `per_job` contacts for a job.

    Args:
        job: job row dict (needs url; ideally title, company, application_url, full_description).
        per_job: how many contacts to find/reveal.
        use_linkedin: reserved for NET-5 (fallback); no-op in NET-1.
        dry_run: search + rank only — no reveal (no Apollo credits), no persistence of email.
        skip_known: drop anyone already stored for this job BEFORE selection, so a second round
            reaches deeper into the ranked pool instead of re-picking the same five. Without it
            a re-run is a no-op that spends credits: `select()` scores title relevance and is
            deterministic, so it returns the same top N, and `upsert_contact` then overwrites
            the rows you already had. This is the "nobody replied, find me new people" path.

    Returns:
        {"company": str|None, "found": int, "revealed": int, "contacts": [dict], "note": str}
    """
    job_url = job.get("url")
    role = job.get("title")
    company = derive.derive_company(job)
    # An ATS tenant slug is not the employer's name. `ouryahoo.wd5.myworkdayjobs.com` stored the
    # company as "Ouryahoo" and Apollo truthfully reported "0 found" — there is no such company.
    # The posting's own text is the corroboration, so this cannot invent a name that the job
    # description does not say.
    refined = derive.refine_company_from_posting(company, job.get("full_description"))
    if refined:
        company = refined
    domain = derive.derive_domain(job, company)
    # WHERE the domain came from decides how much it may be trusted later. A domain read off the
    # careers-site host is an inference: avathongov.com hosts Avathon Government's postings, but
    # its people email from @sparkcognition.com (the company was SparkCognition before the
    # rename). Rejecting real employees for contradicting a GUESS is how that job found nobody.
    # A domain Apollo corroborated is evidence and keeps its full weight.
    domain_source = "url" if domain else ""

    from applypilot.networking import connections
    conns_at_company = connections.count_at_company(company)
    result = {"company": company, "found": 0, "revealed": 0, "contacts": [],
              "connections_at_company": conns_at_company, "note": ""}

    def _log(detail: str, status: str = "ok") -> None:
        """Record the outcome on the job. EVERY exit from this function logs.

        A search that finds nobody used to log nothing at all, which made a completed run
        indistinguishable from a button that never fired — see tests/test_networking_silent_zero.
        """
        if dry_run:
            return
        from applypilot.database import log_event
        log_event(job_url, "outreach", status, detail)

    if not company and not domain:
        result["note"] = "could not determine employer/domain"
        _log("Could not find contacts: the employer name and domain could not be derived "
             "from this job's URL.", "error")
        return result

    # An ATS-hosted posting carries no employer domain (ats.rippling.com is the vendor's), and
    # without one Apollo falls back to a fuzzy NAME search. For a common word that finds the
    # wrong company entirely: "Wander" returned four unrelated Wanders, every candidate came
    # from "Wander AG", verification correctly dropped all 15 — and the real employer's CEO and
    # CMO sat at wander.com untouched. Recover the domain first; the guess is only accepted if
    # Apollo's own people there report a matching employer name.
    if not domain and company:
        slug = derive.employer_slug_from_url(job.get("url") or job.get("application_url"))
        domain = providers.confirm_employer_domain(company, slug) or None
        if domain:
            result["employer_domain"] = domain
            domain_source = "apollo"      # corroborated: people there report this employer

    # Colleagues and recruiters are searched SEPARATELY. One blended query cannot produce a mix
    # because the provider decides the composition — measured at 25 recruiters and 0 peers on a
    # real job, after which the ranking stage was choosing five recruiters out of five.
    # Defaults come from the settings registry, never repeated here: `OUTREACH_ATTACH_DECK`
    # defaulted to "1" at the call site while settings.py declared False, so `doctor --config`
    # reported the feature off while 3.1 MB rode along on 34 real emails. A default in two
    # places is two defaults.
    from applypilot import settings as _settings
    _cfg, _ = _settings.resolve()
    min_peers = _cfg["OUTREACH_MIN_PEERS"]
    min_recruiters = _cfg["OUTREACH_MIN_RECRUITERS"]

    pools = providers.search_mix(company, domain, role, per_page=25)
    candidates = pools["peers"] + pools["recruiters"]
    if not candidates:
        # "coverage or plan/key" named three unrelated problems at once and pointed at none of
        # them. It cost a wrong diagnosis on the Yahoo job, where the real answer was that the
        # employer name was a Workday tenant slug ("Ouryahoo") that no provider has ever heard
        # of — and where a missing API key would have printed exactly the same sentence.
        # §Lessons 15: a zero result has to say WHICH zero it is.
        name = providers.active() or "the provider"
        reachable, why = providers.probe()
        if not reachable:
            result["note"] = f"{name} is not usable: {why}"
            _log(f"Could not search for contacts: {name} is not usable ({why}). "
                 f"No credits were spent.", "error")
        elif not providers.company_known(company, domain):
            result["note"] = f"{name} has no company called {company!r}"
            _log(f"No contacts found: {name} has no company named {company!r}. If that is not "
                 f"how the employer is actually known, correct the company on the Job tab — an "
                 f"ATS tenant slug is often not the employer's name.", "warn")
        else:
            result["note"] = f"{name} knows {company!r} but returned nobody matching this role"
            _log(f"No contacts found at {company} — {name} knows the company but returned "
                 f"nobody for these titles. Try “find contacts” again for a wider search.", "warn")
        return result

    # Rank the WHOLE pool, then work down it in batches. Ranking scores title relevance and
    # knows nothing about which employer a person actually works for, while the strongest
    # verification signal (the work-email domain) only exists after enrichment. So a batch can
    # come back 100% rejected while genuine colleagues sit further down the same pool.
    #
    # That is exactly what "find contacts is not working" was on a Zello job: Apollo lists
    # THREE orgs named Zello/ZELLO, none with a primary_domain to tell them apart. The five
    # best-titled people were all from the wrong one, were all correctly dropped, and the two
    # real @zello.com recruiters were never looked at — they were candidates 6..25. Stopping
    # after batch one turned an ambiguous employer into a silent zero.
    # Interleaved, not scored into one list: the loop below enriches down this order and drops
    # whoever fails verification, so the two sides must stay interwoven the whole way. Putting
    # four peers at the front means a company whose first four peers all fail verification ends
    # up all-recruiter again — the original bug, one step later.
    ranked = rank.select_mix(pools["peers"], pools["recruiters"], role,
                             min_peers=min_peers, min_recruiters=min_recruiters)

    # Round two. Excluded by the SAME identity function that stores them — computing a fresh
    # name/email match here would be a second answer to "is this the same person", and the two
    # would disagree (§Lessons 1 is a whole family of exactly that).
    already = 0
    if skip_known and job_url:
        known = {c["id"] for c in store.get_contacts_for_job(job_url)}
        before = len(ranked)
        ranked = [c for c in ranked
                  if store.contact_id(job_url, c.get("linkedin_url"), c.get("full_name"))
                  not in known]
        already = before - len(ranked)
        log.info("Second round: %d of %d candidates already known, %d left",
                 already, before, len(ranked))
        if not ranked:
            # Loud, not silent. A search that spent credits and found nobody NEW must not look
            # identical to a button that never fired (§Lessons 15).
            note = (f"No new people at {company or 'this company'} — all "
                    f"{already} candidate(s) the provider returned are already on this job.")
            from applypilot.database import log_event
            log_event(job_url, "network", "warn", note)
            return {"company": company, "found": 0, "revealed": 0, "contacts": [],
                    "note": note}

    # LinkedIn fallback (opt-in): when the provider under-covers this company, read
    # the company People page and merge the found profiles.
    if use_linkedin and len(ranked) < per_job:
        ranked = _augment_with_linkedin(ranked, company, role, per_job, result)

    # Enrichment costs Apollo credits, so topping up is bounded rather than "walk all 25".
    max_enriched = min(len(ranked), max(per_job, per_job * _TOPUP_ROUNDS))

    _profile_cache: dict = {}

    def _profile_for_drafting() -> dict:
        if "p" not in _profile_cache:
            from applypilot.config import load_profile
            try:
                _profile_cache["p"] = load_profile()
            except Exception:  # noqa: BLE001
                _profile_cache["p"] = {}
        return _profile_cache["p"]

    stored_contacts: list[dict] = []
    rejected: list[str] = []
    considered: list[dict] = []
    cursor = 0

    while len(stored_contacts) < per_job and cursor < max_enriched:
        batch = ranked[cursor:cursor + (per_job - len(stored_contacts))]
        if not batch:
            break
        cursor += len(batch)
        considered.extend(batch)

        # Reveal contact info for this batch only (Apollo bulk enrichment; consumes credits).
        revealed: dict[str, dict] = {}
        if not dry_run:
            revealed = providers.enrich(batch)
            result["revealed"] += sum(1 for r in revealed.values() if r.get("email"))

        for c in batch:
            rev = revealed.get(c.get("key"), {})
            contact = {
                "job_url": job_url,
                # Enrichment knows the surname that search redacted. `better_name` takes it only
                # when it is a strict EXTENSION of what we have — a looser rule lets a
                # mismatched enrichment row rename a contact into a different person, which is
                # worse than a missing surname: a wrong first name shows up in the greeting and
                # a wrong full name does not.
                "full_name": _lt.better_name(c.get("full_name") or "", rev.get("full_name") or ""),
                "title": c.get("title"),
                "company": company or c.get("company"),
                "linkedin_url": rev.get("linkedin_url") or c.get("linkedin_url"),
                "email": rev.get("email"),
                "email_status": rev.get("email_status", "none"),
                "location": c.get("location"),
                "seniority": c.get("seniority"),
                "match_reason": c.get("match_reason"),
                "source": c.get("source") or providers.active() or "apollo",
                "apollo_id": c.get("apollo_id"),
            }
            # Self-check before this reaches the dashboard. Catches the contacts an org-name
            # filter alone misses — Apollo returns people with no email whose employer is
            # plainly someone else (a freelance resume writer on a "Writer" job).
            v = verify.verify_contact({**contact, "company": c.get("company"),
                                       "from_domain_search": c.get("from_domain_search"),
                                       "domain_source": domain_source},
                                      company, c.get("employer_domain") or "")
            if v["verdict"] == verify.REJECT:
                log.info("Dropping %s — %s", contact.get("full_name"), "; ".join(v["reasons"]))
                rejected.append(contact.get("full_name") or "?")
                continue
            contact["confidence"] = v["confidence"]
            contact["verify_note"] = "; ".join(v["reasons"])
            if not dry_run:
                cid = store.upsert_contact(contact)
                contact["id"] = cid
                # Draft outreach for anyone reachable — an EMAIL or a LINKEDIN profile. A
                # no-email contact still has a LinkedIn note (Copy note + open LinkedIn); only
                # truly unreachable contacts (no email AND no LinkedIn) are skipped.
                if draft and (contact.get("email") or contact.get("linkedin_url")):
                    _draft_and_store(_profile_for_drafting(), job, contact)
            stored_contacts.append(contact)

        # A dry run reveals nothing, so verification has no email domain to judge and every
        # further batch would be decided on identical evidence. One pass is all it can learn.
        if dry_run:
            break

    selected = considered
    result["found"] = len(considered)

    # ── HOT layer: your existing 1st-degree connections at this company (the warm approach). ──
    # Cold (Apollo, above) = strangers. Hot = people you already know there. Enrich their email
    # via Apollo (name+company+LinkedIn → email) and draft WARM outreach (reconnect email + a DM).
    if not dry_run:
        try:
            hot = _find_hot_contacts(job, company, selected, per_job=per_job,
                                     profile_fn=_profile_for_drafting, draft=draft)
            stored_contacts = hot + stored_contacts  # warm contacts first
            result["hot"] = len(hot)
        except Exception as e:  # noqa: BLE001
            log.debug("Hot (connections) layer failed: %s", e)
        # Self-heal rows a previous (buggier) company match attached to this job.
        try:
            pruned = _prune_stale_connection_contacts(job_url, company)
            if pruned:
                result["pruned"] = pruned
                stored_contacts = [c for c in stored_contacts
                                   if (c.get("full_name") or "") not in set(pruned)]
                log.info("Dropped %d stale connection contact(s) at %s: %s",
                         len(pruned), company, ", ".join(pruned))
                from applypilot.database import log_event
                log_event(job_url, "outreach", "info",
                          f"Removed {len(pruned)} contact(s) who no longer match "
                          f"{company}: {', '.join(pruned)}.")
        except Exception as e:  # noqa: BLE001
            log.debug("Stale-connection prune failed: %s", e)

    result["contacts"] = stored_contacts
    result["note"] = "dry-run (no reveal)" if dry_run else "ok"
    log.info("Networking: %s → %d cold + %d hot contacts (%d with email)%s",
             company, result["found"], result.get("hot", 0), result["revealed"],
             " [dry-run]" if dry_run else "")
    if rejected:
        result["rejected"] = rejected
        result["note"] = (result["note"] + "; " if result["note"] else "") + \
            f"dropped {len(rejected)} who work elsewhere"
        log.info("Verification dropped %d contact(s) at %s: %s",
                 len(rejected), company, ", ".join(rejected))
    hot_n = result.get("hot", 0)
    dropped = f" Dropped {len(rejected)} who work elsewhere." if rejected else ""
    if stored_contacts:
        warm = f", {hot_n} you already know" if hot_n else ""
        _log(f"Found {len(stored_contacts)} contact(s) at {company or 'the employer'} — "
             f"{result['revealed']} with a verified email{warm}.{dropped}")
    else:
        # Nobody survived. This is the case that used to be silent, and it is the one the
        # operator most needs explained: the search DID run and DID spend credits. Naming the
        # people who were dropped is what makes an ambiguous employer diagnosable — Apollo
        # lists three orgs called "Zello", none with a domain to disambiguate them.
        who = f" ({', '.join(rejected[:4])})" if rejected else ""
        _log(f"No contacts kept at {company or 'the employer'} — considered "
             f"{result['found']} and dropped {len(rejected)} who work elsewhere{who}. "
             f"The employer name may match more than one company.", "warn")
    return result


def _prune_stale_connection_contacts(job_url: str, company: str | None) -> list[str]:
    """Drop stored hot-layer contacts that no longer match a connection at `company`.

    Contact discovery only ever upserts, so a row written by a buggy matcher survives the
    fix forever — a substring bug once attached Armanino and State Farm people to an "Arm"
    job, and they stayed after the matcher was corrected. Re-running discovery now
    self-heals instead of needing a manual DELETE.

    Deliberately conservative. A row is kept if ANY of these hold, because the cost of
    deleting something real is far higher than leaving a stale row visible:
      - it was emailed or a LinkedIn invite went out (there is history on it)
      - you typed a phone number or notes on it (you invested in it by hand)
      - we cannot tell who the employer is (no company -> no basis to judge)
    """
    from applypilot.networking import connections, store
    if not company or not job_url:
        return []
    removed = []
    for c in store.get_contacts_for_job(job_url):
        if (c.get("source") or "") != "connection":
            continue
        if (c.get("sent_message_id") or "").strip() or (c.get("dm_status") or "") in ("sent", "manual"):
            continue
        if (c.get("phone") or "").strip() or (c.get("notes") or "").strip():
            continue
        rec = connections.match(c.get("full_name"), company)
        if rec and rec.get("company_match"):
            continue
        if store.delete_contact(c["id"]):
            removed.append(c.get("full_name") or c["id"])
    return removed


def _find_hot_contacts(job: dict, company: str | None, cold_selected: list[dict],
                       per_job: int, profile_fn, draft: bool) -> list[dict]:
    """Surface + enrich + draft outreach for your existing connections at `company`.

    Skips anyone already covered by the cold Apollo layer (dedupe by normalized name). Enriches
    email via Apollo identity match (name/company/LinkedIn), stores as source='connection', and
    drafts WARM outreach (reconnect email + a DM to a known connection).
    """
    from applypilot.networking import apollo, connections
    job_url = job.get("url")
    conns = connections.at_company(company, limit=per_job)
    if not conns:
        return []

    # Don't double-list someone the cold layer already found.
    cold_names = {(c.get("full_name") or "").strip().lower() for c in cold_selected}
    conns = [c for c in conns if (c.get("full_name") or "").strip().lower() not in cold_names][:per_job]
    if not conns:
        return []

    # Enrich emails via Apollo (name + company + LinkedIn URL → verified email). One credit each.
    people = [{"key": c.get("url") or c.get("full_name"), "full_name": c.get("full_name"),
               "company": company or c.get("company"), "linkedin_url": c.get("url")} for c in conns]
    enriched = apollo.match_by_identity(people)

    out = []
    for c, p in zip(conns, people):
        rev = enriched.get(p["key"], {})
        contact = {
            "job_url": job_url,
            "full_name": c.get("full_name"),
            "title": c.get("position"),
            "company": company or c.get("company"),
            "linkedin_url": rev.get("linkedin_url") or c.get("url"),
            "email": rev.get("email"),
            "email_status": rev.get("email_status", "none"),
            "match_reason": "🤝 connection — you already know them",
            "source": "connection",  # marks the HOT layer
            "apollo_id": rev.get("apollo_id"),
        }
        cid = store.upsert_contact(contact)
        contact["id"] = cid
        if draft:  # warm draft even without an email (the DM path works for connections)
            _draft_and_store(profile_fn(), job, contact, warm=True)
        out.append(contact)
    return out
