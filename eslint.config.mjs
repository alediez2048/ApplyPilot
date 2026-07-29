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
];
