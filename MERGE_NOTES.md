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
`.sr-lane` placement (inside `.ytp-progress-bar-container`, `bottom: calc(100% + 4px)`, which
components.css already sets; keep a YouTube-specific z-index if needed),
`#movie_player.ad-showing .sr-lane { display:none }`,
`#movie_player.ytp-autohide .sr-badge { opacity: 0 }` (or toggle `.is-hidden`),
toast / badge / panel `position` inside `#movie_player`. Remove the old colour rules, `box-shadow`,
`border-left`, `sr-fade-in`.

## 3. content.js — class and markup changes

| Today | Adopt |
| --- | --- |
| badge `div.sr-badge`, `textContent`, `data-kind` | `div.sr-badge.sr-glass.sr-ui[data-kind]` containing `<span class="sr-dot">`, `<span>` text, optional `<span class="sr-badge__count">`. Kinds: `reading` (reading captions), `busy` (analyzing + count `2/4`), `ready` (ready + count `1 to skip`), `none` (no ads), `nocaptions`, `off` (server offline), `error` (analysis failed). Replace `.sr-dim` after 4 s with `.is-collapsed` after 3 s. |
| markers `div.sr-markers` inside `.ytp-progress-bar` | `div.sr-lane` appended to `.ytp-progress-bar-container` (a sibling above the bar, never on it). Children unchanged: `div.sr-marker[data-action=skip\|keep\|show]`; add `.is-low` when `confidence < SKIP_THRESHOLD` for sponsor reads (or `< SHOW_THRESHOLD + 0.1` for show). |
| marker `title` tooltip | child `div.sr-tooltip.sr-glass.sr-glass--solid` built on `mouseenter`: `__head` (`__brand`, `.sr-chip` "sponsor · read", `__conf` "0.95"), `__time` ("4:47 – 5:39 · will skip" / "· plays"), `__quote` (evidence in quotes). |
| toast `div.sr-toast` + two `button`s | `div.sr-toast.sr-glass.sr-ui`: `__head` (`span.sr-dot.sr-dot--lg`, `__brand`, `.sr-chip`, `__meta` "0:52 skipped"), `.sr-hairline` (`__track` > `__span` left/width = segment / duration; `__ends` = "4:47 → 5:39" and the video length), `div.sr-stream.sr-stream--2` with the evidence (omit when none), `__actions` with `button.sr-button.sr-button--sm` **Undo** and `button.sr-button--text` **Wrong skip**. Add `.is-leaving` 180 ms before removal. |
| `.sr-wrong` | drop; the text button class carries it |
| ad panel (new) | `div.sr-panel.sr-panel--float.sr-ui`: `button.sr-grabber`, `div.sr-card.sr-panel__card` (`__head` > `__thumb` (+`__thumb-img`, `span.sr-dot.sr-dot--xl.sr-dot--corner`), `__info` > `__title-row` (`__title`, `.sr-chip`) + `.sr-hairline`), `div.sr-stream.sr-stream--panel` (append lines as they are spoken), `button.sr-button.sr-button--black.sr-button--xl` **Skip ad** (`span.sr-button__icon` + text). `.is-collapsed` toggled by the grabber. |

## 4. popup.html / popup.css / popup.js

Replace `popup.css` with the popup rules in components.css (it needs only `body { margin: 0 }`).
The popup becomes 360 wide. Markup map:

| Today | Adopt |
| --- | --- |
| `<header>` img + h1 + `.switch` | `div.sr-popup__head` > `span.sr-popup__name` "second read" + `span.sr-chip.sr-chip--status` (`.sr-dot` + text). Drop the icon. Dot classes: ready → `.sr-dot--ink`, analyzing → `.sr-dot.is-live` (accent), offline → `.sr-dot--ring`. |
| `.switch` checkbox `#enabled` | `button.sr-button.sr-toggle#enabled[aria-pressed]` > `span` label ("Skipping on" / "Paused") + `span.sr-toggle__track` > `span.sr-toggle__knob`. popup.js toggles `aria-pressed` and the label. |
| `#status` row with `.dot` + `#statusText` | folded into the head chip |
| `.stats` card (four numbers) | `div.sr-card.sr-hero`: `span.sr-hero__num` (saved this week, m:ss), `span.sr-hero__cap` "saved this week", `div.sr-chips` with `span.sr-chip.sr-chip--mono` "N ads", "N brands", "N wrong". All-time numbers move to the dashboard. |
| `#video` row + `#channelCard` + `#allowBtn` | `div.sr-channel`: `span.sr-channel__thumb` (channel avatar as `background-image`, or the gradient placeholder), `div.sr-channel__text` (`__name`, `__sub` "1 ad on this video" / "analyzing…" / "no captions"), `button.sr-button--text` "Don't skip <channel>" (already allowlisted → "Skip on <channel> again"). The allowlist `ul` moves to the dashboard or stays as `ul.sr-list` under the row. |
| `.card` + `h2` "What to do with…" | `div.sr-rows` > `span.sr-label` "categories" + one `div.sr-row` per category |
| `.row` / `.label` / `.seg button[data-value].on` | `div.sr-row` > `span.sr-row__label` (lowercase: "sponsor read", "sponsor integration", "self-promo", "charity", "like & subscribe") + `div.sr-seg[data-key]` > `button.sr-seg__btn[data-value][aria-pressed]` with labels `skip` / `label` / `off` (value `show` keeps its storage key, only the label changes) |
| `#missedBtn`, `<footer>`, `.link` | `div.sr-popup__spacer` + `div.sr-popup__foot` > `button.sr-button--text#missedBtn` "Missed an ad here" + `a.sr-button--text` "Dashboard ↗". Dev-mode checkbox: keep as a `label.sr-check` in the foot or move to the dashboard. |
| `body.disabled .card { opacity:.45 }` | set `opacity: var(--sr-o3)` on `.sr-hero` and `.sr-rows` while paused |

Dark mode is automatic through `prefers-color-scheme`; `:root[data-theme=dark|light]` forces it.

## 5. stats.py dashboard

- Serve `GET /ui/tokens.css`, `/ui/components.css`, `/ui/fonts/<name>.woff2` from `extension/ui/`
  (static, read once; `Content-Type: text/css` and `font/woff2`). Link the two sheets from the
  dashboard `<head>` and drop the inline `<style>`.
- `<body class="sr-ui sr-sheet">` and `<div class="sr-dash">`; header `div.sr-dash__head` >
  `h1.sr-dash__title` "second read · stats" + `span.sr-dash__sub`.
- `tiles()` → `div.sr-tiles` > `div.sr-card.sr-tile` > `span.sr-stat__num` + `span.sr-stat__cap`.
- `hist()` → `div.sr-card` > `div.sr-bars` > `div.sr-bar` (height %; `.is-peak` on the max) with a
  `<span>` count; `div.sr-axis`.
- `hall()` → `div.sr-card.sr-hall` > `.sr-title` + `ol` > `li` > `span.sr-rank`, name, `span.sr-why`.
- `brandsTable()` / `creatorsTable()` → `table.sr-table` inside `div.sr-card.sr-card--light`
  (padding 16 8); numeric `th`/`td` get `.sr-num`; brand cell: name, `div.sr-raw`, `div.sr-verdict`;
  creator pills → `div.sr-chips` > `span.sr-chip`.
- Section headings `h2` are mono lowercase (`brands`, `creators`, `where ads sit in a video`).
- Aliases line → `.sr-dash__sub`.

## 6. Decisions made on this branch (confirm or revert)

- **Deep card is the default** (`#444444 → #5A5A5A`, white text); light card (`#8E → #B4`, ink) is
  `.sr-card--light` for tables and lists only. Both stops were darkened from `#555555 → #717171` so white o2
  passes 4.5:1 on the whole card (6.3 → 4.5) and the gradient keeps its depth.
- **Marker lane** is a separate 4px strip 4px above YouTube's bar; markers never overlay the bar.
- **Marker shapes.** skip = filled accent, keep = hollow outline, show = dim fill, low confidence =
  hatch (the mockup draws integration as a solid white bar; the outline keeps "never colour alone").
- **Badge kinds** grew from four to seven (`reading`, `busy`, `ready`, `none`, `nocaptions`, `off`,
  `error`); `none` is new and distinct from `ready`.
- **Category chip labels** read `skip / label / off`; storage values are unchanged. The mockup's
  five rows include `sponsor integration` and `charity`, which main is adding.
- **Popup width 360** (was 320) and the product name `second read` in the header.
- **Accent is never text** on the sheet (2.9:1) or a card (2.1–2.9:1); it is a dot, a knob or a marker.
