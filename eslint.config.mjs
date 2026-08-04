// ARCH-2: lint the dashboard frontend, which lived in a Python string until now.
//
// The script is a CLASSIC script, not a module, and that is deliberate: ~56 inline
// `onclick=` attributes in index.html (and in HTML the script itself generates) call
// these functions by name off the global object. Making it a module would break every
// one of them silently.
//
// Dev-only. Nothing in the runtime path shells out to node for the dashboard.
import js from "@eslint/js";
import globals from "globals";

export default [
  {
    files: ["src/applypilot/static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser },
    },
    rules: {
      ...js.configs.recommended.rules,
      // Top-level declarations ARE the public API here — index.html calls them by name.
      // Genuine dead code is caught by tests/test_dashboard_static.py, which reads the
      // HTML and can tell "unreferenced" from "referenced only by an attribute".
      "no-unused-vars": ["error", { vars: "local", args: "none" }],
      eqeqeq: ["warn", "smart"],
      "no-var": "error",
      "prefer-const": "warn",
    },
  },
  {
    // The extension was popup-only markup and went unlinted for that reason. It now reads a
    // page and writes to the local API, and a ReferenceError in a popup is exactly as silent
    // as one in the dashboard (§Lessons 7 — it blanks the view and nothing reports it).
    //
    // `thread_parser.js` and `popup.js` are two classic scripts sharing one global scope, so
    // each references names the other declares: `no-undef` is off here rather than papered
    // over with per-file globals that would drift the moment a function is renamed. What
    // resolves at runtime is proven by tests/test_linkedin_thread.py, which runs the parser.
    files: ["extension/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...globals.webextensions, module: "readonly" },
    },
    rules: {
      ...js.configs.recommended.rules,
      "no-undef": "off",
      // `catch (_e)` is the codebase's deliberate "this failure is handled by the fallback
      // below" marker, and there are four of them in the popup alone.
      "no-unused-vars": ["error", { vars: "local", args: "none", caughtErrors: "none" }],
      eqeqeq: ["warn", "smart"],
      "no-var": "error",
    },
  },
];
