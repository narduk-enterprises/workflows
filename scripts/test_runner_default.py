#!/usr/bin/env python3
"""Evaluate shipped runner expressions across visibility and override inputs.

Only an explicitly public caller may select GitHub-hosted capacity. Private or
unknown callers ignore hosted overrides and keep existing fleet/Blacksmith
routes, including required/lightweight jobs and platform-specific inputs.
"""

from __future__ import annotations

import functools
import itertools
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
# The background tier's route, copied from fleet
# `github_runner_fleet.py route --workload linux-ci-background` (same group,
# its own label). nuxt-cloudflare.yml and taskgraph.yml send non-interactive
# events to it in place of the default linux-ci route (workflows#241).
LINUX_CI_BACKGROUND = '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci-background"]}'
BACKGROUND_EVENT = ("(github.event_name == 'schedule' || github.event.inputs.nightly == 'true'"
                    " || github.event.pull_request.draft == true"
                    " || github.event.pull_request.user.login == 'dependabot[bot]'"
                    " || github.event.pull_request.user.login == 'narduk-lane-automation[bot]')")
# The T2 interactive tier's route (CI-RULES T2), from fleet
# `github_runner_fleet.py route --workload linux-ci-interactive`; both orgs
# resolve to the same value. A non-background pull_request in a T2 repository
# goes here in place of the default linux-ci route.
LINUX_CI_INTERACTIVE = '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci-interactive"]}'
# The one list of T2 repositories (workflows README, "Route by event").
T2_REPOSITORIES = ("narduk-enterprises/stonx", "narduk-enterprises-clients/pacc-trac")
INTERACTIVE_EVENT = ("(github.event_name == 'pull_request'"
                     f" && contains(fromJSON('{json.dumps(list(T2_REPOSITORIES), separators=(',', ':'))}'),"
                     " github.repository))")
# The default tier, routed by event: every place the two callables used to
# fall back to (or were explicitly given) the plain linux-ci route.
DEFAULT_TIER = (f"({BACKGROUND_EVENT} && '{LINUX_CI_BACKGROUND}'"
                f" || {INTERACTIVE_EVENT} && '{LINUX_CI_INTERACTIVE}' || '{LINUX_CI}')")
# callable -> the input that carries the caller's route
CALLABLES = {
    "reusable-browser-tests.yml": "linux-runner",
    "apple.yml": "lint-runner",
    "change-scope.yml": "runner",
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
    r"\s*(?:(?P<attr>\.[A-Za-z_][\w-]*)|(?P<str>'(?:[^']|'')*')|(?P<op>\|\||&&|==|!=|!|\(|\)|,)"
    r"|(?P<num>-?\d+(?:\.\d+)?)|(?P<id>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*))"
)


@functools.lru_cache(maxsize=None)
def tokenize(src: str) -> tuple[tuple[str, str], ...]:
    out, pos = [], 0
    src = src.strip()
    while pos < len(src):
        m = TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot tokenize at {src[pos:pos + 30]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return tuple(out)


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
                result = self.call(val, args)
                while self.peek()[0] == "attr":
                    key = self.peek()[1][1:]
                    self.i += 1
                    result = result.get(key) if isinstance(result, dict) else None
                return result
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
            if isinstance(args[0], list):
                return any(loose_eq(value, args[1]) for value in args[0])
            if isinstance(args[0], dict) or args[0] is None:
                return False
            return str(args[1]).lower() in str(args[0]).lower()
        raise ValueError(f"unsupported function {name}")


def evaluate(template: str, ctx: dict):
    """Evaluate a `${{ ... }}` runs-on value (or return a literal as-is)."""
    m = re.fullmatch(r"\$\{\{(.*)\}\}", template.strip(), re.S)
    return Evaluator(ctx).run(m.group(1)) if m else template


def runner_context(private, inputs, variables, event_name="pull_request", event=None,
                   repository="narduk-enterprises/example"):
    return {"github": {"event_name": event_name, "repository": repository,
                       "event": {**(event or {}), "repository": {"private": private}}},
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
              '{"group":"self-hosted","labels":["ubuntu-latest"]}',
              '{"self-hosted":true,"labels":["windows-latest"]}',
              '{"labels":"ubuntu-self-hosted"}',
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
                            if visibility is None:
                                assert got == "missing-repository-visibility-do-not-run", (fname, jid, value, got)
                            else:
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


def check_precedence():
    fleet_a = '{"group":"linux-ci","labels":["self-hosted","seat-a"]}'
    fleet_b = '{"group":"linux-ci","labels":["self-hosted","seat-b"]}'
    cases = [
        ("red-main-listener.yml", "reconcile", {"runner": fleet_a}, {"CI_LIGHTWEIGHT_RUNNER": fleet_b}, fleet_b),
        ("red-main-listener.yml", "reconcile", {"runner": fleet_a}, {"CI_LIGHTWEIGHT_RUNNER": '\"ubuntu-latest\"'}, fleet_a),
        ("node-library.yml", "required", {"runner": fleet_a, "required-runner": fleet_b}, {"CI_LIGHTWEIGHT_RUNNER": fleet_a}, fleet_b),
        ("node-library.yml", "required", {"runner": fleet_a}, {"CI_LIGHTWEIGHT_RUNNER": fleet_b}, fleet_b),
        ("nuxt-cloudflare.yml", "build", {"runner": fleet_a}, {"CI_LINUX_RUNNER": fleet_b}, fleet_b),
        ("taskgraph.yml", "tasks", {"runner": fleet_a}, {"CI_LINUX_RUNNER": fleet_b}, fleet_b),
        ("docs-governance.yml", "required", {"runner": fleet_a}, {}, fleet_a),
        ("docs-governance.yml", "required", {"runner": fleet_a}, {"BLACKSMITH_RUNNERS_ENABLED": "true"}, '\"blacksmith-2vcpu-ubuntu-2404\"'),
        ("docs-governance.yml", "required", {"runner": fleet_a}, {"BLACKSMITH_RUNNERS_ENABLED": "true", "BLACKSMITH_LINUX_LABEL": "blacksmith-4vcpu-ubuntu-2404"}, '\"blacksmith-4vcpu-ubuntu-2404\"'),
        ("apple.yml", "xcode", {"apple-runner": '{"group":"apple-vm","labels":["self-hosted","macOS","X64","apple-vm"]}'}, {}, '{"group":"apple-vm","labels":["self-hosted","macOS","X64","apple-vm"]}'),
        ("reusable-browser-tests.yml", "chromium", {"browser-runner": '{"group":"playwright-isolated","labels":["self-hosted","Linux","X64","proxmox-playwright-x64"]}'}, {}, '{"group":"playwright-isolated","labels":["self-hosted","Linux","X64","proxmox-playwright-x64"]}'),
    ]
    for filename, job, inputs, variables, wanted in cases:
        expression = yaml.safe_load((WORKFLOWS / filename).read_text())["jobs"][job]["runs-on"]
        assert evaluate(expression, runner_context(True, inputs, variables)) == json.loads(wanted), (filename, job, "precedence")
        assert evaluate(expression, runner_context(None, inputs, variables)) == "missing-repository-visibility-do-not-run", (filename, job, "unknown")
    print(f"ok  {len(cases)} exact fleet/Blacksmith precedence and unknown-visibility cases")


def check_single_literal() -> None:
    literals = set()
    for f in WORKFLOWS.glob("*.yml"):
        literals.update(re.findall(r'\{"group":"linux-ci","labels":\[(?:"[^"]*",?)*\]\}', f.read_text()))
    assert literals == {LINUX_CI, LINUX_CI_BACKGROUND, LINUX_CI_INTERACTIVE}, f"divergent linux-ci route literals: {sorted(literals)}"
    print("ok  one linux-ci, linux-ci-background and linux-ci-interactive route literal across callables")


def pr(login, draft=False):
    return {"pull_request": {"draft": draft, "user": {"login": login}}}


# (name, event_name, payload, background?)
EVENTS = [
    ("schedule", "schedule", {}, True),
    ("nightly dispatch", "workflow_dispatch", {"inputs": {"nightly": "true"}}, True),
    ("manual dispatch", "workflow_dispatch", {"inputs": {"nightly": "false"}}, False),
    ("dispatch without inputs", "workflow_dispatch", {}, False),
    ("Dependabot PR", "pull_request", pr("dependabot[bot]"), True),
    ("Dependabot PR target", "pull_request_target", pr("dependabot[bot]"), True),
    ("lane-automation PR", "pull_request", pr("narduk-lane-automation[bot]"), True),
    ("draft PR", "pull_request", pr("loganrenz", draft=True), True),
    ("human PR", "pull_request", pr("loganrenz"), False),
    ("other bot PR", "pull_request", pr("narduk-durable-agent-sessions[bot]"), False),
    ("push", "push", {}, False),
    ("merge group", "merge_group", {}, False),
    ("human PR target", "pull_request_target", pr("loganrenz"), False),
]
OTHER_FLEET = '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci-tailnet"]}'
BROWSER = '{"group":"playwright-isolated","labels":["self-hosted","Linux","X64","proxmox-playwright-x64"]}'


def check_event_routing() -> None:
    """The default tier follows the event; nothing else moves.

    The reference is the same expression with the default tier pinned to
    linux-ci, which is the pre-#241 shape the rest of this file pins. Every
    event must resolve exactly as that reference, except that a resolved
    linux-ci route (bar a deploy build, production-output-directory set)
    becomes the background route on a background event, and the interactive
    route on any other pull_request in a T2 repository.
    """
    checks = 0
    for fname in ("nuxt-cloudflare.yml", "taskgraph.yml"):
        doc = yaml.safe_load((WORKFLOWS / fname).read_text())
        defaults = {k: v.get("default", "") for k, v in doc[True]["workflow_call"]["inputs"].items()}
        input_variants = [{}]
        input_variants += [{"runner": v} for v in (LINUX_CI, OTHER_FLEET, '"blacksmith-4vcpu-ubuntu-2404"', '"ubuntu-latest"')]
        input_variants += [{"lightweight-runner": OTHER_FLEET}, {"e2e-runner": BROWSER},
                           {"production-output-directory": ".output"},
                           {"production-output-directory": ".output", "runner": LINUX_CI},
                           {"fast-parallel": True}, {"preview-checks": "e2e-subset"}]
        var_variants = [{}, {"CI_LIGHTWEIGHT_RUNNER": LINUX_CI}, {"CI_LINUX_RUNNER": OTHER_FLEET},
                        {"CI_LIGHTWEIGHT_RUNNER": LINUX_CI, "BLACKSMITH_RUNNERS_ENABLED": "true"}]
        for jid, job in doc["jobs"].items():
            expression = job.get("runs-on")
            if not isinstance(expression, str):
                continue
            assert DEFAULT_TIER in expression, (fname, jid, "default tier is not routed by event")
            reference = expression.replace(DEFAULT_TIER, f"'{LINUX_CI}'")
            assert LINUX_CI_BACKGROUND not in reference, (fname, jid, "background route outside the default tier")
            assert LINUX_CI_INTERACTIVE not in reference, (fname, jid, "interactive route outside the default tier")
            for extra in input_variants:
                inputs = {**defaults, **extra}
                for extra_vars in var_variants:
                    variables = {"CI_LIGHTWEIGHT_RUNNER": "", "CI_LINUX_RUNNER": "",
                                 "BLACKSMITH_RUNNERS_ENABLED": "", "BLACKSMITH_LINUX_LABEL": "", **extra_vars}
                    for visibility, (name, event_name, payload, background), repository in itertools.product(
                            (True, False, None), EVENTS, ("narduk-enterprises/example", T2_REPOSITORIES[0])):
                        ctx = runner_context(visibility, inputs, variables, event_name, payload, repository)
                        got, old = evaluate(expression, ctx), evaluate(reference, ctx)
                        wanted = old
                        deploy_build = jid == "build" and inputs["production-output-directory"] != ""
                        interactive = (not background and event_name == "pull_request"
                                       and repository in T2_REPOSITORIES)
                        if old == json.loads(LINUX_CI) and not deploy_build:
                            if background:
                                wanted = json.loads(LINUX_CI_BACKGROUND)
                            elif interactive:
                                wanted = json.loads(LINUX_CI_INTERACTIVE)
                        assert got == wanted, (fname, jid, name, repository, visibility, extra, extra_vars, got, wanted)
                        checks += 1
    # Spelled out: what the done-when runs will see on a private caller with
    # the org CI_LIGHTWEIGHT_RUNNER (linux-ci) and gonogo's hand runner
    # expression, which yields the plain linux-ci route while
    # CI_BACKGROUND_RUNNER is unset.
    org = {"CI_LIGHTWEIGHT_RUNNER": LINUX_CI}
    bg, fg = json.loads(LINUX_CI_BACKGROUND), json.loads(LINUX_CI)
    cases = [
        ("taskgraph.yml", "fast", {"runner": LINUX_CI}, org, "workflow_dispatch", {"inputs": {"nightly": "true"}}, bg),
        ("taskgraph.yml", "tasks", {"runner": LINUX_CI}, org, "pull_request", pr("dependabot[bot]"), bg),
        ("taskgraph.yml", "fast", {"runner": LINUX_CI}, org, "pull_request", pr("loganrenz", draft=True), bg),
        ("taskgraph.yml", "fast", {"runner": LINUX_CI}, org, "pull_request", pr("loganrenz"), fg),
        ("taskgraph.yml", "tasks", {"runner": LINUX_CI}, org, "push", {}, fg),
        # A caller's own background route (CI_BACKGROUND_RUNNER set) is honoured.
        ("taskgraph.yml", "tasks", {"runner": LINUX_CI_BACKGROUND}, org, "schedule", {}, bg),
        # An explicit non-default route keeps precedence on every event.
        ("taskgraph.yml", "tasks", {"runner": OTHER_FLEET}, org, "schedule", {}, json.loads(OTHER_FLEET)),
        ("nuxt-cloudflare.yml", "fast", {}, org, "pull_request", pr("dependabot[bot]"), bg),
        ("nuxt-cloudflare.yml", "build", {}, org, "schedule", {}, bg),
        ("nuxt-cloudflare.yml", "build", {"production-output-directory": ".output"}, org, "schedule", {}, fg),
        # vars.CI_LINUX_RUNNER keeps precedence where it is set.
        ("nuxt-cloudflare.yml", "build", {}, {"CI_LINUX_RUNNER": OTHER_FLEET}, "schedule", {}, json.loads(OTHER_FLEET)),
        ("taskgraph.yml", "tasks", {}, {"CI_LINUX_RUNNER": LINUX_CI}, "schedule", {}, fg),
        ("nuxt-cloudflare.yml", "required", {}, org, "pull_request", pr("narduk-lane-automation[bot]"), bg),
    ]
    # T2 (stonx): only a human or other non-background pull_request moves.
    it, stonx = json.loads(LINUX_CI_INTERACTIVE), "narduk-enterprises/stonx"
    t2_cases = [
        ("nuxt-cloudflare.yml", "fast", {}, org, "pull_request", pr("loganrenz"), it),
        ("nuxt-cloudflare.yml", "ci-admission", {}, org, "pull_request", pr("loganrenz"), it),
        ("nuxt-cloudflare.yml", "build", {}, org, "pull_request", pr("loganrenz"), it),
        ("nuxt-cloudflare.yml", "fast", {}, org, "pull_request", pr("loganrenz", draft=True), bg),
        ("nuxt-cloudflare.yml", "fast", {}, org, "pull_request", pr("dependabot[bot]"), bg),
        ("nuxt-cloudflare.yml", "fast", {}, org, "pull_request", pr("narduk-lane-automation[bot]"), bg),
        ("nuxt-cloudflare.yml", "fast", {}, org, "schedule", {}, bg),
        ("nuxt-cloudflare.yml", "fast", {}, org, "workflow_dispatch", {"inputs": {"nightly": "true"}}, bg),
        ("nuxt-cloudflare.yml", "fast", {}, org, "push", {}, fg),
        ("nuxt-cloudflare.yml", "fast", {}, org, "workflow_dispatch", {"inputs": {"nightly": "false"}}, fg),
        ("nuxt-cloudflare.yml", "build", {"production-output-directory": ".output"}, org, "pull_request", pr("loganrenz"), fg),
        ("nuxt-cloudflare.yml", "build", {}, {"CI_LINUX_RUNNER": OTHER_FLEET}, "pull_request", pr("loganrenz"), json.loads(OTHER_FLEET)),
        ("taskgraph.yml", "tasks", {"runner": OTHER_FLEET}, org, "pull_request", pr("loganrenz"), json.loads(OTHER_FLEET)),
        ("taskgraph.yml", "tasks", {"runner": LINUX_CI}, org, "pull_request", pr("loganrenz"), it),
    ]
    # Every listed repository, not only the one the full sweep above uses.
    t2_cases = [case + (stonx,) for case in t2_cases] + [
        ("nuxt-cloudflare.yml", "fast", {}, org, "pull_request", pr("loganrenz"), it, repository)
        for repository in T2_REPOSITORIES] + [
        ("nuxt-cloudflare.yml", "fast", {}, org, "pull_request", pr("loganrenz"), fg, "narduk-enterprises/stonx-apple"),
        ("nuxt-cloudflare.yml", "fast", {}, org, "push", {}, fg, "narduk-enterprises-clients/pacc-trac"),
    ]
    for filename, jid, inputs, variables, event_name, payload, wanted, repository in [
            *(case + ("narduk-enterprises/example",) for case in cases), *t2_cases]:
        doc = yaml.safe_load((WORKFLOWS / filename).read_text())
        defaults = {k: v.get("default", "") for k, v in doc[True]["workflow_call"]["inputs"].items()}
        ctx = runner_context(True, {**defaults, **inputs}, variables, event_name, payload, repository)
        got = evaluate(doc["jobs"][jid]["runs-on"], ctx)
        assert got == wanted, (filename, jid, repository, event_name, payload, got)
        checks += 1
    print(f"ok  event routing: {checks} default-tier evaluations")


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
    check_precedence()
    check_single_literal()
    check_event_routing()
    print(f"test_runner_default: all passed ({total} evaluations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
