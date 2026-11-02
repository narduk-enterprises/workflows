#!/usr/bin/env python3
"""A night-dispatched run (README "Night-dispatched runs") ships nothing, as a scheduled run shipped nothing.

The estate night dispatcher starts a `workflow_dispatch` run that carries the caller's `nightly` input. The callables read it
as `github.event.inputs.nightly == 'true'`, the one form that works in a called workflow (`github.event` is the caller's
payload; `inputs` is the callable's own). Both production-handoff conditions are evaluated here for every event a caller can
start, so a guard that is dropped, or that stops matching the payload GitHub sends, fails this test.

Run: python3 scripts/test_nightly_signal.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fast_path import StatusEvaluator, truthy  # noqa: E402

WORKFLOWS = Path(".github/workflows")
CI = yaml.safe_load((WORKFLOWS / "nuxt-cloudflare.yml").read_text())
UPLOAD = yaml.safe_load((WORKFLOWS / "cloudflare-version-upload.yml").read_text())
PREPARE_IF = next(s for s in CI["jobs"]["build"]["steps"] if s.get("name") == "Prepare production artifact")["if"]
UPLOAD_IF = UPLOAD["jobs"]["upload"]["if"]


def handoff(condition: str, event: str, ref: str = "refs/heads/main", payload: dict | None = None) -> bool:
    ctx = {
        "inputs": {"production-output-directory": ".output"},
        "github": {"event_name": event, "ref": ref, "repository": "narduk-enterprises/app",
                   "event": {"repository": {"full_name": "narduk-enterprises/app", "private": True}, **(payload or {})}},
        "__status": "success",
    }
    return truthy(StatusEvaluator(ctx).run(" ".join(condition.split())))


class NightlySignal(unittest.TestCase):
    conditions = {"Prepare production artifact": PREPARE_IF, "cloudflare-version-upload": UPLOAD_IF}

    def test_a_nightly_dispatch_on_main_ships_nothing(self):
        # The dispatcher POSTs {"inputs": {"nightly": "true"}}; GitHub stores it in the payload as the string 'true'
        # even for `type: boolean`, which is why the guard compares against 'true' and not against true.
        for name, condition in self.conditions.items():
            with self.subTest(name):
                self.assertFalse(handoff(condition, "workflow_dispatch", payload={"inputs": {"nightly": "true"}}))

    def test_other_main_runs_still_hand_off(self):
        for name, condition in self.conditions.items():
            with self.subTest(name):
                self.assertTrue(handoff(condition, "push"))
                self.assertTrue(handoff(condition, "workflow_dispatch"), "a dispatch without inputs (dependabot-merge)")
                self.assertTrue(handoff(condition, "workflow_dispatch", payload={"inputs": {"nightly": "false"}}),
                                "a person's dispatch leaves the input false")
                self.assertTrue(handoff(condition, "workflow_dispatch", payload={"inputs": {"other": "x"}}))

    def test_nothing_else_hands_off(self):
        for name, condition in self.conditions.items():
            with self.subTest(name):
                self.assertFalse(handoff(condition, "schedule"))
                self.assertFalse(handoff(condition, "pull_request"))
                self.assertFalse(handoff(condition, "workflow_dispatch", ref="refs/heads/feature"))

    def test_guard_reads_the_event_payload_not_the_callables_inputs(self):
        # `inputs.nightly` in a callable is the callable's own input, which does not exist (and Nuxt inputs are frozen).
        for condition in self.conditions.values():
            self.assertIn("github.event.inputs.nightly != 'true'", condition)
            self.assertNotIn("inputs.nightly", condition.replace("github.event.inputs.nightly", ""))
        self.assertNotIn("nightly", CI[True]["workflow_call"]["inputs"])


if __name__ == "__main__":
    unittest.main()
