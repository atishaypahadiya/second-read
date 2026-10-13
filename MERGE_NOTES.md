# Merge notes — applying the design system

> **Status: applied on main.** Sections 1–5 are done for the badge, marker lane, tooltip, toast, popup and
> dashboard; the ad panel (section 3, last row) is not built. Where the real surfaces differ from these notes
> (font files, tooltip parent, popup height and panes, which stats the hero shows) is listed in DESIGN.md under
> "Applied on main".

Branch `design-system` adds only new files: `DESIGN.md`, `extension/ui/tokens.css`,
`extension/ui/components.css`, `extension/ui/fonts/*` (Geist 400/500, Geist Mono 300/400,
`OFL-Geist.txt`), `design/gallery.html`, `design/reference/*` (the approved mockup and board crops),
`design/screenshots/*`. Nothing on `main` is touched, so the merge is clean. Afterwards each surface
adopts the classes below. The gallery is the reference for every state; the mockup boards are the
visual truth.

## 1. manifest.json

- Add `"ui/tokens.css"` and `"ui/components.css"` to the content script `css` list **before**
  `content.css` (so `content.css` keeps only positioning).
- Add `"ui/fonts/*.woff2"` to `web_accessible_resources` (matches `*://*.youtube.com/*`), or the
  `@font-face` URLs in tokens.css will not resolve inside the page.
- Popup: `popup.html` links `ui/tokens.css` and `ui/components.css`; no manifest change needed.

## 2. content.css → keep only layout, delete looks

Everything visual now comes from components.css. What stays in content.css:
`.ss-lane` placement (inside `.ytp-progress-bar-container`, `bottom: calc(100% + 4px)`, which
components.css already sets; keep a YouTube-specific z-index if needed),
`#movie_player.ad-showing .ss-lane { display:none }`,
`#movie_player.ytp-autohide .ss-badge { opacity: 0 }` (or toggle `.is-hidden`),
toast / badge / panel `position` inside `#movie_player`. Remove the old colour rules, `box-shadow`,
`border-left`, `ss-fade-in`.

## 3. content.js — class and markup changes

| Today | Adopt |
| --- | --- |
| badge `div.ss-badge`, `textContent`, `data-kind` | `div.ss-badge.ss-glass.ss-ui[data-kind]` containing `<span class="ss-dot">`, `<span>` text, optional `<span class="ss-badge__count">`. Kinds: `reading` (reading captions), `busy` (analyzing + count `2/4`), `ready` (ready + count `1 to skip`), `none` (no ads), `nocaptions`, `off` (server offline), `error` (analysis failed). Replace `.ss-dim` after 4 s with `.is-collapsed` after 3 s. |
| markers `div.ss-markers` inside `.ytp-progress-bar` | `div.ss-lane` appended to `.ytp-progress-bar-container` (a sibling above the bar, never on it). Children unchanged: `div.ss-marker[data-action=skip\|keep\|show]`; add `.is-low` when `confidence < SKIP_THRESHOLD` for sponsor reads (or `< SHOW_THRESHOLD + 0.1` for show). |
| marker `title` tooltip | child `div.ss-tooltip.ss-glass.ss-glass--solid` built on `mouseenter`: `__head` (`__brand`, `.ss-chip` "sponsor · read", `__conf` "0.95"), `__time` ("4:47 – 5:39 · will skip" / "· plays"), `__quote` (evidence in quotes). |
| toast `div.ss-toast` + two `button`s | `div.ss-toast.ss-glass.ss-ui`: `__head` (`span.ss-dot.ss-dot--lg`, `__brand`, `.ss-chip`, `__meta` "0:52 skipped"), `.ss-hairline` (`__track` > `__span` left/width = segment / duration; `__ends` = "4:47 → 5:39" and the video length), `div.ss-stream.ss-stream--2` with the evidence (omit when none), `__actions` with `button.ss-button.ss-button--sm` **Undo** and `button.ss-button--text` **Wrong skip**. Add `.is-leaving` 180 ms before removal. |
| `.ss-wrong` | drop; the text button class carries it |
| ad panel (new) | `div.ss-panel.ss-panel--float.ss-ui`: `button.ss-grabber`, `div.ss-card.ss-panel__card` (`__head` > `__thumb` (+`__thumb-img`, `span.ss-dot.ss-dot--xl.ss-dot--corner`), `__info` > `__title-row` (`__title`, `.ss-chip`) + `.ss-hairline`), `div.ss-stream.ss-stream--panel` (append lines as they are spoken), `button.ss-button.ss-button--black.ss-button--xl` **Skip ad** (`span.ss-button__icon` + text). `.is-collapsed` toggled by the grabber. |

## 4. popup.html / popup.css / popup.js

Replace `popup.css` with the popup rules in components.css (it needs only `body { margin: 0 }`).
The popup becomes 360 wide. Markup map:

| Today | Adopt |
| --- | --- |
| `<header>` img + h1 + `.switch` | `div.ss-popup__head` > `span.ss-popup__name` "second read" + `span.ss-chip.ss-chip--status` (`.ss-dot` + text). Drop the icon. Dot classes: ready → `.ss-dot--ink`, analyzing → `.ss-dot.is-live` (accent), offline → `.ss-dot--ring`. |
| `.switch` checkbox `#enabled` | `button.ss-button.ss-toggle#enabled[aria-pressed]` > `span` label ("Skipping on" / "Paused") + `span.ss-toggle__track` > `span.ss-toggle__knob`. popup.js toggles `aria-pressed` and the label. |
| `#status` row with `.dot` + `#statusText` | folded into the head chip |
| `.stats` card (four numbers) | `div.ss-card.ss-hero`: `span.ss-hero__num` (saved this week, m:ss), `span.ss-hero__cap` "saved this week", `div.ss-chips` with `span.ss-chip.ss-chip--mono` "N ads", "N brands", "N wrong". All-time numbers move to the dashboard. |
| `#video` row + `#channelCard` + `#allowBtn` | `div.ss-channel`: `span.ss-channel__thumb` (channel avatar as `background-image`, or the gradient placeholder), `div.ss-channel__text` (`__name`, `__sub` "1 ad on this video" / "analyzing…" / "no captions"), `button.ss-button--text` "Don't skip <channel>" (already allowlisted → "Skip on <channel> again"). The allowlist `ul` moves to the dashboard or stays as `ul.ss-list` under the row. |
| `.card` + `h2` "What to do with…" | `div.ss-rows` > `span.ss-label` "categories" + one `div.ss-row` per category |
| `.row` / `.label` / `.seg button[data-value].on` | `div.ss-row` > `span.ss-row__label` (lowercase: "sponsor read", "sponsor integration", "self-promo", "charity", "like & subscribe") + `div.ss-seg[data-key]` > `button.ss-seg__btn[data-value][aria-pressed]` with labels `skip` / `label` / `off` (value `show` keeps its storage key, only the label changes) |
| `#missedBtn`, `<footer>`, `.link` | `div.ss-popup__spacer` + `div.ss-popup__foot` > `button.ss-button--text#missedBtn` "Missed an ad here" + `a.ss-button--text` "Dashboard ↗". Dev-mode checkbox: keep as a `label.ss-check` in the foot or move to the dashboard. |
| `body.disabled .card { opacity:.45 }` | set `opacity: var(--ss-o3)` on `.ss-hero` and `.ss-rows` while paused |

Dark mode is automatic through `prefers-color-scheme`; `:root[data-theme=dark|light]` forces it.

## 5. stats.py dashboard

- Serve `GET /ui/tokens.css`, `/ui/components.css`, `/ui/fonts/<name>.woff2` from `extension/ui/`
  (static, read once; `Content-Type: text/css` and `font/woff2`). Link the two sheets from the
  dashboard `<head>` and drop the inline `<style>`.
- `<body class="ss-ui ss-sheet">` and `<div class="ss-dash">`; header `div.ss-dash__head` >
  `h1.ss-dash__title` "second read · stats" + `span.ss-dash__sub`.
- `tiles()` → `div.ss-tiles` > `div.ss-card.ss-tile` > `span.ss-stat__num` + `span.ss-stat__cap`.
- `hist()` → `div.ss-card` > `div.ss-bars` > `div.ss-bar` (height %; `.is-peak` on the max) with a
  `<span>` count; `div.ss-axis`.
- `hall()` → `div.ss-card.ss-hall` > `.ss-title` + `ol` > `li` > `span.ss-rank`, name, `span.ss-why`.
- `brandsTable()` / `creatorsTable()` → `table.ss-table` inside `div.ss-card.ss-card--light`
  (padding 16 8); numeric `th`/`td` get `.ss-num`; brand cell: name, `div.ss-raw`, `div.ss-verdict`;
  creator pills → `div.ss-chips` > `span.ss-chip`.
- Section headings `h2` are mono lowercase (`brands`, `creators`, `where ads sit in a video`).
- Aliases line → `.ss-dash__sub`.

## 6. Decisions made on this branch (confirm or revert)

- **Deep card is the default** (`#555555 → #5A5A5A`, white text); light card (`#8E → #B4`, ink) is
  `.ss-card--light` for tables and lists only. The bottom stop was darkened from `#717171` so white o2 passes
  4.5:1 on the whole card (4.8 → 4.5); the gradient is nearly flat as a result.
- **Marker lane** is a separate 4px strip 4px above YouTube's bar; markers never overlay the bar.
- **Marker shapes.** skip = filled accent, keep = hollow outline, show = dim fill, low confidence =
  hatch (the mockup draws integration as a solid white bar; the outline keeps "never colour alone").
- **Badge kinds** grew from four to seven (`reading`, `busy`, `ready`, `none`, `nocaptions`, `off`,
  `error`); `none` is new and distinct from `ready`.
- **Category chip labels** read `skip / label / off`; storage values are unchanged. The mockup's
  five rows include `sponsor integration` and `charity`, which main is adding.
- **Popup width 360** (was 320) and the product name `second read` in the header.
- **Accent is never text** on the sheet (2.9:1) or a card (2.2:1); it is a dot, a knob or a marker.
