"""The design ledger: which canvas screen specifies which code, and whether they still agree.

Logan, 25 Sep 2026: "sometimes we change deisgns and forget to get it into code sometimes the
other way around" (agent-infrastructure#1804). A product keeps one ledger per canvas, beside
its generator (`design/<canvas>/ledger.json`):

    {"canvas": "Portal HQ", "artifact": "https://claude.ai/artifact/…",
     "project": "out/project", "build": "python3 gen.py",
     "gate": {"state": "open", "ref": "owner/repo#514"},
     "issue": {"repo": "owner/repo", "labels": ["enhancement", "P2-medium"]},
     "entries": [{"id": "desktop/home", "board": "R1-Desktop.dc.html", "screen": "home",
                  "code": ["apps/web/app/pages/index.vue"], "built": null}]}

An entry is one screen of an app board (`screen`), or a whole board such as a kit piece.
`built` is {design, code, at}: the design hash and code hash when the build last matched
the canvas, and the commit it matched at. The check compares both directions:

  in-sync        neither side moved since `built`
  not-built      the design changed (or was never built); the code has not caught up
  design-stale   the code changed; the canvas was not amended
  diverged       both moved

Who leads is the gate's call (Logan, 25 Sep 2026: "Design until the gate"). While the gate
is open the canvas leads, every screen is expected to be unbuilt, and only screens that
were built once raise a flag. After it clears, shipped code leads and every mismatch
flags: build a changed design, or amend the canvas to the shipped code. `mark-built`
records a match after either.

Flags go to one issue per canvas on the product repo, opened or updated in place and
closed when everything agrees.

    python3 dc_ledger.py status design/portal/ledger.json [--build] [--json] [--check] [--github-summary]
    python3 dc_ledger.py mark-built design/portal/ledger.json desktop/home [...] | --all
    python3 dc_ledger.py flag design/portal/ledger.json [--dry-run]
    python3 dc_ledger.py screens out/project/R1-Desktop.dc.html   # ids for new entries

`status --check` exits 1 while any entry is flagged, printing how to fix each one; CI runs it
on pull requests. `--github-summary` also appends the table to `$GITHUB_STEP_SUMMARY`.
A malformed ledger (a string `code`, a `gate.state` other than `open` or `cleared`, a
`built` without its hashes) is an error naming the entry, with exit 2, never a quiet read.

CI never runs `build`, so the boards `project` points at must be committed: a gitignored,
generated `project` reads every entry as missing, which stays red once anything is built.

Ledger text is untrusted: what reaches the log cannot start a workflow command, and what
reaches markdown cannot break a table, the issue marker, or mention anyone.

This file is stdlib-only so product CI can run it alone: the reusable `design-ledger`
workflow in narduk-enterprises/workflows carries a byte copy, and agent-infrastructure's
`scripts/check-dc-ledger-parity` keeps the two identical.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATUSES = ("in-sync", "not-built", "design-stale", "diverged")
GATE_STATES = ("open", "cleared")
MARK = "<!-- dc-ledger: {canvas} -->"


class LedgerError(ValueError):
    """A malformed ledger, or one `flag` cannot act on. main() reports it and exits 2."""


class GhError(RuntimeError):
    """A failed gh call, carrying gh's own stderr."""


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def log_safe(text):
    """Ledger text as one inert log line: control characters and newlines become spaces,
    and `::` is broken so it can never open a workflow command."""
    return _CONTROL.sub(" ", str(text)).replace("::", ":\u200b:")


def md(text, cell=False):
    """Ledger text for markdown: one line with `@` mentions broken by a zero-width joiner.
    A table cell (always a code span here) escapes `|` and replaces backticks so the span
    holds; running text escapes `<` so it cannot open raw HTML or a comment."""
    text = _CONTROL.sub(" ", str(text)).replace("@", "@\u200d")
    return text.replace("|", "\\|").replace("`", "'") if cell else text.replace("<", "&lt;")


def marker(canvas):
    """The hidden issue marker. The canvas name loses anything that could close the
    comment early or open another, until none is left, and its mentions are broken."""
    name = _CONTROL.sub(" ", str(canvas)).replace("@", "@\u200d")
    while True:
        cut = name.replace("-->", "").replace("--!>", "").replace("<!--", "")
        if cut == name:
            return MARK.format(canvas=name)
        name = cut


def issue_title(canvas):
    return f"Design drift: {_CONTROL.sub(' ', str(canvas))}"


# Inlined from dc_canvas.py so this file needs nothing beside it; keep the two in step.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


def extract_element(fragment, marker):
    """(element, rest): the balanced element whose start tag contains `marker`, and the
    fragment with that element cut out."""
    k = fragment.find(marker)
    if k < 0:
        raise ValueError(f"marker not found: {marker!r}")
    i = fragment.rfind("<", 0, k)
    m = re.match(r"<([a-zA-Z][\w-]*)", fragment[i:])
    if not m or ">" in fragment[i:k]:
        raise ValueError(f"marker is not inside a start tag: {marker!r}")
    tag = m.group(1)
    if tag.lower() in VOID:
        j = fragment.index(">", i) + 1
        return fragment[i:j], fragment[:i] + fragment[j:]
    depth = 0
    for t in re.finditer(r'<(/?)%s\b(?:[^>"]|"[^"]*")*?(/?)>' % re.escape(tag), fragment[i:]):
        if t.group(2):
            continue
        depth += -1 if t.group(1) else 1
        if depth == 0:
            j = i + t.end()
            return fragment[i:j], fragment[:i] + fragment[j:]
    raise ValueError(f"<{tag}> at the marker is never closed")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def screens(board_html):
    """The screen ids of an app board, in order. A screen is a box behind `is.<id>`, or, on a
    board with a later round (`dc_app.py round`), behind `r2.<id>`; round-2-only screens
    follow the round-1 ones."""
    found = re.findall(r'<sc-if value="\{\{(?:is|r2)\.(\w+)\}\}"', board_html)
    return list(dict.fromkeys(found))


def design_hash(project, entry):
    """The hash of what the canvas draws for an entry: its screen's fragment, or the whole
    board. None when the board or screen is gone."""
    path = Path(project) / entry["board"]
    if not path.exists():
        return None
    text = path.read_text()
    if entry.get("screen"):
        # The round-1 box (`is.`) and the later round's box (`r2.`), whichever the board has:
        # a board with no `r2.` box hashes exactly as it did before.
        boxes = []
        for key in ("is", "r2"):
            try:
                box, _ = extract_element(text, 'value="{{%s.%s}}"' % (key, entry["screen"]))
            except ValueError:
                continue
            boxes.append(box)
        if not boxes:
            return None
        text = "".join(boxes)
    return _sha(text)


def code_hash(repo, paths, ref="HEAD"):
    """The hash of the entry's code at `ref`: each path's git object id (a directory's is
    its tree), so any change under it counts. A missing path hashes as missing."""
    ids = []
    for p in sorted(paths):
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}:{p}"],
                           capture_output=True, text=True)
        ids.append(f"{p}={r.stdout.strip() if r.returncode == 0 else 'missing'}")
    return _sha("\n".join(ids))


def head(repo, ref="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True,
                          capture_output=True, text=True).stdout.strip()


def status(entry, design, code):
    b = entry.get("built")
    if not b:
        return "not-built"
    d, c = design != b["design"], code != b["code"]
    return "diverged" if d and c else "not-built" if d else "design-stale" if c else "in-sync"


def _str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _kind(value):
    return "missing" if value is None else f"a {type(value).__name__}" if not isinstance(value, str) else repr(value)


def validate(led, path):
    """Check the ledger's shape once, at load. A shape that would read wrong (a string
    `code` hashed a character at a time, a gate typo read as open) is an error."""
    def bad(msg):
        raise LedgerError(f"{path}: {msg}")
    if not isinstance(led, dict):
        bad("the ledger must be a JSON object")
    if not isinstance(led.get("canvas"), str) or not led["canvas"].strip():
        bad("`canvas` must be a non-empty string")
    for key in ("project", "artifact", "build"):
        if key in led and not isinstance(led[key], str):
            bad(f"`{key}` must be a string")
    if "gate" in led:
        gate = led["gate"]
        if not isinstance(gate, dict) or gate.get("state") not in GATE_STATES:
            bad(f"`gate.state` must be \"open\" or \"cleared\" "
                f"(got {_kind(gate.get('state') if isinstance(gate, dict) else gate)})")
    if led.get("issue") is not None:
        issue = led["issue"]
        if not isinstance(issue, dict):
            bad("`issue` must be an object with `repo` (owner/name) and optional `labels`")
        if "repo" in issue and not (isinstance(issue["repo"], str) and re.fullmatch(r"[\w.-]+/[\w.-]+", issue["repo"])):
            bad(f"`issue.repo` must be owner/name (got {_kind(issue['repo'])})")
        if "labels" in issue and not _str_list(issue["labels"]):
            bad("`issue.labels` must be a list of strings")
    entries = led.get("entries")
    if not isinstance(entries, list):
        bad("`entries` must be a list")
    seen = set()
    for n, e in enumerate(entries):
        if not isinstance(e, dict):
            bad(f"entry {n} must be an object")
        if not isinstance(e.get("id"), str) or not e["id"]:
            bad(f"entry {n}: `id` must be a non-empty string")
        where = f"entry {e['id']!r}"
        if e["id"] in seen:
            bad(f"{where}: entry ids must be distinct")
        seen.add(e["id"])
        if not isinstance(e.get("board"), str) or not e["board"]:
            bad(f"{where}: `board` must be a non-empty string")
        if e.get("screen") is not None and not isinstance(e["screen"], str):
            bad(f"{where}: `screen` must be a string or null")
        if "code" in e and not _str_list(e["code"]):
            bad(f"{where}: `code` must be a list of paths (got {_kind(e['code'])})")
        built = e.get("built")
        if built is not None and not (isinstance(built, dict)
                                      and all(isinstance(built.get(k), str) for k in ("design", "code", "at"))):
            bad(f"{where}: `built` must be null or {{design, code, at}} strings; run mark-built again")
    return led


def load(path):
    path = Path(path)
    try:
        led = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{path}: not valid JSON: {exc}") from exc
    return validate(led, path)


def project_dir(ledger_path, led):
    return Path(ledger_path).resolve().parent / led.get("project", "out/project")


def repo_root(path):
    return Path(subprocess.run(["git", "-C", str(Path(path).resolve().parent), "rev-parse", "--show-toplevel"],
                               check=True, capture_output=True, text=True).stdout.strip())


def report(ledger_path, ref="HEAD", build=False):
    """[{id, status, flagged, design, code, built_at, missing}] for every entry."""
    ledger_path = Path(ledger_path)
    led = load(ledger_path)
    here = ledger_path.resolve().parent
    if build and led.get("build"):
        subprocess.run(led["build"], shell=True, cwd=here, check=True, stdout=subprocess.DEVNULL)
    project = project_dir(ledger_path, led)
    repo = repo_root(ledger_path)
    gate_open = led.get("gate", {}).get("state", "open") != "cleared"
    rows = []
    for e in led["entries"]:
        d, c = design_hash(project, e), code_hash(repo, e.get("code", []), ref)
        st = status(e, d, c)
        rows.append({"id": e["id"], "status": st, "design": d, "code": c,
                     "built_at": (e.get("built") or {}).get("at"),
                     "flagged": st != "in-sync" and (not gate_open or bool(e.get("built"))),
                     "missing": d is None})
    return led, rows


def mark_built(ledger_path, ids, ref="HEAD"):
    """Record that the build matches the canvas now, for these entries."""
    ledger_path = Path(ledger_path)
    led, rows = report(ledger_path, ref)
    at = head(repo_root(ledger_path), ref)
    by = {r["id"]: r for r in rows}
    unknown = [i for i in ids if i not in by]
    if unknown:
        raise SystemExit(log_safe(f"no such entries: {unknown}"))
    for e in led["entries"]:
        if e["id"] in ids:
            r = by[e["id"]]
            if r["missing"]:
                raise SystemExit(log_safe(f"{e['id']}: the board or screen is missing; build the canvas first"))
            e["built"] = {"design": r["design"], "code": r["code"], "at": at}
    ledger_path.write_text(json.dumps(led, indent=2, ensure_ascii=False) + "\n")
    return len(ids)


ACTION = {
    "not-built": "the canvas changed and the code has not followed: build it",
    "design-stale": "the code changed and the canvas was not amended",
    "diverged": "both moved since they last agreed",
}


def issue_body(led, rows, ref_sha):
    canvas = led["canvas"]
    flagged = [r for r in rows if r["flagged"]]
    gate = led.get("gate", {})
    lead = ("The gate is open, so the canvas leads: bring the code to it."
            if gate.get("state", "open") != "cleared" else
            "The gate has cleared, so shipped code leads: build a changed design, or amend the canvas to the shipped code.")
    lines = [marker(canvas), "",
             f"The design ledger for **{md(canvas)}** reads {len(flagged)} screen(s) out of step with the code "
             f"at `{ref_sha[:12]}`. {lead}", ""]
    if led.get("artifact"):
        lines += [f"Canvas: {md(led['artifact'])}", ""]
    lines += ["| Entry | Status | What it means | Last matched |", "|---|---|---|---|"]
    for r in flagged:
        lines.append(f"| `{md(r['id'], cell=True)}` | {r['status']} | {ACTION[r['status']]} | "
                     f"{'`' + md(r['built_at'][:12], cell=True) + '`' if r['built_at'] else 'never'} |")
    unbuilt = sum(1 for r in rows if r["status"] == "not-built" and not r["built_at"])
    if unbuilt and gate.get("state", "open") != "cleared":
        lines += ["", f"{unbuilt} more screen(s) were never built; with the gate open that is expected and not flagged."]
    lines += ["", "After fixing either side, record the match: `python3 dc_ledger.py mark-built <ledger> <id>…`.",
              "This issue is kept by `claude-design-ops/scripts/dc_ledger.py flag`: it is edited in place, never duplicated, "
              "and closed when every screen agrees."]
    return "\n".join(lines) + "\n"


FIX = {
    "not-built": "build it, then `mark-built {id}`",
    "design-stale": "amend the canvas to the code (or confirm the code matches it), then `mark-built {id}`",
    "diverged": "build the changed design and amend the canvas to the changed code, then `mark-built {id}`",
}


def check_lines(rows):
    """One fix line per flagged entry."""
    return [log_safe(f"{r['id']}: {r['status']}: {FIX[r['status']].format(id=r['id'])}"
                     + (" (board or screen missing)" if r["missing"] else ""))
            for r in rows if r["flagged"]]


def summary_markdown(led, rows):
    """The status table as GitHub-flavoured markdown, for $GITHUB_STEP_SUMMARY."""
    gate = led.get("gate", {}).get("state", "open")
    flagged = sum(r["flagged"] for r in rows)
    lines = [f"### Design ledger: {md(led['canvas'])}", "",
             f"{flagged} flagged of {len(rows)} (gate {gate}).", "",
             "| Entry | Status | Flagged | Last matched |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{md(r['id'], cell=True)}` | {r['status']}{' (board or screen missing)' if r['missing'] else ''} | "
                     f"{'yes' if r['flagged'] else ''} | "
                     f"{'`' + md(r['built_at'][:12], cell=True) + '`' if r['built_at'] else 'never'} |")
    return "\n".join(lines) + "\n\n"


def write_github_summary(led, rows):
    """Append the table to $GITHUB_STEP_SUMMARY when it is set. Returns whether it wrote."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(summary_markdown(led, rows))
    return True


def _gh(*args, input=None):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, input=input)
    if r.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed (exit {r.returncode}): "
                      f"{r.stderr.strip() or r.stdout.strip() or 'no output'}")
    return r.stdout


def find_issue(repo, canvas):
    """The one issue this ledger keeps, open or closed: newest first, matched by its marker."""
    title = issue_title(canvas)
    out = json.loads(_gh("issue", "list", "--repo", repo, "--state", "all", "--search", f'"{title}" in:title',
                         "--json", "number,title,state,body", "--limit", "50"))
    mark = marker(canvas)
    hits = sorted((i for i in out if i["title"] == title and mark in (i.get("body") or "")),
                  key=lambda i: -i["number"])
    return hits[0] if hits else None


def issue_repo(led, ledger_path):
    """The repository that keeps the drift issue. In Actions it must be the caller's own:
    the workflow's token can write no other."""
    repo = (led.get("issue") or {}).get("repo")
    if not repo:
        raise LedgerError(f"{ledger_path}: `issue.repo` is missing; flag needs the owner/name "
                          "repository that keeps the drift issue")
    caller = os.environ.get("GITHUB_REPOSITORY")
    if caller and caller.lower() != repo.lower():
        raise LedgerError(f"{ledger_path}: `issue.repo` is {repo} but this run is in {caller}, and the "
                          f"workflow's token can write only {caller}; set `issue.repo` to {caller}")
    return repo


def flag(ledger_path, ref="HEAD", build=False, dry_run=False, gh=None):
    """Open, update, reopen or close the ledger's one issue. Returns (action, number|None)."""
    repo = issue_repo(load(ledger_path), ledger_path)
    led, rows = report(ledger_path, ref, build)
    cfg = led.get("issue") or {}
    canvas = led["canvas"]
    flagged = [r for r in rows if r["flagged"]]
    body = issue_body(led, rows, head(repo_root(ledger_path), ref))
    finder = gh or find_issue
    cur = finder(repo, canvas)
    if dry_run:
        return ("would-" + ("open" if flagged and not cur else "update" if flagged else "close" if cur else "none"),
                cur and cur["number"])
    if not flagged:
        if cur and cur["state"] == "OPEN":
            _gh("issue", "close", str(cur["number"]), "--repo", repo, "--comment",
                f"Every screen in the {md(canvas)} ledger agrees with the code again.")
            return ("closed", cur["number"])
        return ("none", cur and cur["number"])
    # The body goes to gh on stdin: nothing is written inside the checkout, where a committed
    # symlink could point the write at any file the runner user owns.
    if cur:
        if cur["state"] != "OPEN":
            _gh("issue", "reopen", str(cur["number"]), "--repo", repo)
        if (cur.get("body") or "") != body:
            _gh("issue", "edit", str(cur["number"]), "--repo", repo, "--body-file", "-", input=body)
        return ("updated", cur["number"])
    args = ["issue", "create", "--repo", repo, "--title", issue_title(canvas), "--body-file", "-"]
    for lab in cfg.get("labels", []):
        args += ["--label", lab]
    url = _gh(*args, input=body).strip()
    return ("opened", int(url.rsplit("/", 1)[-1]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="The design ledger: canvas screens against the code they specify.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("ledger")
    s.add_argument("--ref", default="HEAD")
    s.add_argument("--build", action="store_true", help="run the ledger's build command first")
    s.add_argument("--json", action="store_true")
    s.add_argument("--check", action="store_true", help="exit 1 when any entry is flagged, with a fix line for each")
    s.add_argument("--github-summary", action="store_true", help="append the table to $GITHUB_STEP_SUMMARY when set")
    m = sub.add_parser("mark-built")
    m.add_argument("ledger")
    m.add_argument("ids", nargs="*")
    m.add_argument("--all", action="store_true")
    m.add_argument("--ref", default="HEAD")
    f = sub.add_parser("flag")
    f.add_argument("ledger")
    f.add_argument("--ref", default="HEAD")
    f.add_argument("--build", action="store_true")
    f.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("screens")
    c.add_argument("board")
    a = ap.parse_args(argv)
    try:
        return _run(a)
    except (LedgerError, GhError) as exc:
        print(f"dc-ledger: error: {log_safe(exc)}", file=sys.stderr)
        return 2


def _run(a):
    if a.cmd == "screens":
        print("\n".join(screens(Path(a.board).read_text())))
        return 0
    if a.cmd == "status":
        led, rows = report(a.ledger, a.ref, a.build)
        project = project_dir(a.ledger, led)
        if not project.is_dir():
            print(log_safe(f"hint: the ledger's project directory {project} is not in this checkout, so every "
                           "entry reads missing. CI never runs `build`: commit the canvas boards, not a gitignored "
                           "build output."), file=sys.stderr)
        if a.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"{'!' if r['flagged'] else ' '} {r['status']:<13} {log_safe(r['id'])}"
                      + ("  (board or screen missing)" if r["missing"] else ""))
            counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUSES}
            print(f"{log_safe(led['canvas'])}: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
                  + f"; {sum(r['flagged'] for r in rows)} flagged (gate {led.get('gate', {}).get('state', 'open')})")
        if a.github_summary:
            write_github_summary(led, rows)
        if a.check:
            fixes = check_lines(rows)
            for line in fixes:
                print(f"fix: {line}", file=sys.stderr)
            return 1 if fixes else 0
        return 0
    if a.cmd == "mark-built":
        led = load(a.ledger)
        ids = [e["id"] for e in led["entries"]] if a.all else a.ids
        print(f"marked {mark_built(a.ledger, ids, a.ref)} entries built")
        return 0
    action, num = flag(a.ledger, a.ref, a.build, a.dry_run)
    print(f"{action}{' #' + str(num) if num else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
