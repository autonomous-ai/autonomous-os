# Plan: Make `/edit` (EditConfig) visually consistent with Setup V2

## Context

Setup.tsx was recently reworked into "Version 2" (one-step wizard) with a polished
amber look: sidebar nav items with an animated amber accent rail + hover lift
(`.lm-nav-item`), pill-shaped mobile tabs (`.lm-tab`), and amber primary buttons
with hover glow/lift (`.lm-btn` / `.lm-btn-primary`). These styles live in
`index.css` but are **scoped to `.lm-setup`**, so they don't apply to the `/edit`
page (`lm-root lm-edit`).

`/edit` (EditConfig.tsx) is a **multi-tab settings manager** — a different UX from
the onboarding wizard, and it should stay that way. But its chrome (sidebar nav,
mobile tabs, Save button, topbar title) is hand-rolled with **inline styles** that
predate the V2 styling, so it looks visually out of step with Setup: flatter nav
(no accent rail/hover), square tabs, a plain Save button (no glow), smaller title.

**Goal:** visual consistency only. Make `/edit`'s chrome match Setup V2's
look&feel by reusing the SAME CSS classes — without turning EditConfig into a
wizard and without duplicating CSS. Card headers (`SectionCard` / `.lm-card`) and
eye toggles (`.lm-eye-btn`) already match, so no work needed there.

## Approach

**Promote the shared chrome classes from `.lm-setup` scope to the neutral
`.lm-root` scope** (both pages already wrap in `.lm-root`, which is also where
`.lm-eye-btn` and the `.lm-u-*` utilities already live). Then convert EditConfig's
inline-styled nav/tabs/Save button to those same classes.

Chosen over the alternatives because it's the DRY-est: no duplicated rules (the
project already hit a `.lm-btn` naming collision once — avoid re-introducing two
copies that can drift), and fewer selectors than a comma-list. Setup keeps
rendering identically because it's still inside `.lm-root`; the Setup-only pieces
(progress bar, brand header, "done" checkmarks) are separate elements/modifiers
that EditConfig simply won't render, so nothing leaks.

## Changes

### 1. `index.css` — widen scope `.lm-setup ` → `.lm-root ` on chrome classes only

Mechanical prefix change on these selectors (~16 one-token edits):
- **Nav item** (~lines 444–480): `.lm-nav-item`, `::before`, `:hover`,
  `--done`, `--active`, `--active::before`
- **Buttons** (~lines 507–529): `.lm-btn`, `:hover`, `:active`,
  `.lm-btn-primary` (+`:hover`, `:disabled`), `.lm-btn-ghost` (+`:hover`)
- **Tabs** (~lines 555–572): `.lm-tab`, `.lm-tab--active`

**Leave scoped to `.lm-setup` (Setup-only, do NOT touch):** `.lm-progress-*`,
`.lm-card`, `.lm-mobile-tabs-wrap` edge-fade, the placeholder opacity rule, and the
`.lm-setup` `@media` block. `.lm-eye-btn` is already `.lm-root` — no change.

### 2. `EditConfig.tsx` — inline styles → classNames

- **Sidebar nav button** (~lines 462–473): replace the whole inline `style` with
  `className={` + "`lm-nav-item${active ? \" lm-nav-item--active\" : \"\"}`" + `}`.
  Drop the layout/color inline props (class supplies them). Do NOT add `--done`
  (Edit has no completion concept). Gains the amber accent rail + hover lift.
- **Mobile tab button** (~lines 509–514): replace inline `style` with
  `className={` + "`lm-tab${active ? \" lm-tab--active\" : \"\"}`" + `}`. Converts
  square tabs to Setup's pill tabs.
- **Save button** (~lines 536–552): add `className="lm-btn lm-btn-primary"`; remove
  the background/color/disabled/opacity ternaries from inline style (the class's
  `:disabled` rule reproduces the disabled look). Keep Edit's smaller size as inline
  overrides only: `style={{ padding: "6px 18px", fontSize: 12, borderRadius: 7 }}`
  (inline wins over the class's 14px). Keep the existing
  `disabled={saving || loadingCfg || !dirty}`.
- **Topbar title** (~line 532): `fontSize: 13` → `fontSize: 15` to match Setup's
  topbar title (Setup uses 15/600).

**Do not touch:** `.lm-sidebar` width/bg (Setup keeps these inline too — already
consistent), `← Monitor` link, theme toggle, error banner, SectionCard usages,
EditConfig's own mobile `@media` `<style>` block (independent of Setup's).

## Files
- `os/services/web/src/index.css` — scope widening (chrome classes only)
- `os/services/web/src/pages/EditConfig.tsx` — 3 inline→className swaps + 1 font size
- `os/services/web/src/pages/Setup.tsx` — reference only (confirm no regression)

## Verification
1. `cd os/services/web && ./node_modules/.bin/tsc -b` → expect TSC=0.
2. `./node_modules/.bin/vite build` → expect clean build.
3. Run `make web-dev`, open `/edit` and `/setup` side by side:
   - `/edit` sidebar nav now shows the amber accent rail + hover lift, matching Setup.
   - `/edit` Save button is amber with hover glow/lift; disabled state (when not
     dirty) shows the muted surface look.
   - Mobile (≤640px): `/edit` tabs are pills matching Setup; no layout regression.
   - `/setup` is visually unchanged (no regression) in both V1 and V2 (toggle
     `SETUP_VERSION`).
4. Hard refresh (Cmd+Shift+R) to bypass any stale `dist/` cache.
