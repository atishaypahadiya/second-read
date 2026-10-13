# Merge notes — applying the design system

Branch `design-system` adds only new files: `DESIGN.md`, `extension/ui/tokens.css`,
`extension/ui/components.css`, `extension/ui/fonts/*` (Inter 400/500, JetBrains Mono 300/400, OFL),
`design/gallery.html`, `design/screenshots/*`. Nothing on `main` is touched, so the merge is clean.
Afterwards each surface adopts the classes below. The gallery is the reference for every state.

## 1. manifest.json

- Add `"extension/ui/tokens.css"` and `"extension/ui/components.css"` to the content script `css`
  list **before** `content.css` (so `content.css` can keep positioning rules only).
- Add `"ui/fonts/*.woff2"` to `web_accessible_resources` (matches `*://*.youtube.com/*`), or the
  `@font-face` URLs in tokens.css will not resolve inside the page.
- Popup: `popup.html` links `ui/tokens.css` and `ui/components.css`; no manifest change needed.

## 2. content.css → keep only layout, delete looks

Everything visual now comes from components.css. What stays in content.css:
`.ss-markers` z-index inside `.ytp-progress-bar`, `#movie_player.ad-showing .ss-markers { display:none }`,
`#movie_player.ytp-autohide .ss-badge { opacity: 0 }` (or toggle `.is-hidden`), toast/badge `position`
inside `#movie_player`. Remove the old color rules, `box-shadow`, `border-left`, `ss-fade-in`.

## 3. content.js — class and markup changes

| Today | Adopt |
| --- | --- |
| badge `div.ss-badge`, `textContent`, `data-kind` | `div.ss-badge.ss-glass.ss-ui[data-kind]` containing `<span class="ss-dot">` (add `.is-live` while kind=busy), `<span class="ss-badge__text">`, optional `<span class="ss-badge__count">2/4</span>`; for *ready* use `<span class="ss-chip">ready</span>` + count text. Replace `.ss-dim` after 4s with `.is-collapsed` after 3s. |
| `data-kind` values `ready / busy / off / error` | unchanged |
| markers `div.ss-marker[data-action=skip\|keep\|show]` with `title` | unchanged attributes; add `.is-low` when `confidence < SKIP_THRESHOLD` for sponsor reads (or `< SHOW_THRESHOLD + 0.1` for show). Replace the `title` tooltip with a child `div.ss-tooltip.ss-glass` built on `mouseenter`: `__head` (`__brand` + `.ss-chip` "sponsor · read"), `__time` ("9:17 → 9:55 · 38s · skip"), `__quote` (evidence), `__meta` ("confidence 0.91"). |
| toast `div.ss-toast` + two `button`s | `div.ss-toast.ss-glass.ss-ui` with `__head` (`__brand` "Skipped Odoo" + `.ss-chip`), `.ss-hairline` (`__track` > `__fill` @ o4 full width + `__span` left/width = segment / duration; `__ends` = start, "end · len"), `__quote.ss-stream.ss-stream--2` (omit when no evidence), `__actions` with `button.ss-button.ss-button--sm` Undo and `button.ss-button--text.ss-button--sm` Wrong skip. Add `.is-leaving` 180ms before removal. |
| `.ss-wrong` | drop; the text button class carries it |

Status text to show: `reading captions`, `analyzing` + count, `ready` + `N to skip` / `nothing to
skip`, `no captions`, `server offline`, `analysis failed`.

## 4. popup.html / popup.css / popup.js

Replace `popup.css` with the popup rules in components.css (it needs only `body { margin:0 }`).
Markup map:

| Today | Adopt |
| --- | --- |
| `<header>` img + h1 + `.switch` | `div.ss-popup__head` > `span.ss-popup__name` + `span.ss-status` (`.ss-dot` + `.ss-chip` server state). Drop the icon. |
| `.switch` checkbox `#enabled` | `button.ss-button.ss-toggle#enabled[aria-pressed]` with `span.ss-toggle__dot` + label "Skipping on" / "Paused". popup.js toggles `aria-pressed` and the label. |
| `#status` row with `.dot` + `#statusText` | folded into the head; dot classes: live → `.ss-dot.is-live`, ok → `.ss-dot` with `background: var(--ss-ink)`, off → `.ss-dot--faint` |
| `#video` row + `#missedBtn` | `div.ss-status` > `.ss-chip` channel + `span.ss-status__video` title; "Missed an ad here" becomes a `.ss-button--text.ss-button--sm` inside the channel card |
| `.card` + `h2` | `div.ss-card`, no heading; rows carry their own `.ss-row__hint` |
| `.row` / `.label` | `.ss-row` / `.ss-row__label` (+ optional `.ss-row__hint`) |
| `.seg button[data-value].on` | `div.ss-seg` > `button.ss-chip[data-value][aria-pressed]`; labels `skip` / `label` / `off` (value `show` keeps its storage key, only the label changes); the skip chip contains a `.ss-dot` |
| `.stats .num / .cap` | `div.ss-card.ss-stats` > `div` > `span.ss-stat__num` (mono 28) + `span.ss-stat__cap` |
| `#channelCard`, `.list`, `.remove`, `.btn` | `.ss-card` > `.ss-row` (channel name + `.ss-button--text.ss-button--sm` "Don't skip here") + `ul.ss-list` with `.ss-button--text.ss-button--sm` "remove" |
| `<footer>`, `.check`, `.link`, `.version` | `div.ss-popup__foot` > `label.ss-check` + `a` + `span.ss-mono.ss-mono--400` |
| `body.disabled .card { opacity:.45 }` | `.ss-card { opacity: var(--ss-o3) }` while paused (same idea, use the ladder) |

Dark mode is automatic through `prefers-color-scheme`; `:root[data-theme=dark|light]` forces it.

## 5. stats.py dashboard

- Serve `GET /ui/tokens.css`, `/ui/components.css`, `/ui/fonts/<name>.woff2` from `extension/ui/`
  (static, read once; `Content-Type: text/css` and `font/woff2`). Link the two sheets from the
  dashboard `<head>` and drop the inline `<style>`.
- `<body class="ss-ui ss-sheet">` and `<div class="ss-dash">`; header `div.ss-dash__head` >
  `h1.ss-dash__title` + `span.ss-dash__sub`.
- `tiles()` → `div.ss-tiles` > `div.ss-card.ss-tile` > `span.ss-stat__num` + `span.ss-stat__cap`.
- `hist()` → `div.ss-card` > `div.ss-bars` > `div.ss-bar` (height %; add `.is-peak` to the max) with
  `<span>` count; `div.ss-axis`.
- `hall()` → `div.ss-card.ss-hall` > `.ss-title` + `ol` > `li` > `span.ss-rank`, name, `span.ss-why`.
- `brandsTable()` / `creatorsTable()` → `table.ss-table` inside a `div.ss-card` (padding 16 8);
  numeric `th`/`td` get `.ss-num`; brand cell: `div.ss-primary`, `div.ss-raw`, `div.ss-verdict`;
  creator pills → `div.ss-chips` > `span.ss-chip`.
- Aliases line → `.ss-dash__sub`.

## 6. Decisions made on this branch (confirm or revert)

- **Ink text on light cards.** White on `#8E8E8E → #B4B4B4` is 3.3:1 at best and fails the 4.5:1
  rule in the spec; ink passes at o1 and o2. `.ss-card--deep` (`#4A → #5A`) is available where white
  text on a light sheet is wanted. Swap `--ss-card-text` to `#FFF` and the card gradient to the deep
  values if the look matters more than the ratio.
- **Marker shapes.** skip = filled accent, keep = hollow outline, show = dim fill, low confidence =
  hatch. This keeps "never color alone" without a second color.
- **Category chip labels** read `skip / label / off` instead of `Skip / Show / Off`; storage values
  are unchanged.
- **Accent is never text on the light sheet** (2.9:1); it is a dot beside the label.
