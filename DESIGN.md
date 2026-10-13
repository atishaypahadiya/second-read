# Sponsor Skipper — design language

One system for everything the extension draws: the badge and markers on the player, the toast,
the popup, the dashboard and any future panel. Implemented as `extension/ui/tokens.css`
(variables, fonts, keyframes) and `extension/ui/components.css` (classes). Every component in
every state is on `design/gallery.html` (open via `file://`, no server needed).

## Principles

1. **Monochrome plus one accent.** Color is state only. The accent `#FF4A1C` means *live* or *skip*:
   the pulsing dot while analyzing, the skip marker, the skipped span on a hairline, the selected
   "skip" chip. Nothing else is colored.
2. **Hierarchy by opacity, never by weight or extra colors.** Five steps: o1 1.0, o2 0.72, o3 0.55,
   o4 0.32, o5 0.18. No font-weight above 500.
3. **Depth by tone.** Sheet, card gradient, glass. No drop shadows, no borders, no dividers.
   Whitespace separates.
4. **Soft geometry.** Large continuous radii; pills for every button and chip; concentric radii
   (outer = inner + gap). Sheet 40, card 32, toast 20, thumbnails 16–28.
5. **Two typefaces.** Inter for UI labels (400/500). JetBrains Mono (300/400) for anything that is
   data or content: transcripts, timestamps, numbers, evidence quotes. Chip labels are lowercase.
   Fonts are bundled woff2 under `extension/ui/fonts/` (SIL OFL); no Google Fonts requests.
6. **Bias toward playing is visible.** Anything the system will *not* skip is quiet (white, outlined
   or dim); only a skip gets the accent. A low-confidence segment is hatched so it never looks like
   a confident one.

## Tokens (`extension/ui/tokens.css`)

All variables are prefixed `--ss-`.

| Group | Tokens |
| --- | --- |
| Light sheet | `--ss-sheet #F0F0F0`, `--ss-ink #000`, `--ss-card-top #8E8E8E`, `--ss-card-bottom #B4B4B4` (vertical gradient), `--ss-on-card #FFF` (marks), `--ss-card-text` (ink; see Contrast) |
| Deep card | `--ss-deep-top #4A4A4A`, `--ss-deep-bottom #5A5A5A` — `.ss-card--deep`, white text passes 4.5:1 |
| Dark sheet | `--ss-sheet #111`, cards `#2A2A2A → #3A3A3A`, ink white. Applied by `prefers-color-scheme: dark`, `:root[data-theme=dark]`, or scoped with `.ss-theme-dark` / `.ss-theme-light` |
| Glass | `--ss-glass rgba(28,28,28,.62)` + `--ss-glass-blur blur(20px) saturate(140%)`, `--ss-on-glass #FFF` |
| Accent | `--ss-accent #FF4A1C`, `--ss-accent-rgb` |
| Opacity | `--ss-o1 … --ss-o5` = 1 / .72 / .55 / .32 / .18 |
| Radius | `--ss-r-sheet 40`, `--ss-r-card 32`, `--ss-r-toast 20`, `--ss-r-pill 999`, `--ss-r-thumb-s/m/l 16/20/28` |
| Spacing | `--ss-s1 … --ss-s6` = 4 / 8 / 12 / 16 / 24 / 32 |
| Type | `--ss-font-ui`, `--ss-font-mono`; sizes `--ss-fs-chip 11`, `--ss-fs-body 13`, `--ss-fs-title 15`, `--ss-fs-stat 28`; `--ss-lh-body 1.4`, `--ss-lh-title 1.2`; weights 400 / 500 / mono 300 |
| Sizes | chip 20, button 48, dot 10 / 12, hairline 2, marker 4, grabber 44×28 |
| Motion | `--ss-dur 180ms`, `--ss-ease cubic-bezier(.2,.8,.2,1)`, `--ss-rise 6px`, `--ss-pulse 1.6s`; keyframes `ss-enter`, `ss-exit`, `ss-pulse`. Under `prefers-reduced-motion` duration and rise are 0 and the pulse stops. |

Per-surface derived variables (`--ss-fg-rgb`, `--ss-chip-bg`, `--ss-mark`) are set by `.ss-sheet`,
`.ss-card` and `.ss-glass`, so a chip, dot or hairline looks right wherever it is dropped.

## Components (`extension/ui/components.css`)

Everything is prefixed `ss-` so it can live inside YouTube's DOM. Add `.ss-ui` on the root of any
surface to get the font stack and box-sizing.

- **Chip** `.ss-chip` — pill, height 20, padding 0 8, 11px lowercase; white @ .22 on cards and glass,
  black @ .06 on the sheet. `.ss-chip--mono` for numbers. May contain a `.ss-dot`.
- **Status dot** `.ss-dot` — 10px accent circle; `.ss-dot--lg` 12px; `.is-live` pulses 1.6s;
  `.ss-dot--neutral` / `--faint` for not-live states; `.ss-dot--corner` overlaps a parent's corner
  (parent gets `.ss-has-dot`).
- **Hairline** `.ss-hairline` > `.ss-hairline__track` (2px @ o4) > `.ss-hairline__fill` (@ o1, set
  `width`) or `.ss-hairline__span` (accent, set `left` + `width`); `.ss-hairline__ends` holds mono
  timestamps @ o3 under the ends.
- **Primary button** `.ss-button` — full-width pill, 48 tall, black on light / white on dark or glass,
  15px label, optional `.ss-button__icon` (rounded square). One per surface. `--sm` 36 tall,
  `--auto` shrink-to-fit.
- **Secondary** `.ss-button--text` — text only @ o3, no background.
- **Grabber** `.ss-grabber` — 44×28 pill @ o4 with a 16×2 dash. Collapses panels.
- **Fading stream** `.ss-stream` — mono 300, `mask-image` fades the top 45%; `.ss-stream--2` clamps
  to two lines and fades the bottom (toast evidence).

### Badge (player, bottom right, dev mode)
`.ss-badge.ss-glass[data-kind=busy|ready|off|error]` containing `.ss-dot`, `.ss-badge__text`,
`.ss-badge__count` (mono, e.g. `2/4`) and a `.ss-chip` for *ready*. `.is-collapsed` after 3s leaves
only the dot. `.is-hidden` follows YouTube's control autohide. The off and error dots carry a dash so
they are not color-only.

### Markers (progress bar)
`.ss-markers` is a 4px strip centred on the bar; each `.ss-marker[data-action]` is a round-capped
bar. Meaning is **shape and color**: `skip` filled accent; `keep` (integration) hollow white outline;
`show` (label only) dim white fill @ o4; `.is-low` adds a diagonal hatch. Hover scales to 1.5× and
shows `.ss-tooltip.ss-glass` with brand, category chip, mono time range, the evidence quote in mono
@ o2, and confidence.

### Toast
`.ss-toast.ss-glass` (r20, padding 16, 340 wide): `__head` with brand (15px) and chip; hairline with
the skipped span in accent and mono ends; `__quote.ss-stream.ss-stream--2`; `__actions` with a white
pill **Undo** and a text button **Wrong skip**. `ss-enter` on mount, `.is-leaving` to exit, 6s
auto-dismiss.

### Popup (320px)
`.ss-popup.ss-sheet`: `__head` (name + status dot + chip), the master `.ss-button.ss-toggle`
(`aria-pressed`, label *Skipping on* / *Paused*), `.ss-status` with the channel chip and video title,
`.ss-card` blocks: category `.ss-row`s with `.ss-seg` chip selectors (`skip` / `label` / `off`,
`aria-pressed`), `.ss-stats` grid with `.ss-stat__num` (mono 28 tabular) and `.ss-stat__cap`, the
channel card, and `.ss-popup__foot` with the dev-mode `.ss-check` and dashboard link.

### Dashboard
`.ss-dash` on a `.ss-sheet`: `.ss-tiles` of `.ss-card.ss-tile` (mono 40 numbers), the ad-position
histogram `.ss-bars` (hairline-weight bars, peak in accent, `.ss-axis` labels), `.ss-hall` lists with
mono rank and reason, and `.ss-table` with no borders: 4px row spacing, lowercase headers @ o3,
`.ss-num` cells right-aligned mono @ o2, `.ss-verdict` mono @ o2, `.ss-raw` @ o4, brand chips.

### Ad panel (future)
`.ss-panel.ss-glass` with a grabber, title + chip, the live transcript in `.ss-stream` (ad lines in
accent), the ad's span on a hairline and one primary action. `.is-collapsed` leaves the grabber.

## Contrast

Measured WCAG ratios (see the table at the bottom of the gallery):

| Surface | Text | o1 | o2 |
| --- | --- | --- | --- |
| sheet #F0F0F0 | ink | 18.4 | 8.7 |
| card #8E8E8E → #B4B4B4 | ink | 6.4 – 10.1 | 4.5 – 6.2 |
| card #8E8E8E → #B4B4B4 | white | 3.3 – 2.1 | 2.5 – 1.7 |
| deep card #4A → #5A | white | 6.9 | 4.5 |
| dark sheet #111 / cards #2A → #3A | white | 11.4 – 18.9 | 6.8 – 9.9 |
| glass over #1C1C1C | white | 17.0 | 9.3 |
| glass | accent | 5.1 | – |
| sheet | accent | 2.9 | – |

Consequences, applied in the tokens:
- **Light cards carry ink text**, not white. White on the spec's `#8E8E8E → #B4B4B4` gradient is
  3.3:1 at best. `--ss-on-card #FFF` is kept for marks (chip fill, hairline fill, grabber dash).
  `.ss-card--deep` (`#4A → #5A`) is the variant for white text on a light sheet.
- **The accent is never text on the light sheet** (2.9:1). It is a dot or a mark next to a label.
  On glass it may be text (5.1:1), which the panel uses for ad lines.
- o3 and below are for captions, timestamps and disabled states, which are not required to pass.
- Marker meaning does not depend on color: filled / outlined / dim / hatched, plus the tooltip text.

## Motion

Enter and exit: fade plus a 6px rise over 180ms with `cubic-bezier(.2,.8,.2,1)`. Only the accent dot
animates on its own (1.6s pulse while live). Hover on a marker scales it; nothing else moves without
a user action. `prefers-reduced-motion` zeroes the duration and rise and stops the pulse.

## Writing

Sentence case everywhere except chips, which are lowercase. Buttons say what happens: *Skipping on*,
*Paused*, *Undo*, *Wrong skip*, *Missed an ad here*, *Skip to 5:10*. Status reads as a state, not a
sentence: *analyzing 2/4*, *ready · 1 to skip*, *server offline*. Empty and error states say what to
do next (*Start the server: python3 server/server.py*).
