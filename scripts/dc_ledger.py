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
MARK = "<!-- dc-ledger: {canvas} -->"


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
    """The screen ids of an app board, in order."""
    return list(dict.fromkeys(re.findall(r'<sc-if value="\{\{is\.(\w+)\}\}"', board_html)))


def design_hash(project, entry):
    """The hash of what the canvas draws for an entry: its screen's fragment, or the whole
    board. None when the board or screen is gone."""
    path = Path(project) / entry["board"]
    if not path.exists():
        return None
    text = path.read_text()
    if entry.get("screen"):
        try:
            text, _ = extract_element(text, 'value="{{is.%s}}"' % entry["screen"])
        except ValueError:
            return None
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


def load(path):
    path = Path(path)
    led = json.loads(path.read_text())
    ids = [e["id"] for e in led["entries"]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: entry ids must be distinct")
    return led


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
    project = here / led.get("project", "out/project")
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
        raise SystemExit(f"no such entries: {unknown}")
    for e in led["entries"]:
        if e["id"] in ids:
            r = by[e["id"]]
            if r["missing"]:
                raise SystemExit(f"{e['id']}: the board or screen is missing; build the canvas first")
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
    lines = [MARK.format(canvas=canvas), "",
             f"The design ledger for **{canvas}** reads {len(flagged)} screen(s) out of step with the code "
             f"at `{ref_sha[:12]}`. {lead}", ""]
    if led.get("artifact"):
        lines += [f"Canvas: {led['artifact']}", ""]
    lines += ["| Entry | Status | What it means | Last matched |", "|---|---|---|---|"]
    for r in flagged:
        lines.append(f"| `{r['id']}` | {r['status']} | {ACTION[r['status']]} | "
                     f"{'`' + r['built_at'][:12] + '`' if r['built_at'] else 'never'} |")
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
    return [f"{r['id']}: {r['status']}: {FIX[r['status']].format(id=r['id'])}"
            + (" (board or screen missing)" if r["missing"] else "")
            for r in rows if r["flagged"]]


def summary_markdown(led, rows):
    """The status table as GitHub-flavoured markdown, for $GITHUB_STEP_SUMMARY."""
    gate = led.get("gate", {}).get("state", "open")
    flagged = sum(r["flagged"] for r in rows)
    lines = [f"### Design ledger: {led['canvas']}", "",
             f"{flagged} flagged of {len(rows)} (gate {gate}).", "",
             "| Entry | Status | Flagged | Last matched |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['id']}` | {r['status']}{' (board or screen missing)' if r['missing'] else ''} | "
                     f"{'yes' if r['flagged'] else ''} | "
                     f"{'`' + r['built_at'][:12] + '`' if r['built_at'] else 'never'} |")
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
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=input).stdout


def find_issue(repo, canvas):
    """The one issue this ledger keeps, open or closed: newest first, matched by its marker."""
    title = f"Design drift: {canvas}"
    out = json.loads(_gh("issue", "list", "--repo", repo, "--state", "all", "--search", f'"{title}" in:title',
                         "--json", "number,title,state,body", "--limit", "50"))
    mark = MARK.format(canvas=canvas)
    hits = sorted((i for i in out if i["title"] == title and mark in (i.get("body") or "")),
                  key=lambda i: -i["number"])
    return hits[0] if hits else None


def flag(ledger_path, ref="HEAD", build=False, dry_run=False, gh=None):
    """Open, update, reopen or close the ledger's one issue. Returns (action, number|None)."""
    led, rows = report(ledger_path, ref, build)
    cfg = led.get("issue") or {}
    repo = cfg["repo"]
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
                f"Every screen in the {canvas} ledger agrees with the code again.")
            return ("closed", cur["number"])
        return ("none", cur and cur["number"])
    tmp = Path(ledger_path).resolve().parent / ".dc-ledger-issue.md"
    tmp.write_text(body)
    try:
        if cur:
            if cur["state"] != "OPEN":
                _gh("issue", "reopen", str(cur["number"]), "--repo", repo)
            if (cur.get("body") or "") != body:
                _gh("issue", "edit", str(cur["number"]), "--repo", repo, "--body-file", str(tmp))
            return ("updated", cur["number"])
        args = ["issue", "create", "--repo", repo, "--title", f"Design drift: {canvas}", "--body-file", str(tmp)]
        for lab in cfg.get("labels", []):
            args += ["--label", lab]
        url = _gh(*args).strip()
        return ("opened", int(url.rsplit("/", 1)[-1]))
    finally:
        tmp.unlink(missing_ok=True)


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
    if a.cmd == "screens":
        print("\n".join(screens(Path(a.board).read_text())))
        return 0
    if a.cmd == "status":
        led, rows = report(a.ledger, a.ref, a.build)
        if a.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"{'!' if r['flagged'] else ' '} {r['status']:<13} {r['id']}"
                      + ("  (board or screen missing)" if r["missing"] else ""))
            counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUSES}
            print(f"{led['canvas']}: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
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
