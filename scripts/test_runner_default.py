#!/usr/bin/env python3
"""Evaluate shipped runner expressions across visibility and override inputs.

Only an explicitly public caller may select GitHub-hosted capacity. Private or
unknown callers ignore hosted overrides and keep existing fleet/Blacksmith
routes, including required/lightweight jobs and platform-specific inputs.
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
# callable -> the input that carries the caller's route
CALLABLES = {
    "reusable-browser-tests.yml": "linux-runner",
    "apple.yml": "lint-runner",
    "docs-governance.yml": "runner",
    "node-library.yml": "runner",
    "python-data.yml": "runner",
    "nuxt-cloudflare.yml": "runner",
    "red-main-listener.yml": "runner",
    "flake-digest.yml": "runner",
    "design-ledger.yml": "runs-on",
    "design-publish.yml": "runs-on",
    "taskgraph.yml": "runner",
}


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
        if name == "toJSON":
            return json.dumps(args[0])
        if name == "fromJSON":
            return json.loads(args[0])
        if name == "format":
            out = args[0]
            for n, a in enumerate(args[1:]):
                out = out.replace("{%d}" % n, str(a))
            return out
        if name == "startsWith":
            return str(args[0]).lower().startswith(str(args[1]).lower())
        if name == "contains":
            return str(args[1]).lower() in str(args[0]).lower()
        raise ValueError(f"unsupported function {name}")


def evaluate(template: str, ctx: dict):
    """Evaluate a `${{ ... }}` runs-on value (or return a literal as-is)."""
    m = re.fullmatch(r"\$\{\{(.*)\}\}", template.strip(), re.S)
    return Evaluator(ctx).run(m.group(1)) if m else template


def runner_context(private, inputs, variables):
    return {"github": {"event": {"repository": {"private": private}}},
            "inputs": inputs, "vars": variables}


def is_private_route(route):
    if isinstance(route, str):
        return route.lower().startswith("blacksmith-") or route.lower() == "self-hosted"
    labels = route.get("labels", []) if isinstance(route, dict) else route
    if isinstance(labels, str):
        labels = [labels]
    return isinstance(labels, list) and "self-hosted" in [x.lower() for x in labels]


def check_callable(fname, _name=None):
    doc = yaml.safe_load((WORKFLOWS / fname).read_text())
    definitions = doc[True]["workflow_call"]["inputs"]
    inputs = {key: value.get("default", "") for key, value in definitions.items()}
    runner_names = [key for key in definitions if key.endswith("runner") or key == "runs-on"]
    variables = {"CI_LIGHTWEIGHT_RUNNER": "", "CI_LINUX_RUNNER": "",
                 "BLACKSMITH_RUNNERS_ENABLED": "", "BLACKSMITH_LINUX_LABEL": ""}
    hosted = ['"ubuntu-latest"', '"ubuntu-24.04"', '"ubuntu-slim"',
              '"windows-latest"', '"macos-latest"', '["ubuntu-latest"]',
              '{"labels":"ubuntu-latest"}', '{"group":"large","labels":["ubuntu-latest"]}']
    checks = 0
    for jid, job in doc["jobs"].items():
        expression = job.get("runs-on")
        if not isinstance(expression, str):
            continue
        # Exercise ordinary, lightweight, preview-browser, and parallel routes.
        for extra in ({}, {"fast-parallel": True}, {"preview-checks": "e2e-subset"}):
            base_inputs = {**inputs, **extra}
            for visibility in (True, None):
                for blacksmith in ("", "true"):
                    base_vars = {**variables, "BLACKSMITH_RUNNERS_ENABLED": blacksmith}
                    for value in hosted:
                        # One bad override at a time, and all together, catch precedence leaks.
                        variants = [({**base_inputs, key: value}, base_vars) for key in runner_names]
                        variants += [(base_inputs, {**base_vars, key: value})
                                     for key in ("CI_LIGHTWEIGHT_RUNNER", "CI_LINUX_RUNNER")]
                        variants += [({**base_inputs, **dict.fromkeys(runner_names, value)},
                                      {**base_vars, "CI_LIGHTWEIGHT_RUNNER": value, "CI_LINUX_RUNNER": value,
                                       "BLACKSMITH_LINUX_LABEL": "ubuntu-latest"})]
                        for trial_inputs, trial_vars in variants:
                            got = evaluate(expression, runner_context(visibility, trial_inputs, trial_vars))
                            assert is_private_route(got), (fname, jid, visibility, value, got)
                            checks += 1
            # Valid explicit fleet routes retain precedence when this job reads them.
            for key in runner_names:
                if f"inputs.{key}" not in expression:
                    continue
                for route in (LINUX_CI, '["self-hosted","Linux","X64","proxmox","linux-ci"]',
                              '"blacksmith-4vcpu-ubuntu-2404"'):
                    got = evaluate(expression, runner_context(True, {**base_inputs, key: route}, variables))
                    assert is_private_route(got), (fname, jid, key, route, got)
                    # Conditional preview/parallel branches need not select this
                    # particular input. Direct input jobs must preserve its value.
                    if len(runner_names) == 1:
                        assert got == json.loads(route), (fname, jid, key, route, got)
                    checks += 1
            # Empty public inputs retain free GitHub-hosted Linux routing. Required
            # platform inputs may specify a public macOS/browser runner explicitly.
            public_inputs = {**base_inputs, **dict.fromkeys(runner_names, '"ubuntu-latest"')}
            got = evaluate(expression, runner_context(False, public_inputs,
                           {**variables, "BLACKSMITH_RUNNERS_ENABLED": "true"}))
            assert got == "ubuntu-latest", (fname, jid, "public", got)
            checks += 1
    print(f"ok  {fname}: {checks} routing evaluations")
    return checks


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
    check_single_literal()
    print(f"test_runner_default: all passed ({total} evaluations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
