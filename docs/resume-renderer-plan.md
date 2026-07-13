# Plan: React-PDF resume renderer for ApplyPilot (Option A)

Replace the brittle text→re-parse→Chromium PDF path with a headless **React-PDF**
sidecar ported from the Resume Formatting Tool, keeping the existing Chromium HTML
renderer as an automatic fallback. Skip reference-PDF style matching.

## Goals / non-goals

- **Goal:** better-looking, consistent, always-one-page tailored resumes (and cover letters later).
- **Goal:** zero regressions — if Node/renderer is unavailable, fall back to today's output.
- **Non-goal (this pass):** reference-PDF style extraction (pdfjs + OpenAI vision).
- **Non-goal:** a UI. This is a headless batch renderer only.

## Key facts established by spike

- Node `v22.22.3`, npm `10.9.8` present.
- `@react-pdf/renderer@3.4.5` renders a valid one-page PDF from Node via
  `ReactPDF.renderToFile(el, path)` using `React.createElement` — **no JSX/TS build needed**.
- ApplyPilot's `tailor.py` already produces structured JSON from the LLM; it flattens
  it to text and `pdf.py` re-parses it. We will pass the structure straight through.

## Architecture

```
tailor.py  ──writes──▶  {prefix}.txt            (unchanged, human-readable + fallback)
                └─writes─▶ {prefix}_DATA.json    (NEW: raw LLM JSON + title)

pdf.py / resume_render.py
   1. load DATA.json (or parse .txt as fallback source) + profile
   2. build_render_request()  → normalized RenderRequest JSON
   3. render_with_node(request, out.pdf)
         ├─ node present + deps ok ─▶ node <runtime>/render.mjs req.json out.pdf ─▶ PDF ✨
         └─ any failure ───────────▶ build_html()+render_pdf() (Chromium)        ─▶ PDF
```

### Runtime install location (important)

`site-packages` may be read-only, and shipping `node_modules` in the wheel is fragile
(yoga wasm, size). So:

- The pip package ships only **source** (`*.mjs` + `package.json`) under
  `src/applypilot/resume_renderer/`.
- On first use, we materialize a **runtime dir in the writable app dir**:
  `~/.applypilot/resume_renderer_runtime/`, copy the source there (if hash changed),
  run `npm install --omit=dev` once, and invoke node from there. Cached across runs.

## The data contract (Python ↔ Node boundary)

Single `RenderRequest` object. This is the *only* coupling between the two languages;
`render.mjs` validates it and fails loudly (Python then falls back).

```jsonc
{
  "schemaVersion": 1,
  "resume": {
    "contactInfo": { "name": "", "title": "", "email": "", "phone": "",
                     "location": "", "website": "", "links": ["github…","linkedin…"] },
    "summary": "string|null",
    "skills":     [ { "category": "string|null", "value": "string" } ],  // ordered
    "experience": [ { "header": "", "subtitle": "", "location": "", "date": "", "bullets": [] } ],
    "projects":   [ { "header": "", "subtitle": "", "date": "", "bullets": [] } ],
    "education":  [ { "school": "", "degree": "", "detail": "", "date": "" } ]
  },
  "options": { "kind": "resume", "fit": "auto", "theme": "classic" }
}
```

### Mapping (ApplyPilot → RenderRequest)

| ApplyPilot source | RenderRequest field | Notes |
|---|---|---|
| `profile.personal.full_name` | `contactInfo.name` | header always from profile, never LLM |
| tailor `data.title` | `contactInfo.title` | |
| `profile.personal.email/phone/github_url/linkedin_url` | `contactInfo.*`/`links` | |
| tailor `data.summary` | `summary` | |
| tailor `data.skills` (dict) | `skills[]` (ordered category/value) | preserves order |
| tailor `data.experience[]` | `experience[]` | header/subtitle/bullets pass through |
| tailor `data.projects[]` | `projects[]` | **new section in template** |
| tailor `data.education` (string) | `education[]` (1 entry, `detail`) | normalize string→array |

Fallback source: when `_DATA.json` is absent (older runs), reuse the existing
`parse_resume`/`parse_entries` to build the same RenderRequest from `.txt`.

## Node renderer package (`src/applypilot/resume_renderer/`)

No build step — author with `React.createElement`.

| File | Responsibility |
|---|---|
| `package.json` | pinned deps: `react@18`, `@react-pdf/renderer@3.4.5`; `"private": true` |
| `render.mjs` | CLI: `node render.mjs <request.json> <out.pdf>`; validate → render → one-page loop → write; nonzero exit on error |
| `document.mjs` | `ResumeDocument` (ported from `ResumeDocument.jsx` + **Projects section**) via createElement |
| `styles.mjs` | `createDynamicStyles()` + default `classic` theme `stylingSpecs` (ported) |
| `onePage.mjs` | `computeDensityScale()` + `adjustStyling()` (ported `adjustStylingMinimally`) |
| `.gitignore` | `node_modules/` |

### One-page guarantee (stronger than the original heuristic)

1. First pass: density heuristic (`onePage.mjs`) picks an initial scale.
2. Render to **buffer**, count pages (`/Type\s*/Page\b` on the buffer).
3. If > 1 page: increase shrink, re-render. Bounded to ~4 iterations, min font floor 8pt.
4. Write final buffer to disk. Deterministic (no `Math.random`/time).

### Fonts

v1 uses built-in **Helvetica** (clean, ATS-safe, zero assets). `styles.mjs` exposes a
single hook so a registered TTF (e.g. OFL Carlito) can be dropped in later without touching
the template. No fonts shipped in v1.

## Python changes

1. **`scoring/tailor.py`** (~line 499): also `write_text` `{prefix}_DATA.json` = `json.dumps({**data, "title": ...})`.
2. **`scoring/resume_render.py`** (NEW):
   - `build_render_request(data: dict | None, parsed_text: dict | None, profile: dict) -> dict`
   - `ensure_runtime() -> Path | None` (copy source to app dir, `npm install` once, cache)
   - `render_with_node(request: dict, out: Path) -> bool`
3. **`scoring/pdf.py`**: `convert_to_pdf` prefers `_DATA.json`→node; else text→node; on node
   failure → existing `build_html`+`render_pdf`. Load `profile` for the header.
4. **`config.py`**: `RESUME_RENDERER_SRC = PACKAGE_DIR / "resume_renderer"`,
   `RESUME_RENDERER_RUNTIME = APP_DIR / "resume_renderer_runtime"`, `get_node_path()`.
5. **`cli.py doctor`**: add optional "Node.js resume renderer" readiness line.
6. **`pyproject.toml`**: add `resume_renderer/**/*` to wheel artifacts; ensure `node_modules` excluded.

## Testing / verification

- **Unit (Python):** `build_render_request` mapping is pure — table tests incl. dict-skills,
  string-education, empty projects, missing contact fields.
- **Golden render:** sample profile + tailor JSON → PDF; assert valid PDF **and exactly 1 page**;
  extract text and assert name/section titles present.
- **Long-resume:** oversized bullets still yield 1 page (exercises the refit loop).
- **Fallback:** force node absent (PATH shim) → Chromium path still writes a valid PDF.
- **Manual:** render one real tailored job and eyeball it.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Read-only site-packages / shipping node_modules | Install into writable `~/.applypilot/resume_renderer_runtime/` |
| First-run `npm install` slow / offline | Do it in `doctor`; lazy + logged; **fallback covers failure** |
| Node absent (Tier 2 users) | Automatic Chromium fallback; renderer is a soft dependency |
| react-pdf can't measure height directly | Render-to-buffer + page-count refit loop |
| Projects section not in original template | Add it (mirrors experience section) |

## Rollout order

1. Build + self-test the Node package standalone (fixtures). ← de-risks before touching Python
2. `resume_render.py` + config + tailor JSON sidecar.
3. Wire `pdf.py` with fallback.
4. Tests + doctor + pyproject.
5. Verify end-to-end on a real tailored resume.
