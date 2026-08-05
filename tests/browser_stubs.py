"""The browser globals `dashboard.js` may rely on, in ONE place.

Six test files evaluate the served script under their own hand-written DOM stubs. They were six
copies of the same object literal, and SPACE-2 found out what that costs: the Space nav reads
`location.search` at module load, five of the six stubs defined `location` only as a property of
`window`, and **47 tests failed at once** with `ReferenceError: location is not defined`.

The failure was correct — a ReferenceError at the top of the file blanks the entire dashboard
exactly as a syntax error does (§Lessons 7) — but it was six edits to fix, and the seventh copy
would have been written before anyone noticed. `location` and `history` are GLOBALS in a
browser, not only properties of `window`, and a stub that is quietly less of a browser than a
browser is will keep producing this.

Each file still owns its own element stub, because those differ by what the file renders. This
holds only the part that is the same everywhere and has no reason to differ.
"""

from __future__ import annotations

#: Everything a browser provides that is neither the DOM nor an element.
BROWSER_GLOBALS = """
globalThis.location = { href:'http://localhost:8765/', search:'' };
globalThis.history = { replaceState(){}, pushState(){} };
globalThis.window = { open(){}, location: globalThis.location, history: globalThis.history };
Object.defineProperty(globalThis, "navigator",
  { value:{ clipboard:{ writeText(){} } }, configurable:true });
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = async () => ({ json: async () => ({}) });
globalThis.alert = () => {};
globalThis.confirm = () => true;
"""
