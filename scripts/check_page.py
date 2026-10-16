#!/usr/bin/env python3
"""Fail when docs/index.html or project.yml disagrees with a number in README.md.

    python3 scripts/check_page.py          # run before every commit, after scripts/regress.py

The README is the source: each number is read from it by the README's own wording, then looked for at its place on
the page (by the page's wording) and in project.yml (by field). Covered: synthetic pass rates and DEV pass, reads
missed, runs and seconds that cut content (also the per-scenario seconds the page names), median start/end error,
regression rows, speed, the lead-in caps. A number the page or project.yml states differently, or no longer states
where it should, is a failure.

Structure, before any number: the README must have every expected section heading (outside code blocks), and the page
every expected section; neither may leave a <!--, <details> or <picture> open, the README no code fence, and the page
must end with </html>. An unclosed fence or comment renders the rest of the file as code or not at all, and the
number checks would still pass on the raw text. Stdlib only.
"""
import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
problems = []
checked = 0


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def need(pattern, text, where):
    m = re.search(pattern, text, re.S)
    if not m:
        problems.append(f"{where}: not found: {pattern}")
    return m


def same(name, want, got, where):
    global checked
    checked += 1
    norm = lambda v: None if v is None else (f"{float(v):+.1f}" if re.fullmatch(r"[+\-]?\d+(\.\d+)?", str(v)) else str(v))
    if norm(want) != norm(got):
        problems.append(f"{where}: {name} is {got!r}, the README says {want!r}")


# ---------------------------------------------------------------- structure
README_HEADINGS = ["# Second Read", "## How it works", "## Coverage", "## UI", "## Evaluation",
                   "### Behavioral tests (synthetic)", "## Local setup", "## Permissions and privacy",
                   "## Project layout", "## Limitations", "## Status"]
PAGE_SECTIONS = ["why", "how", "results", "interface", "limits"]
OPENERS = (("<!--", "-->", "<!--"), (r"<details\b", r"</details\s*>", "<details>"), (r"<picture\b", r"</picture\s*>", "<picture>"))


def unclosed(lines, where):
    """Report every <!--, <details> or <picture> in these (line number, text) pairs that is never closed."""
    text = "\n".join(t for _, t in lines)
    starts = [n for n, _ in lines]
    offsets, pos = [], 0
    for _, t in lines:
        offsets.append(pos)
        pos += len(t) + 1
    line_of = lambda off: starts[max(i for i, o in enumerate(offsets) if o <= off)] if lines else 0
    m = re.search(r"<!--", text)
    while m:   # comments do not nest: each <!-- needs a --> after it
        end = text.find("-->", m.end())
        if end < 0:
            problems.append(f"{where}:{line_of(m.start())}: <!-- is never closed (everything after it is hidden)")
            break
        m = re.compile(r"<!--").search(text, end + 3)
    for open_re, close_re, name in OPENERS[1:]:
        depth = []
        for t in sorted([(o.start(), 1) for o in re.finditer(open_re, text)] + [(c.start(), -1) for c in re.finditer(close_re, text)]):
            if t[1] > 0:
                depth.append(t[0])
            elif depth:
                depth.pop()
            else:
                problems.append(f"{where}:{line_of(t[0])}: </{name[1:]} without an opening {name}")
        for off in depth:
            problems.append(f"{where}:{line_of(off)}: {name} is never closed")


def readme_structure(text, where="README.md"):
    """Walk the README as CommonMark does for fences: returns nothing, appends problems."""
    fence, fence_line, outside, headings = None, 0, [], set()
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if m and m[1][0] == fence[0] and len(m[1]) >= len(fence) and not m[2].strip():
                fence = None
            continue
        if m and not (m[1][0] == "`" and "`" in m[2]):
            fence, fence_line = m[1], n
            continue
        if re.match(r"#{1,6} ", line):
            headings.add(line.rstrip())
        outside.append((n, re.sub(r"(`+)(?!`).+?(?<!`)\1(?!`)", "", line)))   # inline code shows tags as text
    if fence:
        problems.append(f"{where}:{fence_line}: code fence {fence} is never closed (the rest of the file renders as code)")
    for h in README_HEADINGS:
        if h not in headings:
            problems.append(f"{where}: section heading missing: {h}")
    unclosed(outside, where)


def page_structure(text, where="docs/index.html"):
    # script and style bodies are not markup; blank them but keep their newlines for the line numbers
    body = re.sub(r"(<(script|style)\b[^>]*>)(.*?)(</\2>)", lambda m: m[1] + re.sub(r"[^\n]", " ", m[3]) + m[4], text, flags=re.S | re.I)
    unclosed(list(enumerate(body.splitlines(), 1)), where)
    for s in PAGE_SECTIONS:
        if not re.search(rf'<section\b[^>]*\bid="{s}"', body):
            problems.append(f'{where}: section missing: <section id="{s}">')
    if not text.rstrip().lower().endswith("</html>"):
        problems.append(f"{where}: does not end with </html> (truncated?)")


readme_structure(read("README.md"))
page_structure(read("docs/index.html"))

# ---------------------------------------------------------------- the README's numbers
readme = re.sub(r"\s+", " ", read("README.md"))
R = {}
m = need(r"\| Runs passing \| (\d+)/(\d+) \| (\d+)/(\d+) \|", readme, "README")
if m:
    R.update(pass_c=m[1], runs=m[2], pass_n=m[3])
m = need(r"\| Scripted reads missed \(of (\d+)\) \| (\d+) \(.*?\) \| (\d+) \(", readme, "README")
if m:
    R.update(reads=m[1], missed_c=m[2], missed_n=m[3])
m = need(r"\| Runs that cut content, seconds cut \| (\d+) runs, ([\d.]+) s .*?\| (\d+) runs, ([\d.]+) s ", readme, "README")
if m:
    R.update(cut_runs_c=m[1], cut_s_c=m[2], cut_runs_n=m[3], cut_s_n=m[4])
m = need(r"\| Median start / end error[^|]*\| ([+\-]?[\d.]+) s / ([+\-]?[\d.]+) s \| ([+\-]?[\d.]+) s / ([+\-]?[\d.]+) s \|", readme, "README")
if m:
    R.update(med_sc=m[1], med_ec=m[2], med_sn=m[3], med_en=m[4])
m = need(r"DEV on the same code: (\d+)/(\d+) clean, (\d+)/\d+ noisy", readme, "README")
if m:
    R.update(dev_c=m[1], dev_runs=m[2], dev_n=m[3])
for key, label in (("verified", "verified"), ("transcript_labeled", "transcript-labeled"), ("snapshot", "snapshot")):
    m = need(rf"\*\*{label}\*\* \((\d+) rows", readme, "README")
    R[key] = m[1] if m else None
if all(R.get(k) for k in ("verified", "transcript_labeled", "snapshot")):
    R["rows"] = str(int(R["verified"]) + int(R["transcript_labeled"]) + int(R["snapshot"]))
# speed: the time to the first published skip, from both speed tables ("total / first skip / calls")
speed = readme[readme.find("Speed, on a 24 GB"):readme.find("Per-call latency")]
# first table: | video | baseline total / first skip / calls | current total / first skip / calls |
first_table = speed[:speed.find("Live first visits")]
base = [int(m[1]) for m in re.finditer(r"\| `[\w-]{11}`[^|]*\| \d+ s / (\d+) s / \d+ \| ", first_table)]
now = [int(m[1]) for m in re.finditer(r"\| \d+ s / \d+ s / \d+ \| \d+ s / (\d+) s / \d+ \|", speed)]
live = [int(m[1]) for m in re.finditer(r"\| `[\w-]{11}`[^|]*\| \d+ s / (\d+) s / \d+ \| [^|]*\|", speed[speed.find("Live first visits"):])]
if base and (now or live):
    R.update(first_min=str(min(now + live)), first_max=str(max(now + live)), base_min=str(min(base)), base_max=str(max(base)))
else:
    problems.append("README: speed tables not found")
# what the page says still cuts content: per-scenario seconds from the synthetic table
R["cut_scenarios"] = sorted({float(x) for m in re.finditer(r"content cut ([\d.]+)(?: / ([\d.]+))? s", readme) for x in m.groups() if x})
# the page's "soft and question segues start N to M seconds late": rows 2 and 3 of the synthetic table
late = [int(x) for m in re.finditer(r"\| [23] \| (?:Soft|Question) segue \|[^|]*\|[^|]*\|[^|]*\| late, ad plays \((\d+)–(\d+) s", readme) for x in m.groups()]
R["late_min"], R["late_max"] = (str(min(late)), str(max(late))) if late else (None, None)
m = need(r"by at most (\d+) seconds, never before the skip", readme, "README")
R["leadin_s"] = m[1] if m else None
m = need(r"by at most (\d+) seconds\. The second rule", readme, "README")
R["break_s"] = m[1] if m else None

# ---------------------------------------------------------------- the page
page_html = read("docs/index.html")
page = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)\b.*?</\1>", " ", page_html, flags=re.S))))
P = "docs/index.html"
m = need(r"Runs passing (\d+) / (\d+) (\d+) / (\d+)", page, P)
if m:
    same("clean pass", R.get("pass_c"), m[1], P); same("runs", R.get("runs"), m[2], P); same("noisy pass", R.get("pass_n"), m[3], P)
m = need(r"Sponsor reads missed (\d+) of (\d+) (\d+) of (\d+)", page, P)
if m:
    same("clean missed", R.get("missed_c"), m[1], P); same("reads", R.get("reads"), m[2], P); same("noisy missed", R.get("missed_n"), m[3], P)
m = need(r"Runs that cut any content (\d+) · ([\d.]+) s total (\d+) · ([\d.]+) s total", page, P)
if m:
    for name, k, v in (("clean cut runs", "cut_runs_c", m[1]), ("clean cut s", "cut_s_c", m[2]), ("noisy cut runs", "cut_runs_n", m[3]), ("noisy cut s", "cut_s_n", m[4])):
        same(name, R.get(k), v, P)
m = need(r"Median start / end error ([+\-]?[\d.]+) / ([+\-]?[\d.]+) s ([+\-]?[\d.]+) / ([+\-]?[\d.]+) s", page, P)
if m:
    for name, k, v in (("clean median start", "med_sc", m[1]), ("clean median end", "med_ec", m[2]), ("noisy median start", "med_sn", m[3]), ("noisy median end", "med_en", m[4])):
        same(name, R.get(k), v, P)
m = need(r"passes (\d+) / (\d+) on the development scripts", page, P)
if m:
    same("DEV clean pass", R.get("dev_c"), m[1], P); same("DEV runs", R.get("dev_runs"), m[2], P)
m = need(r"(\d+) scripted transcripts across (\d+) situations", page, P)
if m:
    same("scripts", R.get("runs"), m[1], P)
m = need(r"Real videos regression (\d+) rows", page, P)
if m:
    same("regression rows", R.get("rows"), m[1], P)
m = need(r"(\d+) checked by ear, (\d+) read from the transcript, and (\d+) snapshots", page, P)
if m:
    same("verified rows", R.get("verified"), m[1], P); same("transcript-labeled rows", R.get("transcript_labeled"), m[2], P); same("snapshot rows", R.get("snapshot"), m[3], P)
m = need(r"(\d+)–(\d+) s to first skip", page, P)
if m:
    same("first skip, fastest", R.get("first_min"), m[1], P); same("first skip, slowest", R.get("first_max"), m[2], P)
m = need(r"Down from (\d+) to (\d+) seconds in the first build", page, P)
if m:
    same("first build, fastest", R.get("base_min"), m[1], P); same("first build, slowest", R.get("base_max"), m[2], P)
m = need(r"start (\d+) to (\d+) seconds late", page, P)
if m:
    same("late start, least", R.get("late_min"), m[1], P); same("late start, most", R.get("late_max"), m[2], P)
m = need(r"by at most (\d+) seconds; a podcast's break announcement, by rule, by at most (\d+)", page, P)
if m:
    same("lead-in cap", R.get("leadin_s"), m[1], P); same("break announcement cap", R.get("break_s"), m[2], P)
m = need(r"Still cuts content (.*?) open items", page, P)
if m:
    for x in re.findall(r"(\d+\.\d) s", m[1]):
        same(f"content-cut seconds {x}", x if float(x) in R["cut_scenarios"] else "not in the README's synthetic table", x, P)


# ---------------------------------------------------------------- project.yml (a YAML subset: mappings, lists, >- blocks)
def parse_yaml(text):
    rows = []
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw) if not raw.lstrip().startswith("#") else ""
        if line.strip():
            rows.append((len(line) - len(line.lstrip()), line.strip()))

    def scalar(v):
        v = v.strip()
        return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v

    def block(i, indent):
        """Parse rows[i:] at this indent: returns (value, next index)."""
        if rows[i][1].startswith("- "):
            out = []
            while i < len(rows) and rows[i][0] == indent and rows[i][1].startswith("- "):
                body = rows[i][1][2:]
                if re.match(r"[\w-]+:( |$)", body):   # a mapping item: re-read it as a mapping one level in
                    rows[i] = (indent + 2, body)
                    v, i = mapping(i, indent + 2)
                    out.append(v)
                else:
                    out.append(scalar(body))
                    i += 1
            return out, i
        return mapping(i, indent)

    def mapping(i, indent):
        out = {}
        while i < len(rows) and rows[i][0] == indent and not rows[i][1].startswith("- "):
            key, _, rest = rows[i][1].partition(":")
            rest = rest.strip()
            i += 1
            if rest in (">-", ">", "|"):
                parts = []
                while i < len(rows) and rows[i][0] > indent:
                    parts.append(rows[i][1])
                    i += 1
                out[key] = " ".join(parts)
            elif rest:
                out[key] = scalar(rest)
            elif i < len(rows) and rows[i][0] > indent:
                out[key], i = block(i, rows[i][0])
            else:
                out[key] = None
        return out, i

    return block(0, 0)[0]


Y = "project.yml"
y = parse_yaml(read(Y))
syn = ((y.get("results") or {}).get("synthetic") or {})
for side, suffix in (("clean", "c"), ("noisy", "n")):
    s = syn.get(side) or {}
    for field, key in (("pass", f"pass_{suffix}"), ("runs", "runs"), ("missed", f"missed_{suffix}"), ("reads", "reads"),
                       ("cut_runs", f"cut_runs_{suffix}"), ("cut_s", f"cut_s_{suffix}"),
                       ("median_start_s", f"med_s{suffix}"), ("median_end_s", f"med_e{suffix}")):
        same(f"results.synthetic.{side}.{field}", R.get(key), s.get(field), Y)
dev = syn.get("dev_pass") or {}
same("results.synthetic.dev_pass.clean", R.get("dev_c"), dev.get("clean"), Y)
same("results.synthetic.dev_pass.noisy", R.get("dev_n"), dev.get("noisy"), Y)
same("results.synthetic.dev_pass.runs", R.get("dev_runs"), dev.get("runs"), Y)
same("results.synthetic.scripts", R.get("runs"), syn.get("scripts"), Y)
rr = (y.get("results") or {}).get("regression_rows") or {}
for field, key in (("verified", "verified"), ("transcript_labeled", "transcript_labeled"), ("snapshot", "snapshot"), ("total", "rows")):
    same(f"results.regression_rows.{field}", R.get(key), rr.get(field), Y)
sp = (y.get("results") or {}).get("speed") or {}
for field, key in (("first_skip_s_min", "first_min"), ("first_skip_s_max", "first_max"),
                   ("first_build_first_skip_s_min", "base_min"), ("first_build_first_skip_s_max", "base_max")):
    same(f"results.speed.{field}", R.get(key), sp.get(field), Y)
for g in (y.get("results") or {}).get("scenario_groups") or []:
    for x in re.findall(r"(\d+\.\d) s", g.get("text") or ""):
        same(f"scenario_groups content-cut seconds {x}", x if float(x) in R["cut_scenarios"] else "not in the README's synthetic table", x, Y)
steps = " ".join(s.get("text", "") for s in y.get("steps") or [])
m = need(r"by at most (\d+) seconds; a podcast's break announcement, by rule, by at most (\d+)", steps, Y)
if m:
    same("steps lead-in cap", R.get("leadin_s"), m[1], Y); same("steps break announcement cap", R.get("break_s"), m[2], Y)
same("results.real_heldout_status", "not run" if "The held-out evaluation has not been run" in readme else "run", (y.get("results") or {}).get("real_heldout_status"), Y)

if problems:
    print("check_page: FAILED")
    for p in problems:
        print("  " + p)
    sys.exit(1)
print(f"check_page: structure ok ({len(README_HEADINGS)} README headings, {len(PAGE_SECTIONS)} page sections, nothing left open); "
      f"{checked} numbers agree between README.md, docs/index.html and project.yml")
