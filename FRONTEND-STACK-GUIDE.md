# Frontend Stack & Design Guide

How the VERA / HORUS GUI (`LoDISA-GUI/vera-web/frontend`) is built, and which tools to
use if you want a new GUI with the same look, feel, and structure.

---

## 1. The stack at a glance

| Layer | What this project uses | Notes |
|-------|------------------------|-------|
| UI library | **React 19** | Function components + hooks only, no classes |
| Build tool | **Vite 6** + `@vitejs/plugin-react` | `npm run dev` / `build` / `preview` |
| Language | **Plain JavaScript (JSX)** | No TypeScript — keeps iteration fast |
| Styling | **One global `styles.css` with CSS custom properties (design tokens)** | No Tailwind, no CSS-in-JS, no SASS |
| UI components | **Hand-rolled kit** in `src/components/ui/` | No MUI / Ant / shadcn / Chakra |
| Routing | **Custom page registry** (`src/app/pageRegistry.jsx`) | No react-router — pages are keyed objects with `title`, `group`, `permission` |
| State | **React Context + custom hooks** | `AuthContext`, `CellContext`, `PreferencesContext`; hooks like `useCells` |
| Data layer | **Plain `fetch` wrappers** in `src/api/*.js` + **Supabase JS** for auth | One small module per backend domain |
| Graphics / diagrams | **Hand-written SVG components** | e.g. `PalletPreviewSvg`, `SafetyCroquisSvg` — no three.js, no chart library |
| Font | **Inter**, falling back to system UI fonts | |

The key takeaway: **the distinctive design comes from the token system and the custom
UI kit, not from any framework**. There is nothing to "install" for the look — you
recreate it by copying the tokens and the component conventions.

---

## 2. Scaffolding an equivalent project

```bash
npm create vite@latest my-gui -- --template react
cd my-gui
npm install
# only if you need auth/data like this project does:
npm install @supabase/supabase-js
```

That is the entire dependency footprint of this project's frontend
(`react`, `react-dom`, `vite`, `@vitejs/plugin-react`, `@supabase/supabase-js`).

Recommended folder layout (mirrors this project):

```
src/
  main.jsx            # entry, imports styles.css
  App.jsx             # shell: auth gate + Layout + active page
  styles.css          # ALL styling: tokens + component classes
  app/pageRegistry.jsx# page metadata: title, nav group, permission
  api/                # one fetch-wrapper module per backend domain
  auth/               # AuthContext + permissions.js
  context/            # app-wide contexts (preferences, selection, ...)
  hooks/              # data-fetching hooks (useX per resource)
  components/
    ui/               # generic kit: Button, Card, Field, Stack, ...
    <feature>/        # feature-specific components
  pages/              # one file per page, listed in pageRegistry
```

---

## 3. The design system (what actually makes it look like this)

### 3.1 Design tokens — the core of the visual identity

Everything is driven by CSS custom properties declared on `:root` in `styles.css`.
Copy this block as your starting point (it is the "Slate Daylight" light theme):

```css
:root {
  color-scheme: light;

  /* surfaces */
  --bg: #f6f8fb;            /* page canvas */
  --surface: #ffffff;       /* cards, panels, inputs */
  --surface-2: #eceff4;     /* section headers, secondary fills */
  --line: #d6dce6;          /* default borders */
  --line-strong: #cdd5e0;

  /* text */
  --text: #2b3340;
  --text-body: #33404f;
  --text-muted: #5d6b7d;
  --text-faint: #8b97a8;

  /* one hero color for ALL primary interaction */
  --primary: #4a5d8a;
  --primary-hover: #3f5179;
  --primary-soft: #e9edf4;
  --focus: rgba(74, 93, 138, 0.35);
  --on-primary: #ffffff;

  /* machine-state colors — status ONLY, never decoration */
  --ok-text: #3d7256;   --ok-bg: #dde9e2;   --ok-dot: #4f9e76;
  --warn-text: #8a6420; --warn-bg: #f3e6cf; --warn-dot: #c8922f;
  --danger-text: #b23b34; --danger-bg: #f5dedc; --danger-dot: #c0564f;

  /* spacing / radius / control scale */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px; --sp-4: 16px;
  --sp-5: 20px; --sp-6: 24px; --sp-7: 32px;
  --r-control: 6px; --r-card: 10px; --r-pill: 999px;
  --ctl-h: 36px; --ctl-h-sm: 30px;
  --shadow-sm: 0 1px 2px rgba(43, 51, 64, 0.06);
  --shadow-md: 0 12px 30px rgba(43, 51, 64, 0.08);
}
```

The full palette for both themes (including the dark **HORUS** theme: amber phosphor
`#FFB000` on near-black, cyan `#36E0D0` as a rare second channel) is documented in
[`LoDISA-GUI/COLORS.md`](LoDISA-GUI/COLORS.md) — read that file before choosing any
color in a new project.

### 3.2 Design rules the project follows

1. **One hero color.** A single accent (`--primary`) drives active nav, primary
   buttons, focus rings, selected rows, and links. Nothing else gets branded color.
2. **Status colors are semantic only.** Green/amber/red exist exclusively for
   machine/system state (StatusPill, health cards). Never used decoratively.
3. **Neutral slate canvas.** Light gray-blue background, white cards, 1px hairline
   borders (`--line`) instead of heavy shadows. Shadows are subtle (`--shadow-sm`).
4. **Dark sidebar, light content.** The app shell is a CSS grid:
   `grid-template-columns: 260px 1fr;` — a near-black sidebar (`#111821`) with grouped
   nav, and a light main panel with a topbar.
5. **Compact industrial density.** Controls are 36px tall (30px small), radii are
   modest (6px controls / 10px cards), spacing steps on a 4px scale.
6. **Inter font**, `font-family: Inter, ui-sans-serif, system-ui, -apple-system,
   BlinkMacSystemFont, "Segoe UI", sans-serif;`.
7. **Theming by token swap.** The dark theme is just
   `:root[data-theme="dark"] { ...same token names, new values... }` toggled from a
   Settings page (theme / density / font-scale / reduce-motion set as `data-*`
   attributes on `<html>`). Components never know which theme is active.

### 3.3 The UI kit — small primitives, BEM-ish class names

`src/components/ui/` contains ~10 tiny components; each just composes class names
onto semantic HTML, and all real styling lives in `styles.css`:

| Component | Purpose |
|-----------|---------|
| `Button` | variants (`primary`/`secondary`/`ghost`/`danger`), sizes, `busy` spinner |
| `Card`, `Section`, `PageFrame` | surface + padding + title conventions |
| `Field` | label + input + help/error wrapper for forms |
| `Stack`, `Columns` | vertical / horizontal layout with token-based gaps |
| `StatTile` | dashboard KPI tile |
| `StatusPill` | ok / warn / danger state chip (the only place status colors appear) |
| `NavGroup` | grouped sidebar navigation |

Pattern to copy (from `Button.jsx`): props map to modifier classes like
`ui-btn ui-btn--primary ui-btn--md`, styled centrally in the stylesheet.

---

## 4. Suggested tools for the new GUI

### Recommended: replicate the same minimal stack

- **Vite + React (JS or TS)** — same scaffold as above.
- **Plain CSS with the token block from §3.1** — this is what makes it look the same.
- **Copy `src/components/ui/` wholesale** and adapt; the components have no
  project-specific dependencies.
- **Custom page registry + sidebar** if your app is a single-shell tool like this one.
- **Inter** via a local `@font-face` or system fallback.

### Reasonable additions (only if the new project needs them)

| Need | Tool | Why it stays consistent |
|------|------|------------------------|
| Real URLs / deep links | `react-router-dom` | Swap the page registry's `useState` for routes; keep the same registry metadata |
| Type safety | TypeScript (`--template react-ts`) | Zero visual impact |
| Lots of forms | `react-hook-form` | Pairs well with the existing `Field` component |
| Server data caching | `@tanstack/react-query` | Replaces hand-rolled hooks like `useCells` |
| Charts | Plain SVG (as here) or `recharts` | If using a library, feed it the CSS tokens for colors |
| Icons | `lucide-react` | Thin-stroke icons match the hairline-border aesthetic |

### Avoid (would break the look or fight the approach)

- **Tailwind / CSS-in-JS** — the design lives in one tokenized stylesheet; mixing
  systems fragments it.
- **Component frameworks (MUI, Ant Design, Bootstrap, Chakra)** — they impose their
  own density, radii, and shadows; you'd spend more time overriding than building.
- **three.js for previews** — this project renders even "3D" previews
  (`ObjectDimensionPreview3D`) with hand-computed SVG, which keeps bundles tiny and
  styling token-driven.

---

## 5. Quick-start checklist for the new project

1. `npm create vite@latest my-gui -- --template react`
2. Create `src/styles.css`, paste the token block from §3.1, import it in `main.jsx`.
3. Add the app shell: grid layout, dark sidebar with `NavGroup`, topbar, main panel.
4. Copy `components/ui/` from `LoDISA-GUI/vera-web/frontend/src/components/ui/` and
   the matching `ui-*` classes from that project's `styles.css`.
5. Create `app/pageRegistry.jsx` with your pages and nav groups.
6. Build pages out of `PageFrame > Section > Card / Field / Stack` — never raw divs
   with ad-hoc styles.
7. When you add a dark theme, do it only via `:root[data-theme="dark"]` token swaps,
   following [`LoDISA-GUI/COLORS.md`](LoDISA-GUI/COLORS.md).
