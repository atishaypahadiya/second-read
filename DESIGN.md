# Second Read — design language

One system for everything the extension draws: the badge, marker lane, toast and ad panel on the
player, the popup, and the dashboard. The approved visual mockup is `design/reference/mockup.html`
(four boards; crops in `design/reference/mock-board*.png`). It is the source of truth; the values
below were extracted from it. Implemented as `extension/ui/tokens.css` (variables,
keyframes), `extension/ui/fonts.css` / `fonts-content.css` (the bundled faces, for extension pages and for the
YouTube page respectively) and `extension/ui/components.css` (classes). Every board is rebuilt with the real classes
next to its mockup crop on `design/gallery.html` (open via `file://`, no server needed).

The product is **Second Read** (formerly Sponsor Skipper). Display name "Second Read" in the manifest, README and
docs; the popup and dashboard headers use the lowercase `second read`. Classes and variables are prefixed `sr-`.

## Principles

1. **Monochrome plus one accent.** Color is state only. The accent `#FF4A1C` means *live* or *skip*:
   the dot while analyzing, the ready dot, the skip marker, the toggle knob, the toast's brand dot,
   the thumbnail's corner badge. Nothing else is colored.
2. **Hierarchy by opacity, never by weight or extra colors.** Five steps: o1 1.0, o2 0.72, o3 0.55,
   o4 0.32, o5 0.18. No font-weight above 500; in practice everything is 400 and mono is 300.
3. **Depth by tone.** Sheet, deep card gradient, glass. No drop shadows, no borders, no dividers.
   Whitespace separates.
4. **Soft geometry.** Large continuous radii; pills for every button and chip; concentric radii
   (outer = inner + gap). Panel 44, sheet 40, card 32, thumbnail 26, toast 20, tooltip 16, small
   thumbnail 12.
5. **Two typefaces.** Geist for UI (400, 500 reserved). Geist Mono (300 content, 400 numbers in
   tables) for anything that is data or content: transcripts, timestamps, counts, evidence quotes,
   section labels. Chip labels and section labels are lowercase. Fonts are bundled woff2 under
   `extension/ui/fonts/` (SIL OFL, `OFL-Geist.txt`); no Google Fonts requests.
6. **Bias toward playing is visible.** Anything the system will *not* skip is quiet (white, outlined
   or dim); only a skip gets the accent. A low-confidence segment is hatched so it never looks like
   a confident one.
7. **Invisible until it matters.** The viewer should feel they are getting YouTube's own experience,
   slightly better. Nothing announces itself: no pop-ups, no text on the player that the viewer did
   not ask for. What the extension did shows as a small change of state in something already there
   (a marker draining from accent to dim after a skip) and the detail sits behind a hover. Before
   adding anything visible, look for a way to say it with a state change instead.

## Tokens (`extension/ui/tokens.css`)

All variables are prefixed `--sr-`.

| Group | Tokens |
| --- | --- |
| Light sheet | `--sr-sheet #F0F0F0`, `--sr-ink #000`, chips black @ .06, primary button black / white |
| Deep card (default) | `--sr-card-top #444444 → --sr-card-bottom #5A5A5A`, white text on the ladder (o1 and o2 pass everywhere). Stats hero, tiles, ad panel, toast-like cards. |
| Light card | `--sr-card-light-top #8E8E8E → #B4B4B4`, ink text. `.sr-card--light`, secondary blocks only (tables, lists). |
| Dark sheet | `#111`, cards `#2A2A2A → #3A3A3A`, ink white. Applied by `prefers-color-scheme: dark`, `:root[data-theme=dark]`, or scoped with `.sr-theme-dark` / `.sr-theme-light`. |
| Glass | `--sr-glass rgba(28,28,28,.62)` + `blur(20px) saturate(140%)` for badge, toast and floating panels; `--sr-glass-solid rgba(40,40,40,.82)` for the marker tooltip (no blur). |
| Accent | `--sr-accent #FF4A1C`, `--sr-accent-rgb` |
| Opacity | `--sr-o1 … --sr-o5` = 1 / .72 / .55 / .32 / .18 |
| Radius | `--sr-r-panel 44`, `--sr-r-sheet 40`, `--sr-r-card 32`, `--sr-r-thumb-l 26`, `--sr-r-toast 20`, `--sr-r-tooltip 16`, `--sr-r-thumb-s 12`, `--sr-r-pill 999` |
| Spacing | `--sr-s1 … --sr-s7` = 4 / 8 / 12 / 16 / 20 / 24 / 32 |
| Type | `--sr-font-ui`, `--sr-font-mono`; sizes chip 11, body 13, title 15, panel 17, stat 28, hero 44; line-height 1.4 body, 1.45 stream, 1.2 titles |
| Sizes | chip 20 / 22 (mono counts) / 24 (status), badge 32, buttons 48 / 44 (toast, text) / 56 (master toggle) / 64 (Skip ad), segmented buttons 28, dot 8 / 10 / 14, hairline 2, marker 4, lane gap 4, grabber 44×28 / 112×56 |
| Motion | `--sr-dur 180ms`, `--sr-ease cubic-bezier(.2,.8,.2,1)`, `--sr-rise 6px`, `--sr-pulse 1.6s` (scale .75, opacity .35); keyframes `sr-enter`, `sr-exit`, `sr-pulse`. Under `prefers-reduced-motion` duration and rise are 0 and the pulse stops. |

Per-surface derived variables (`--sr-fg-rgb`, `--sr-chip-bg`, `--sr-mark`) are set by `.sr-sheet`,
`.sr-card`, `.sr-card--light` and `.sr-glass`, so a chip, dot, hairline or grabber looks right
wherever it is dropped.

## Components (`extension/ui/components.css`)

Everything is prefixed `sr-` so it can live inside YouTube's DOM. Put `.sr-ui` on the root of any
surface to get the font stack and box-sizing.

- **Chip** `.sr-chip` — pill, 20 tall, padding 0 8, 11px lowercase; white @ .22 on cards and glass,
  black @ .06 on the sheet. `.sr-chip--mono` (22 tall, mono counts), `.sr-chip--status` (24 tall,
  with a 6px dot, @ o2). `.sr-chips` lays them out.
- **Dot** `.sr-dot` — 8px accent; `--lg` 10, `--xl` 14; `--ink` (black on sheet / white on dark),
  `--neutral` (@ .6), `--faint` (@ o4), `--ring` (1.5px accent ring, "offline"); `.is-live` pulses;
  `--corner` overlaps the bottom-right corner of a `.sr-has-dot` parent.
- **Hairline** `.sr-hairline` > `__track` (2px @ o4) > `__fill` (@ o1, set `width`) or `__span`
  (set `left` + `width`; white by default, `--accent` variant); `__ends` holds mono 11 timestamps @ o2.
- **Primary button** `.sr-button` — full-width pill, black on light / white on dark, glass and cards;
  `--black` forces black (the ad panel's "Skip ad"); heights `--sm` 44, default 48, `--lg` 56, `--xl` 64;
  `--auto` shrink-to-fit; `__icon` is a 14px rounded square. One primary per surface.
- **Text button** `.sr-button--text` — 44 tall, @ .6 on the sheet, @ o2 on glass, no background.
- **Grabber** `.sr-grabber` — 44×28 pill (`#8E8E8E` on the sheet, white @ o4 on glass) with a
  16×2 white dash; `--lg` 112×56 with an 18px dash.
- **Stream** `.sr-stream` — mono 300, `mask-image` dissolves the top 50% so older lines fade upward;
  `--2` clamps the toast quote to two lines @ o2; `--panel` is the 15px / 1.45 transcript.
- **Section label** `.sr-label` — mono 11 @ o3, lowercase (`categories`).

### Badge (player, bottom right, dev mode)
`.sr-badge.sr-glass[data-kind]`: 32 tall, 8px dot, 13px text, mono count @ o2. States:

| `data-kind` | dot | text |
| --- | --- | --- |
| `reading` | white @ .6, pulsing | reading captions |
| `busy` | accent, pulsing | analyzing + `__count` 2/4 |
| `ready` | accent, solid | ready + `__count` 1 to skip |
| `none` | white @ o4, text @ o2 | no ads |
| `nocaptions` | white @ o4, text @ o2 | no captions |
| `off` | accent ring | server offline |
| `error` | accent with a dark dash | analysis failed |

`.is-collapsed` after 3 s leaves a 32×32 pill with only the dot. `.is-hidden` follows YouTube's
control autohide.

### Marker lane (above the progress bar)
`.sr-lane` is a 4px strip positioned `bottom: calc(100% + 4px)` relative to YouTube's bar, so the
accent never sits on YouTube's red. Each `.sr-marker[data-action]` is a round-capped bar. Meaning is
**shape and color**: `skip` filled accent; `keep` (integration) hollow white outline; `show` (label
only) white fill @ o4; `.is-low` hatches (white .7 / .12, 3px stripes; accent stripes on a skip
candidate). `.is-done` on a skip marker (the segment was skipped, undone, or seeked into, so it will
not skip again in this page view) drains the accent to the dim fill over `--sr-dur`; its tooltip
reads "skipped" or "plays" instead of "will skip". Hover scales to 1.5× and shows `.sr-tooltip.sr-glass.sr-glass--solid` (320 wide, r16,
padding 14 16): brand, chip, mono confidence, mono time line ("4:47 – 5:39 · will skip"), evidence
quote mono 13 @ .85.

### Toast (after a skip)
`.sr-toast.sr-glass` (380 wide, r20, padding 16, gap 12, bottom-left of the player): `__head` with a
10px accent dot, brand (15px), chip "sponsor · read" and `__meta` mono "0:52 skipped" @ o2; hairline
with the skipped span in white and mono ends "4:47 → 5:39" / "21:16"; `.sr-stream--2` evidence
quote (max 2 lines); `__actions` with a 44px white pill **Undo** and a 44px text button **Wrong
skip**. `sr-enter` on mount, `.is-leaving` to exit, 6 s auto-dismiss.

### Popup (360)
`.sr-popup.sr-sheet` (padding 20, gap 16): `__head` (32 tall) with the lowercase `__name` and a
`.sr-chip--status` server chip; the master `.sr-button.sr-toggle` (56 tall, black, label
*Skipping on* / *Paused*, `__track` 52×32 white @ .22 with a 24px accent `__knob`; `aria-pressed`
drives it); the deep `.sr-card.sr-hero` with `__num` mono 44, `__cap` "saved this week" @ o2 and
mono count chips; `.sr-channel` (36px r12 thumbnail, name, mono sub, text button "Don't skip
<channel>"); `.sr-rows` with `.sr-label` and `.sr-row`s (40 tall) holding `.sr-seg` > `.sr-seg__btn`
(28 tall, 11px lowercase, selected = black pill with white text, `aria-pressed`); `__spacer`;
`__foot` with text buttons "Missed an ad here" and "Dashboard ↗".

### Ad panel
`.sr-panel` (440 wide board; `--float` for the on-player version at 376 wide): a `.sr-grabber--lg`,
then `.sr-card.sr-panel__card` (r44, padding 24, gap 20) with `__head` (104×84 r26 `__thumb` +
14px accent `.sr-dot--corner`, `__title` 17px + chip, hairline with mono ends) and the
`.sr-stream--panel` transcript (ad lines, newest at the bottom), then a 64px `.sr-button--black`
**Skip ad** with the rounded-square icon. `.is-collapsed` leaves the grabber.

### Dashboard
`.sr-dash` on a `.sr-sheet`: deep `.sr-tile`s for numbers (mono 40), the ad-position histogram
`.sr-bars` (hairline-weight bars, peak in accent), `.sr-hall` lists, and `.sr-table` inside a
`.sr-card--light`: no borders, 4px row spacing, mono lowercase headers @ o3, `.sr-num` cells
right-aligned mono @ o2, `.sr-verdict` mono @ o2.

## Contrast

Measured WCAG ratios (also tabulated at the bottom of the gallery):

| Surface | Text | o1 | o2 | 0.6 | o3 |
| --- | --- | --- | --- | --- | --- |
| sheet #F0F0F0 | ink | 18.4 | 8.7 | 5.5 | 4.6 |
| deep card #444444 (top) | white | 9.7 | 6.0 | 4.7 | 4.2 |
| deep card #4F4F4F (middle) | white | 8.2 | 5.2 | 4.2 | 3.8 |
| deep card #5A5A5A (bottom) | white | 6.9 | 4.5 | 3.7 | 3.4 |
| light card #8E8E8E → #B4B4B4 | ink | 6.4 – 10.1 | 4.5 – 6.2 | 3.5 – 4.4 | 3.2 – 3.8 |
| dark sheet #111 / cards #2A → #3A | white | 11.4 – 18.9 | 6.8 – 9.9 | 6.3 | 4.7 – 6.2 |
| glass / tooltip over #282828 | white | 14.7 | 8.4 | 6.3 | 5.5 |
| glass over #282828 | accent | 4.4 | | | |
| deep card | accent | 2.1 – 2.9 | | | |
| sheet | accent | 2.9 | | | |

Decisions that follow:
- **Deep cards carry white text.** o1 and o2 pass across the whole gradient, `#444444 → #5A5A5A`. The bottom
  stop is the lightest grey where white at o2 still reads 4.5:1 (4.51); the top stop was darkened from `#555555`
  so the card keeps its depth (22 levels) instead of going flat. Earlier values: `#555555 → #717171` (o2 fell to
  3.4 at the bottom) and `#555555 → #5A5A5A` (passed, nearly flat). 0.6 and o3 are not used for text on a deep card.
- **Light cards carry ink.** White on `#8E → #B4` is 3.3:1 at best; they are secondary blocks only.
- **The accent is never text on the sheet or a card.** It is a dot, a knob or a marker. On glass it
  reaches 4.4:1, enough for a mark but used as text only at ≥ 18px (not currently anywhere).
- Mockup text at 0.6 (channel sub, text buttons, unselected segment labels) is 5.5:1 on the sheet.
- Marker meaning does not depend on color: filled / outlined / dim / hatched, plus the tooltip text.

## Motion

Enter and exit: fade plus a 6px rise over 180 ms with `cubic-bezier(.2,.8,.2,1)`. Only the badge dot
animates on its own (1.6 s pulse while reading or analyzing). Hover on a marker scales it; the toggle
knob slides on press. `prefers-reduced-motion` zeroes the duration and rise and stops the pulse.

## Writing

Sentence case for buttons and titles; lowercase for chips, section labels, category names and the
product name. Buttons say what happens: *Skipping on*, *Paused*, *Undo*, *Wrong skip*, *Skip ad*,
*Don't skip fern*, *Missed an ad here*. Status reads as a state: *analyzing 2/4*, *ready · 1 to skip*,
*no ads*, *server offline*. Empty and error states say what to do next.

## Applied on main (and where the real surfaces differ from the mockup)

All five real surfaces use the system: badge, marker lane (+ tooltip), toast, popup, dashboard. The ad panel is
specified here but not built; nothing in the product opens it yet.

- **Fonts.** The faces live in `ui/fonts.css` (relative URLs: popup, dashboard, gallery) and
  `ui/fonts-content.css` (absolute `chrome-extension://` URLs for the YouTube page, where a relative URL would be
  requested from youtube.com). `tokens.css` sets `color-scheme` on `.sr-sheet`, not on `:root`, because it is
  injected into YouTube.
- **Badge.** Two states beyond the table: `busy` with the text *paused* (the server paused the job), and `none`
  with the text *nothing to skip* (segments were found, but none will be skipped). The count slot carries the
  server's error category on `error`.
- **Tooltip.** A child of the lane, not of the marker (a marker scales on hover and would stretch it), centred on
  the marker and clamped inside the player. A brand-less segment is titled by its category in sentence case and
  gets no chip (*Outro*, not *outro [outro]*).
- **Popup.** Chrome caps a popup at 600px; the board is 720. The default state fits by using 12px gaps, 36px
  rows and text buttons and 16px hero padding (overrides in `popup.css`). Four categories are always visible
  (sponsor read, sponsor integration, self-promo, like & subscribe); the other eight are behind a
  *more categories* disclosure that shows a mono chip (*2 changed*) when any of them is off its default.
  *Sponsor integration* is a static *always plays* chip: it is not a setting. The hero shows *saved today* with
  today's and all-time counts, because that is what the extension stores (no weekly, per-brand or wrong-skip
  numbers in the popup). Codes, the don't-skip list, dev mode and the version are a second pane (*Codes & more*).
  A server problem replaces the channel row's sub line with what to do (*start it: python3 server/server.py*).
- **Dashboard.** Tabs are a `.sr-seg` in the header. Beyond the mockup: ad cards (`.sr-adcard` with a hairline
  timeline, most-replayed curve, chapter ticks, `.sr-kv` facts, `.sr-inset` for the placement suggestion) and the
  insights tab (tiles + `.sr-hall` distributions). A warning is `.sr-chip--flag`, a chip with a ring dot; nothing
  is amber or red. Category swatches are gone. `?theme=light|dark` forces a theme.

## Not matched exactly (deliberate)

- The opacity ladder keeps o3 = 0.55 where the mockup uses 0.6 for secondary text; both pass on the
  sheet (4.6 vs 5.5) and one ladder is simpler. Text buttons, channel sub and unselected segment
  labels use the mockup's 0.6 literally.
- Integration markers are a hollow outline (shape encoding), where the mockup draws a solid white
  bar; the mockup's low-confidence hatch values are used as-is.
- The popup header says `second read`; the mockup predates the rename and says `sponsor skipper`, and its classes
  were `ss-` (now `sr-`).
- The ad panel floating on the player is a derived state (376 wide, deep card over the video); the
  mockup only shows the 440 board on the light sheet.
- The toast's evidence quote is clamped to two lines as specified; the mockup board lets the same
  quote run to three.
- The badge uses the board 1 glass (`rgba(28,28,28,.62)` with blur). Board 4 draws its badges on a
  flat `rgba(40,40,40,.72)` because that board has no video behind it; that value is kept only for
  the tooltip (`--sr-glass-solid`, at the mockup's .82).
- "Dashboard ↗": the bundled Geist draws its own arrow; the mockup's subset font fell back to a
  system arrow, so the glyph is slightly smaller here.
- `analysis failed` is an eighth badge state that the mockup does not show (accent dot with a dash).
- Dark-sheet variants of the popup and dashboard are derived; the mockup is light only. In dark mode
  the selected segment and the primary button flip to white, and the master toggle stays black.
