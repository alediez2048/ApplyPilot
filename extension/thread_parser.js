/* Reading the LinkedIn thread the operator already has open.
 *
 * `readLinkedInThread` is injected into the page by chrome.scripting.executeScript, which
 * serialises THAT FUNCTION and re-evaluates it there. So it must be self-contained — every
 * helper it uses is nested inside it, because a sibling top-level function would simply not
 * exist in the page and the call would throw at the first message.
 *
 * It lives in its own file so it can be loaded into a stub DOM and tested, which a
 * selector-driven parser has to be: when LinkedIn renames a class this returns zero messages
 * and the popup says so, but only a test catches it before the operator does. Nothing here
 * clicks, types, sends, or issues a request. It reads a page that is already on screen.
 *
 * What the live DOM actually gives (read off it on 2026-08-04, not assumed):
 *
 *   li.msg-s-message-list__event               one message
 *     time.msg-s-message-list__time-heading    "TODAY"   — a DATE boundary, present only on
 *                                              the first message of that date
 *     .msg-s-message-group__profile-link       the sender's NAME — a <span> in this build,
 *                                              carrying no href, so matching is by name
 *     time.msg-s-message-group__timestamp      "2:38 AM" — per GROUP, not per message
 *     .msg-s-event__content                    the text
 *
 * Two properties of that shape drive everything below:
 *
 *   1. Consecutive messages from one person form a GROUP, and only the first carries the name
 *      and the time. Reading per-message drops the sender on every continuation.
 *   2. There is no machine-readable timestamp anywhere. <time> carries a class and nothing
 *      else; no element in the list holds an ISO string or an epoch. The date heading and the
 *      group time are all there is, so `at` is resolved from what the operator can see.
 */

function readLinkedInThread() {
  const TEXT = el => ((el && el.innerText) || '').replace(/\s+\n/g, '\n').trim();

  /* "TODAY" + "2:38 AM" -> an ISO timestamp, or '' when it cannot be read.
   *
   * '' is a real answer. Inventing a time puts a message at a moment it demonstrably did not
   * arrive, and `at` is not decoration: the interactions row id is a hash over it. */
  const resolve = (dateLabel, timeLabel) => {
    const now = new Date();
    const d = String(dateLabel || '').trim().toUpperCase();
    const t = String(timeLabel || '').trim();

    let day;
    if (!d || d === 'TODAY') {
      day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    } else if (d === 'YESTERDAY') {
      day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    } else {
      // "AUG 3" or "AUG 3, 2025". Month NAMES only — a bare numeric date is ambiguous between
      // locales and nothing on the page says which one is in force.
      const m = d.match(/^([A-Z]{3})[A-Z]*\.?\s+(\d{1,2})(?:,\s*(\d{4}))?$/);
      if (!m) return '';
      const idx = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                   'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'].indexOf(m[1]);
      if (idx < 0) return '';
      const year = m[3] ? Number(m[3]) : now.getFullYear();
      day = new Date(year, idx, Number(m[2]));
      // LinkedIn drops the year on recent dates. Assuming the current one puts a December
      // message in the future every January, so fall back a year when that happens.
      if (!m[3] && day.getTime() > now.getTime()) day = new Date(year - 1, idx, Number(m[2]));
    }

    const hm = t.match(/^(\d{1,2}):(\d{2})\s*([AP]M)?$/i);
    if (!hm) return '';
    let hour = Number(hm[1]);
    const mins = Number(hm[2]);
    const ampm = (hm[3] || '').toUpperCase();
    if (ampm === 'PM' && hour !== 12) hour += 12;
    if (ampm === 'AM' && hour === 12) hour = 0;
    if (hour > 23 || mins > 59) return '';
    return new Date(day.getFullYear(), day.getMonth(), day.getDate(), hour, mins, 0, 0)
      .toISOString();
  };

  // Anywhere in the document, not under a fixed ancestor. LinkedIn renders the same
  // `msg-s-*` components on the full Messaging page and inside the chat OVERLAY it opens from
  // a profile or the feed, and the overlay is exactly the case a URL check gets wrong.
  const root = document.querySelector('.msg-s-message-list-content, .msg-s-message-list');
  if (!root) {
    return { ok: false,
             error: 'No open conversation found on this page. Open the thread in LinkedIn '
                    + 'Messaging (or in the chat window), then try again.' };
  }

  // The other participant, from the thread header — so the popup can name them before a single
  // message is classified. Falls back to the senders found below.
  const headerName = TEXT(document.querySelector(
    '.msg-entity-lockup__entity-title, .msg-thread__link-to-profile'));

  const events = Array.from(root.querySelectorAll('li.msg-s-message-list__event'));
  const out = [];
  let date = '';      // carried forward from the last date heading
  let sender = '';    // carried forward across a group
  let clock = '';     // carried forward across a group

  for (const li of events) {
    const heading = TEXT(li.querySelector('.msg-s-message-list__time-heading'));
    if (heading) date = heading;

    const name = TEXT(li.querySelector(
      '.msg-s-message-group__name, .msg-s-message-group__profile-link, .msg-s-event-listitem__name'));
    if (name) sender = name;

    const t = TEXT(li.querySelector('.msg-s-message-group__timestamp'));
    if (t) clock = t;

    const text = TEXT(li.querySelector('.msg-s-event-listitem__body, .msg-s-event__content'));
    if (!text) continue;   // typing indicators, loaders, seen-receipts, system rows

    out.push({
      sender, text, dateLabel: date, timeLabel: clock, at: resolve(date, clock),
      // A corroborating signal ONLY. `--other` marks the other participant in this build, but
      // direction is decided by matching the sender to the contact — a modifier class seen in
      // one version of a page is not something to key a write on.
      markedOther: li.className.indexOf('--other') >= 0
        || !!li.querySelector('[class*="--other"]'),
    });
  }

  const senders = Array.from(new Set(out.map(m => m.sender).filter(Boolean)));
  return { ok: true, url: location.href, headerName, senders, messages: out };
}

/* Direction, decided by WHO SENT IT. Runs in the POPUP, after a contact is chosen.
 *
 * The thread has two participants; whichever sender matches the contact is them, and anyone
 * else is you. That needs no selector for "me" and no knowledge of your own name, so it
 * survives the class rename that would break `--other`.
 *
 * Word containment, never substring — `"arm" in "armanino"` is four shipped bugs in this
 * codebase (§Lessons 1). The server runs the same test in domain/linkedin_thread.py, and a
 * test asserts the two agree.
 */
function linkedInDirection(senderName, contactName, markedOther) {
  const words = s => String(s || '')
    .split(/[|·•–—]|\s-\s/)[0]
    .normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .toLowerCase().match(/[a-z']{2,}/g) || [];
  const shown = new Set(words(senderName));
  const stored = words(contactName);
  if (stored.length >= 2 && shown.size) {
    return stored.every(w => shown.has(w)) ? 'linkedin_in' : 'linkedin_out';
  }
  // Nothing usable to compare against: fall back to the page's own marker rather than guess,
  // and the popup lets the operator flip any row before a single thing is written.
  return markedOther ? 'linkedin_in' : 'linkedin_out';
}

/* Whether the read button can work here, and what to say when it cannot.
 *
 * **The URL only decides whether we are on LinkedIn. It does NOT decide whether a thread is
 * open** — `readLinkedInThread` reads the DOM and knows that for certain, while a URL pattern
 * can only guess. Two reports in one afternoon came from that guess, and both were the same
 * mistake pointing different ways:
 *
 *   1. The panel was HIDDEN off a messaging URL, so it read as missing (§Lessons 43, in the
 *      file whose header quotes it).
 *   2. Then it was DISABLED on `/messaging/`-less LinkedIn URLs — and a conversation is
 *      readable from plenty of them. LinkedIn opens threads in an overlay from a profile,
 *      from search, from the feed, and the address bar never changes.
 *
 * So this answers the one question a URL can honestly answer, and the parser answers the rest:
 * on LinkedIn it is enabled, and a click either reads a conversation or says there is none.
 * Guessing earlier only let the guess be wrong.
 *
 * `url` is UNDEFINED whenever Chrome has not granted access to the tab yet: with `activeTab`
 * and no host permission, `chrome.tabs.query` returns a tab whose url is withheld. Treating
 * that as "not LinkedIn" would disable the button exactly when it would have worked, so an
 * unknown url stays ENABLED and the click reports whatever really happens.
 */
function threadPanelState(url) {
  if (url === undefined || url === null || url === '') {
    return { enabled: true, hint: '' };
  }
  if (/^https:\/\/([a-z]+\.)?linkedin\.com(\/|$)/i.test(url)) {
    return { enabled: true, hint: '' };
  }
  return { enabled: false, hint: 'Open a LinkedIn conversation in this tab to read it.' };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { readLinkedInThread, linkedInDirection, threadPanelState };
}
