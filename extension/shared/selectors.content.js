// ApplyPilot LinkedIn Assistant — CLASSIC content-script selector table.
//
// WHY A .js (not the selectors.json fetch): a content script fetching an extension resource
// can be blocked by LinkedIn's CSP. Injecting the table as a classic script (before content.js)
// sidesteps CSP entirely. This is the version the extension actually consumes.
//
// A LinkedIn UI change is a config edit HERE: bump `version` and adjust a target's ranked
// Strategy[] (the content script logs the active version on load). Resolver rules: try
// strategies in order, first VISIBLE + ENABLED match wins. `by`: aria-label -> text -> role ->
// structural (best/most-stable first). `match`: 'exact'|'contains' (aria-label/text only).
// `scope`: 'document' | 'actionBar' (profile top-card CTAs) | 'dialog' (open [role=dialog]).
// (extension/selectors.json is kept as human-readable documentation of the same table.)

globalThis.__APPLYPILOT_SELECTORS__ = {
  version: 1,
  targets: {
    connectButton: [
      { by: "aria-label", value: "to connect", match: "contains", scope: "actionBar" },
      { by: "aria-label", value: "Connect with", match: "contains", scope: "actionBar" },
      { by: "text", value: "Connect", match: "exact", scope: "actionBar" },
      { by: "structural", value: 'button[aria-label*="to connect" i]', scope: "actionBar" },
      { by: "structural", value: '.pvs-profile-actions button[aria-label*="connect" i]', scope: "document" },
      { by: "structural", value: '.pv-top-card-v2-ctas button[aria-label*="connect" i]', scope: "document" },
    ],
    moreButton: [
      { by: "aria-label", value: "More actions", match: "contains", scope: "actionBar" },
      { by: "aria-label", value: "More", match: "exact", scope: "actionBar" },
      { by: "text", value: "More", match: "exact", scope: "actionBar" },
      { by: "structural", value: '.pvs-profile-actions button[aria-label*="More actions" i]', scope: "document" },
      { by: "structural", value: '.pv-top-card-v2-ctas button[aria-label*="More" i]', scope: "document" },
      { by: "structural", value: ".pvs-profile-actions__overflow-toggle", scope: "document" },
    ],
    connectMenuItem: [
      { by: "aria-label", value: "to connect", match: "contains", scope: "document" },
      { by: "aria-label", value: "Connect with", match: "contains", scope: "document" },
      { by: "text", value: "Connect", match: "exact", scope: "document" },
      { by: "structural", value: '.artdeco-dropdown__content-inner [aria-label*="to connect" i]', scope: "document" },
      { by: "structural", value: '.artdeco-dropdown__content [role="button"][aria-label*="connect" i]', scope: "document" },
      { by: "structural", value: 'div[role="menu"] [aria-label*="connect" i]', scope: "document" },
    ],
    addNoteButton: [
      { by: "aria-label", value: "Add a note", match: "contains", scope: "dialog" },
      { by: "text", value: "Add a note", match: "exact", scope: "dialog" },
      { by: "structural", value: '[role="dialog"] button[aria-label*="Add a note" i]', scope: "dialog" },
      { by: "structural", value: '.artdeco-modal button[aria-label*="note" i]', scope: "dialog" },
    ],
    noteTextarea: [
      { by: "structural", value: "textarea#custom-message", scope: "dialog" },
      { by: "structural", value: '[role="dialog"] textarea[name="message"]', scope: "dialog" },
      { by: "structural", value: '[role="dialog"] textarea[id*="message" i]', scope: "dialog" },
      { by: "structural", value: '[role="dialog"] textarea', scope: "dialog" },
      { by: "structural", value: '[role="dialog"] input[name="message"]', scope: "dialog" },
    ],
    sendButton: [
      { by: "aria-label", value: "Send invitation", match: "contains", scope: "dialog" },
      { by: "text", value: "Send", match: "exact", scope: "dialog" },
      { by: "structural", value: '[role="dialog"] button[aria-label*="Send invitation" i]', scope: "dialog" },
      { by: "structural", value: '.send-invite__actions button[aria-label*="Send" i]', scope: "dialog" },
      { by: "structural", value: '[role="dialog"] button.artdeco-button--primary', scope: "dialog" },
    ],
    pendingBadge: [
      { by: "aria-label", value: "Pending", match: "contains", scope: "actionBar" },
      { by: "text", value: "Pending", match: "exact", scope: "actionBar" },
      { by: "structural", value: '.pvs-profile-actions [aria-label^="Pending" i]', scope: "document" },
      { by: "structural", value: '.pv-top-card-v2-ctas button[aria-label*="Pending" i]', scope: "document" },
    ],
    dismissModal: [
      { by: "aria-label", value: "Dismiss", match: "exact", scope: "dialog" },
      { by: "aria-label", value: "Dismiss", match: "contains", scope: "document" },
      { by: "aria-label", value: "Close", match: "contains", scope: "document" },
      { by: "structural", value: '[role="dialog"] button.artdeco-modal__dismiss', scope: "dialog" },
      { by: "structural", value: 'button[aria-label*="Dismiss" i]', scope: "document" },
    ],
  },
};
