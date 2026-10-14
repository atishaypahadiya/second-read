# Synthetic transcript format

One JSON file per variant: `scripts/synth/<split>/s<NN>_<split letter><n>.json`, e.g. `held/s05_h2.json`,
`dev/s05_d1.json`. Writers produce the *text* and the *truth tags*; `build.py` turns it into caption lines with
realistic timings (clean: sentence lines with word timing; noisy: lowercase, no punctuation, swaps applied, ~8 s
lines, word timing removed).

```json
{
  "id": "s05_d1",
  "scenario": 5,
  "split": "dev",
  "lang": "en",
  "title": "I Tried Living on $10 a Day for a Week",
  "creator": "Maya Lin",
  "description": "Full description text, newlines as \n. Sponsor lines here if the scenario calls for them.",
  "paid_promotion": true,
  "sentences": [
    {"t": "Okay so day three, and I am already regretting this."},
    {"t": "Before we get to the grocery run, a word from today's sponsors.", "seg": "A"},
    {"t": "This video is sponsored by Nomad Bank.", "seg": "A"},
    {"t": "...", "seg": "A"},
    {"t": "And our second sponsor is Quillbox.", "seg": "B"},
    {"t": "Anyway, the grocery store.", "gap": 1.2}
  ],
  "segments": {
    "A": {"expect": "skip", "category": "sponsor", "brand": "Nomad Bank"},
    "B": {"expect": "skip", "category": "sponsor", "brand": "Quillbox"}
  },
  "noisy": {"swaps": [["Quillbox", "quill box"], ["Nomad", "no mad"]]}
}
```

- `sentences`: in spoken order, each one sentence ending in `.`, `?` or `!` (spoken style, not prose). `seg` tags the
  sentence as part of a truth segment; untagged sentences are content. A segment's sentences are contiguous.
  `gap` (seconds, optional) is silence before the sentence (default: a natural 0.2–0.6 s).
- `segments[*].expect`: `skip` | `keep` | `label` | `none` | `noskip` (see `taxonomy.json`). `category` is one of
  sponsor, selfpromo, membership, affiliate, charity, giveaway, crosspromo. `brand` as the creator would write it.
- `paid_promotion`: YouTube's "Includes paid promotion" flag: true, false, or null (not reported).
- `lang`: `en`, or `hi` for scenario 18, where every sentence also has `"en": "<English translation>"` (YouTube's
  auto-translation, used as the analysis track).
- `noisy.swaps`: 1–2 `[from, to]` replacements that auto-captions would plausibly make (a brand or an unusual word
  misheard). Applied case-insensitively to the noisy twin only.
- Length: about 500–700 words (3.5–5 minutes) unless the scenario says otherwise. Fictional or real brands are both
  fine; vary brands, wording, creators and topics across variants.
