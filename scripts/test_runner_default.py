#!/usr/bin/env python3
"""Behaviour tests for the visibility-gated default runner route.

Row 18 Q3 (Logan, 2026-09-18, "Flip the default (Recommended)"): a caller that
passes NO runner input used to land on GitHub-hosted `ubuntu-latest` whether it
was private or public, which put every private caller that forgot the input on
hosted capacity -- drift from agent-infrastructure docs/standards/CI-RUNNER-POLICY.md Sec 1. The default
input is now EMPTY and each `runs-on:` resolves it per run:

  explicit input                       -> that input, exactly as before (on
                                          nuxt-cloudflare's ordinary route a
                                          SET vars.CI_LINUX_RUNNER on a private
                                          caller wins over it, W8 item 8)
  empty + repository.private == true   -> the linux-ci organization-group route
  empty + anything else                -> "ubuntu-latest" (Sec 3: public repos)

These tests extract every shipped `runs-on:` expression from the callables and
EVALUATE it with a small GitHub-expression interpreter over a matrix of caller
visibility x runner input x org variables, so they cannot drift from the YAML.
They prove:

  1. a public or visibility-unknown caller never resolves to a self-hosted or
     Blacksmith route when it passes nothing or passes "ubuntu-latest", even
     with BLACKSMITH_RUNNERS_ENABLED=true (that variable is visible to public
     repos);
  2. a private caller passing nothing lands on the linux-ci group route, group
     AND labels, and every such literal in the repo is the same string;
  3. an explicit caller value resolves exactly as the pre-flip expression did
     (parity, derived by substituting the old bare `inputs.<name>` back in);
  4. Blacksmith overflow and CI_LIGHTWEIGHT_RUNNER keep working.

nuxt-cloudflare.yml joined CALLABLES after workflows#110 added its `checks`
job, so all ten of its runner-reading `runs-on:` lines are covered.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# The fleet manifest's linux-ci organization route (fleet
# Config/github-runner-fleet.json: group `linux-ci`, runnerClass `linux-ci`
# labels). Verified against the manifest when this test was written; the test
# asserts the callables agree with each other and with this one value.
LINUX_CI = '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'
DEFAULT_TAIL = f"github.event.repository.private == true && '{LINUX_CI}' || '\"ubuntu-latest\"'"
# The ORDINARY private route (nuxt-cloudflare.yml's Build/Checks/Extra gate/
# Deploy dry run/Fast) also honours a per-repo `vars.CI_LINUX_RUNNER` (ci-reset
# W4 addendum, 2026-09-28): a controller sets or deletes that repo variable to
# move one burst repo's ordinary CI. Since ci-reset W10 (W8 item 8) a SET
# variable wins over an explicit `inputs.runner` too, so the Blacksmith
# overflow can engage on a caller that pins a runner. Private callers only;
# unset is identical to the pre-variable expression, which check_callable
# proves by evaluating both.
LINUX_VAR_SITES = {"nuxt-cloudflare.yml": {"build", "checks", "extra-gate", "deploy-dry-run", "fast", "fast-escalable"}}

# callable -> the input that carries the caller's route
CALLABLES = {
    "apple.yml": "lint-runner",
    "docs-governance.yml": "runner",
    "node-library.yml": "runner",
    "python-data.yml": "runner",
    "nuxt-cloudflare.yml": "runner",
    "red-main-listener.yml": "runner",
    "flake-digest.yml": "runner",
    "design-ledger.yml": "runs-on",
}
BLACKSMITH_LABEL = "blacksmith-2vcpu-ubuntu-2404"


# --------------------------------------------------------------------------
# Minimal GitHub Actions expression interpreter (the subset these files use).
# --------------------------------------------------------------------------
TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>\|\||&&|==|!=|!|\(|\)|,)"
    r"|(?P<num>-?\d+(?:\.\d+)?)|(?P<id>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*))"
)


def tokenize(src: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    src = src.strip()
    while pos < len(src):
        m = TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot tokenize at {src[pos:pos + 30]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def truthy(v) -> bool:
    return not (v is None or v is False or v == "" or v == 0)


def to_num(v):
    if v is None or v is False:
        return 0
    if v is True:
        return 1
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v) if v.strip() else 0
    except (ValueError, AttributeError):
        return float("nan")


def loose_eq(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if type(a) is type(b):
        return a == b
    return to_num(a) == to_num(b)


class Evaluator:
    def __init__(self, ctx: dict):
        self.ctx = ctx

    def run(self, src: str):
        self.toks, self.i = tokenize(src), 0
        v = self.or_()
        assert self.i == len(self.toks), f"trailing tokens in {src!r}"
        return v

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def eat(self, val):
        assert self.peek()[1] == val, f"expected {val} got {self.peek()}"
        self.i += 1

    def or_(self):
        v = self.and_()
        while self.peek()[1] == "||":
            self.i += 1
            r = self.and_()
            v = v if truthy(v) else r
        return v

    def and_(self):
        v = self.cmp()
        while self.peek()[1] == "&&":
            self.i += 1
            r = self.cmp()
            v = r if truthy(v) else v
        return v

    def cmp(self):
        v = self.unary()
        if self.peek()[1] in ("==", "!="):
            op = self.peek()[1]
            self.i += 1
            r = self.unary()
            eq = loose_eq(v, r)
            v = eq if op == "==" else not eq
        return v

    def unary(self):
        if self.peek()[1] == "!":
            self.i += 1
            return not truthy(self.unary())
        return self.primary()

    def primary(self):
        kind, val = self.peek()
        self.i += 1
        if kind == "str":
            return val[1:-1].replace("''", "'")
        if kind == "num":
            return float(val)
        if val == "(":
            v = self.or_()
            self.eat(")")
            return v
        if kind == "id":
            if val in ("true", "false"):
                return val == "true"
            if val == "null":
                return None
            if self.peek()[1] == "(":
                self.i += 1
                args = []
                while self.peek()[1] != ")":
                    args.append(self.or_())
                    if self.peek()[1] == ",":
                        self.i += 1
                self.eat(")")
                return self.call(val, args)
            cur = self.ctx
            for part in val.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            return cur
        raise ValueError(f"unexpected token {val!r}")

    @staticmethod
    def call(name, args):
        if name == "fromJSON":
            return json.loads(args[0])
        if name == "format":
            out = args[0]
            for n, a in enumerate(args[1:]):
                out = out.replace("{%d}" % n, str(a))
            return out
        if name == "contains":
            return str(args[1]).lower() in str(args[0]).lower()
        raise ValueError(f"unsupported function {name}")


def evaluate(template: str, ctx: dict):
    """Evaluate a `${{ ... }}` runs-on value (or return a literal as-is)."""
    m = re.fullmatch(r"\$\{\{(.*)\}\}", template.strip(), re.S)
    return Evaluator(ctx).run(m.group(1)) if m else template


def ctx_for(private, input_name, value, blacksmith="", lightweight="", extra_inputs=None, linux=""):
    repo = None if private is None else {"private": private}
    inputs = {input_name: value, "required-runner": "", "lightweight-runner": "", "e2e-runner": ""}
    inputs.update(extra_inputs or {})
    # CI_LIGHTWEIGHT_RUNNER has org visibility `private`, so a public caller
    # never sees it; BLACKSMITH_RUNNERS_ENABLED has visibility `all`.
    vars_ = {"BLACKSMITH_RUNNERS_ENABLED": blacksmith,
             "CI_LIGHTWEIGHT_RUNNER": lightweight if private else "",
             # A repo-level variable: a public caller can hold one too.
             "CI_LINUX_RUNNER": linux}
    return {"github": {"event": {"repository": repo} if repo else {}},
            "inputs": inputs, "vars": vars_}


def runs_on_sites(doc: dict, input_name: str):
    for jid, job in doc["jobs"].items():
        ro = job.get("runs-on")
        if isinstance(ro, str) and f"inputs.{input_name}" in ro:
            yield jid, ro


def is_hosted_ubuntu_latest(route) -> bool:
    return route == "ubuntu-latest"


def check_callable(fname: str, name: str) -> int:
    text = (WORKFLOWS / fname).read_text()
    doc = yaml.safe_load(text)
    inp = doc[True]["workflow_call"]["inputs"][name]  # `on:` parses as True
    assert inp.get("default") == "", f"{fname}: {name} default must be '' (got {inp.get('default')!r})"
    assert inp.get("required") is False, f"{fname}: {name} must stay optional"

    default_effective = f"(inputs.{name} || {DEFAULT_TAIL})"
    var_effective = f"(github.event.repository.private == true && vars.CI_LINUX_RUNNER || inputs.{name} || {DEFAULT_TAIL})"
    var_sites = LINUX_VAR_SITES.get(fname, set())
    sites = list(runs_on_sites(doc, name))
    assert sites, f"{fname}: no runs-on site reads inputs.{name}"
    checks = 0
    seen_var_sites = set()
    for jid, ro in sites:
        if jid in var_sites:
            assert var_effective in ro and "CI_LINUX_RUNNER" in ro, f"{fname}:{jid}: ordinary route must honour vars.CI_LINUX_RUNNER"
            seen_var_sites.add(jid)
            # Unset CI_LINUX_RUNNER: identical to the pre-variable expression.
            legacy = ro.replace(var_effective, default_effective)
            assert "CI_LINUX_RUNNER" not in legacy, f"{fname}:{jid}: CI_LINUX_RUNNER outside the effective route"
            for bs in ("", "false", "true"):
                for vis in (True, False, None):
                    for val in ("", '"ubuntu-latest"', LINUX_CI):
                        c = ctx_for(vis, name, val, blacksmith=bs)
                        assert evaluate(ro, c) == evaluate(legacy, c), f"{fname}:{jid} unset CI_LINUX_RUNNER changed the route"
                        checks += 1
                        # Set: it wins on a private caller, over an explicit
                        # input too (W8 item 8); a public or visibility-unknown
                        # caller never uses it.
                        c = ctx_for(vis, name, val, blacksmith=bs, linux='"blacksmith-4vcpu-ubuntu-2404"')
                        got = evaluate(ro, c)
                        if vis is not True:
                            assert got == evaluate(legacy, c), f"{fname}:{jid} public caller took CI_LINUX_RUNNER -> {got!r}"
                            if not val:
                                assert is_hosted_ubuntu_latest(got), f"{fname}:{jid} public caller left hosted -> {got!r}"
                        elif bs == "true":
                            # Blacksmith logic unchanged and not widened: a
                            # non-hosted effective route becomes the Blacksmith label.
                            assert got == BLACKSMITH_LABEL, f"{fname}:{jid} -> {got!r}"
                        else:
                            assert got == "blacksmith-4vcpu-ubuntu-2404", f"{fname}:{jid} private CI_LINUX_RUNNER -> {got!r}"
                        checks += 1
            # A hosted value in the variable keeps Blacksmith off, like an explicit hosted input.
            got = evaluate(ro, ctx_for(True, name, "", blacksmith="true", linux='"ubuntu-latest"'))
            assert is_hosted_ubuntu_latest(got), f"{fname}:{jid} hosted CI_LINUX_RUNNER + blacksmith -> {got!r}"
            ro = legacy
        else:
            assert "CI_LINUX_RUNNER" not in ro, f"{fname}:{jid}: only the ordinary route reads CI_LINUX_RUNNER"
        effective = default_effective
        assert effective in ro, f"{fname}:{jid}: runs-on does not use the effective-default expression"
        assert f"inputs.{name}" not in ro.replace(effective, ""), (
            f"{fname}:{jid}: a bare inputs.{name} remains outside the effective-default expression"
        )
        old = ro.replace(effective, f"(inputs.{name})")
        has_bs = "BLACKSMITH_RUNNERS_ENABLED" in ro
        has_lw = "CI_LIGHTWEIGHT_RUNNER" in ro

        for bs in ("", "false", "true"):
            # (1) public / unknown visibility: never self-hosted, never Blacksmith.
            for vis in (False, None):
                for val in ("", '"ubuntu-latest"'):
                    got = evaluate(ro, ctx_for(vis, name, val, blacksmith=bs, lightweight='"ubuntu-slim"'))
                    assert is_hosted_ubuntu_latest(got), f"{fname}:{jid} public={vis} input={val!r} bs={bs} -> {got!r}"
                    checks += 1

            # (1b) public / unknown visibility with an explicit self-hosted
            # input and the switch ON: never Blacksmith (a public repository
            # runs free on GitHub-hosted capacity; Logan 2026-10-05).
            if has_bs:
                for vis in (False, None):
                    for val in (LINUX_CI, '["self-hosted","Linux","X64","proxmox","linux-ci"]'):
                        got = evaluate(ro, ctx_for(vis, name, val, blacksmith=bs, lightweight='"ubuntu-slim"'))
                        assert "blacksmith" not in json.dumps(got), f"{fname}:{jid} public={vis} input={val!r} bs={bs} landed on Blacksmith -> {got!r}"
                        checks += 1

            for lw in ("", '"ubuntu-slim"'):
                # (2) private, nothing passed.
                got = evaluate(ro, ctx_for(True, name, "", blacksmith=bs, lightweight=lw))
                if has_lw and lw:
                    want = "ubuntu-slim"
                elif has_bs and bs == "true":
                    want = BLACKSMITH_LABEL
                else:
                    want = json.loads(LINUX_CI)
                assert got == want, f"{fname}:{jid} private empty bs={bs} lw={lw!r} -> {got!r}, want {want!r}"
                checks += 1

                # (3) explicit caller value: identical to the pre-flip expression.
                for val in ('"ubuntu-latest"', LINUX_CI, '"ubuntu-24.04"',
                            '["self-hosted","Linux","X64","proxmox","linux-ci"]'):
                    for vis in (True, False, None):
                        c = ctx_for(vis, name, val, blacksmith=bs, lightweight=lw)
                        got, before = evaluate(ro, c), evaluate(old, c)
                        assert got == before, f"{fname}:{jid} explicit {val} vis={vis}: {got!r} != pre-flip {before!r}"
                        checks += 1

        # explicit hosted value on a private caller with Blacksmith on stays hosted.
        got = evaluate(ro, ctx_for(True, name, '"ubuntu-latest"', blacksmith="true"))
        assert is_hosted_ubuntu_latest(got), f"{fname}:{jid} explicit hosted + blacksmith -> {got!r}"
    assert seen_var_sites == var_sites, f"{fname}: CI_LINUX_RUNNER sites {sorted(seen_var_sites)} != {sorted(var_sites)}"
    print(f"ok  {fname}: {len(sites)} runs-on site(s), {checks} evaluations")
    return checks


def check_browser_tests() -> None:
    doc = yaml.safe_load((WORKFLOWS / "reusable-browser-tests.yml").read_text())
    jobs = doc["jobs"]
    for jid in ("contract", "required"):
        ro = jobs[jid]["runs-on"]
        assert "inputs." not in ro, f"reusable-browser-tests:{jid} must not read a caller input"
        for vis in (False, None):
            got = evaluate(ro, ctx_for(vis, "linux-runner", LINUX_CI, lightweight='"ubuntu-slim"'))
            assert is_hosted_ubuntu_latest(got), f"browser-tests:{jid} public={vis} -> {got!r}"
        got = evaluate(ro, ctx_for(True, "linux-runner", ""))
        assert got == json.loads(LINUX_CI), f"browser-tests:{jid} private -> {got!r}"
    got = evaluate(jobs["required"]["runs-on"], ctx_for(True, "linux-runner", "", lightweight='"ubuntu-slim"'))
    assert got == "ubuntu-slim", f"browser-tests:required keeps CI_LIGHTWEIGHT_RUNNER first -> {got!r}"
    print("ok  reusable-browser-tests.yml: contract + required")


def check_python_data_required() -> None:
    # CI reset 2026-09-28: python-data's Required runs on the caller's own
    # lint/test route, never the org lightweight route, so a caller that runs
    # lint and test hosted (narduk-data earth-data-ci) takes no linux-ci seat.
    jobs = yaml.safe_load((WORKFLOWS / "python-data.yml").read_text())["jobs"]
    ro = jobs["required"]["runs-on"]
    # ci-reset W10 (W8 item 10) folded the `lint` job into `test`.
    assert "lint" not in jobs, "python-data: ruff runs inside `test`, not a job of its own"
    assert ro == jobs["test"]["runs-on"], "python-data:required must share test's route"
    assert "CI_LIGHTWEIGHT_RUNNER" not in ro, "python-data:required must not read CI_LIGHTWEIGHT_RUNNER"
    got = evaluate(ro, ctx_for(True, "runner", '"ubuntu-24.04"', lightweight=LINUX_CI))
    assert got == "ubuntu-24.04", f"python-data:required hosted caller -> {got!r}"
    print("ok  python-data.yml: Required follows the caller route")


def check_single_literal() -> None:
    literals = set()
    for f in WORKFLOWS.glob("*.yml"):
        literals.update(re.findall(r'\{"group":"linux-ci","labels":\[(?:"[^"]*",?)*\]\}', f.read_text()))
    assert literals == {LINUX_CI}, f"divergent linux-ci route literals: {sorted(literals)}"
    print("ok  one linux-ci route literal across callables")


def self_test() -> None:
    c = {"github": {"event": {}}, "inputs": {"a": ""}, "vars": {}}
    assert evaluate("${{ inputs.a || 'x' }}", c) == "x"
    assert evaluate("${{ github.event.repository.private == true && 'y' || 'n' }}", c) == "n"
    assert evaluate("${{ fromJSON('\"q\"') }}", c) == "q"
    assert evaluate("${{ format('\"{0}\"', 'z') }}", c) == '"z"'
    assert evaluate("${{ 'A' == 'a' }}", c) is True


def main() -> int:
    self_test()
    total = sum(check_callable(f, n) for f, n in CALLABLES.items())
    check_browser_tests()
    check_python_data_required()
    check_single_literal()
    print(f"test_runner_default: all passed ({total} evaluations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
