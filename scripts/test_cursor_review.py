#!/usr/bin/env python3
"""Offline proofs for `cursor-review.yml` and `scripts/cursor_review.py`.

The whole flow runs against a fake transport that records every request and
answers from fixtures, so these tests prove the shape of what the job sends to
Cursor and to GitHub without a key, a runner, or a network.

Properties worth this machinery (agent-infrastructure#1564):

  * Skips stay green: draft, fork head, opt-out label, missing secret, no PR.
  * The review is a REAL pull-request review on the reviewed head, with the
    event decided only by the contracted vocabulary: any blocking finding
    requests changes; a self-authored PR never asks GitHub for the events it
    refuses.
  * Inline comments anchor only to new-side lines inside the PR diff; the rest
    land in the body, so no bad anchor can 422 the whole review.
  * A non-blocking review dismisses this bot's own stale REQUEST_CHANGES.
  * A head that moved while the agent ran posts nothing (the new head gets its
    own review), and a reviewer error makes the job red, never silently green.
  * The callable itself passes the repository's structural rules and wires
    every value the script reads.

Run: python3 scripts/test_cursor_review.py  (needs PyYAML, as every test here does)
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cursor_review as cr  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "cursor-review.yml"
BRIEF = ROOT / "scripts" / "cursor_review_brief.md"
HEAD = "a" * 40
BASE = "b" * 40
REPO = "narduk-enterprises/narduk-libs"

PATCH = "@@ -1,3 +1,4 @@\n line\n+added\n line\n line\n@@ -40,2 +41,6 @@\n ctx\n+x\n+y\n+z\n+w\n ctx"


def review_json(**overrides: Any) -> str:
    value = {
        "verdict": "comment",
        "summary": "Looks fine; check the loop bound.",
        "findings": [
            {"severity": "consider", "path": "src/a.ts", "line": 2, "title": "Off by one", "body": "The bound is exclusive."},
            {"severity": "nit", "path": "src/a.ts", "line": 999, "title": "Naming", "body": "Rename it."},
        ],
        "checks_run": ["pnpm -s test -> passed"],
    }
    value.update(overrides)
    return "Model: grok-4.6\n\nHere is my review.\n\n```json\n" + json.dumps(value) + "\n```\n"


class FakeTransport:
    """Routes by (method, path) and records everything that was sent."""

    def __init__(self, *, result: str = review_json(), run_statuses: list[str] | None = None, pr_author: str = "loganrenz", head_now: str = HEAD, prior_reviews: list[dict[str, Any]] | None = None, marker_comment: dict[str, Any] | None = None, review_status: int = 200) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.run_statuses = list(run_statuses or ["RUNNING", "FINISHED"])
        self.pr_author = pr_author
        self.head_now = head_now
        self.prior_reviews = prior_reviews or []
        self.marker_comment = marker_comment
        self.review_status = review_status
        self.cancelled: list[str] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int) -> tuple[int, Any]:
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": headers, "body": payload})
        path = url.split("?")[0]
        if url.startswith(cr.CURSOR_API):
            return self.cursor(method, path, payload)
        return self.github(method, path, payload)

    def cursor(self, method: str, path: str, payload: Any) -> tuple[int, Any]:
        if method == "POST" and path.endswith("/v1/agents"):
            # The real API (verified 2026-09-18) answers 201 with the agent WRAPPED.
            return 201, {"agent": {"id": "bc-new", "url": "https://cursor.com/agents/bc-new", "status": "CREATING"}}
        if method == "GET" and path.endswith("/v1/agents/bc-old"):
            return 200, {"id": "bc-old", "status": "RUNNING", "latestRunId": "run-old"}
        if method == "POST" and path.endswith("/runs/run-old/cancel"):
            self.cancelled.append("bc-old")
            return 200, {}
        if method == "GET" and path.endswith("/v1/agents/bc-new"):
            return 200, {"id": "bc-new", "status": "RUNNING", "latestRunId": "run-new"}
        if method == "GET" and path.endswith("/runs/run-new"):
            status = self.run_statuses.pop(0) if len(self.run_statuses) > 1 else self.run_statuses[0]
            return 200, {"id": "run-new", "status": status, "result": self.result if status == "FINISHED" else "boom"}
        raise AssertionError(f"unexpected Cursor call {method} {path}")

    def github(self, method: str, path: str, payload: Any) -> tuple[int, Any]:
        if method == "GET" and path.endswith("/issues/7/comments"):
            return 200, [self.marker_comment] if self.marker_comment else []
        if method == "POST" and path.endswith("/issues/7/comments"):
            return 201, {"id": 501, "body": payload["body"]}
        if method == "PATCH" and "/issues/comments/" in path:
            return 200, {"id": int(path.rsplit("/", 1)[1]), "body": payload["body"]}
        if method == "GET" and path.endswith("/pulls/7"):
            return 200, {"number": 7, "head": {"sha": self.head_now}, "user": {"login": self.pr_author}}
        if method == "GET" and path.endswith("/pulls/7/files"):
            return 200, [{"filename": "src/a.ts", "patch": PATCH}, {"filename": "bin/blob", "patch": None}]
        if method == "GET" and path.endswith("/pulls/7/reviews"):
            return 200, self.prior_reviews
        if method == "POST" and path.endswith("/pulls/7/reviews"):
            if self.review_status != 200:
                status, self.review_status = self.review_status, 200
                return status, {"message": "Unprocessable Entity: line could not be resolved" if status == 422 else "Forbidden"}
            return 200, {"id": 900, "html_url": "https://github.com/x/pull/7#pullrequestreview-900", "state": payload["event"]}
        if method == "PUT" and "/dismissals" in path:
            return 200, {"state": "DISMISSED"}
        raise AssertionError(f"unexpected GitHub call {method} {path}")


def env(**overrides: str) -> dict[str, str]:
    value = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_TOKEN": "ghs_fixture",
        "CURSOR_CLOUD_AGENTS_API_KEY": "key_fixture",
        "PR_NUMBER": "7",
        "PR_TITLE": "feat: thing",
        "PR_URL": f"https://github.com/{REPO}/pull/7",
        "PR_HEAD_SHA": HEAD,
        "PR_HEAD_REF": "cursor/thing",
        "PR_HEAD_REPOSITORY": REPO,
        "PR_BASE_SHA": BASE,
        "PR_BASE_REF": "main",
        "PR_DRAFT": "false",
        "PR_OPTED_OUT": "false",
        "CURSOR_MODEL": "grok-4.6",
        "CURSOR_EFFORT": "xhigh",
        "CURSOR_FAST": "true",
        "CONTEXT_REPOS": "narduk-enterprises/agent-infrastructure,narduk-enterprises/company-hq",
        "WAIT_MINUTES": "30",
        "APPROVE_ON_CLEAN": "true",
        "BRIEF_PATH": str(BRIEF),
    }
    value.update(overrides)
    return value


def run(transport: FakeTransport, **overrides: str) -> tuple[int, str]:
    clock = iter(range(0, 100000, 10))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cr.run(env(**overrides), transport, sleep=lambda _s: None, clock=lambda: float(next(clock)))
    return code, out.getvalue()


def posted_review(transport: FakeTransport) -> dict[str, Any]:
    reviews = [c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/pulls/7/reviews")]
    assert reviews, "no review was posted"
    return reviews[-1]["body"]


class SkipTests(unittest.TestCase):
    def test_each_skip_reason_is_green_and_launches_nothing(self):
        for label, overrides in (
            ("no PR", {"PR_NUMBER": ""}),
            ("draft", {"PR_DRAFT": "true"}),
            ("opted out", {"PR_OPTED_OUT": "true"}),
            ("fork head", {"PR_HEAD_REPOSITORY": "someone/narduk-libs"}),
            ("no secret", {"CURSOR_CLOUD_AGENTS_API_KEY": ""}),
        ):
            with self.subTest(label):
                transport = FakeTransport()
                code, out = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertIn("::notice::review skipped:", out)
                self.assertEqual([], transport.calls, "a skip must make no network call")

    def test_a_malformed_head_sha_is_an_error_not_a_skip(self):
        with self.assertRaises(cr.ReviewError):
            run(FakeTransport(), PR_HEAD_SHA="abc")


class ReviewFlowTests(unittest.TestCase):
    def test_launch_carries_head_branch_context_repos_and_the_grok_params(self):
        transport = FakeTransport()
        code, _ = run(transport)
        self.assertEqual(0, code)
        launch = next(c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/v1/agents"))
        body = launch["body"]
        self.assertEqual({"url": f"https://github.com/{REPO}", "startingRef": "cursor/thing"}, body["repos"][0])
        self.assertEqual([{"url": "https://github.com/narduk-enterprises/agent-infrastructure"}, {"url": "https://github.com/narduk-enterprises/company-hq"}], body["repos"][1:])
        self.assertEqual("grok-4.6", body["model"]["id"])
        self.assertEqual([{"id": "effort", "value": "xhigh"}, {"id": "fast", "value": "true"}], body["model"]["params"])
        self.assertTrue(body["workOnCurrentBranch"])
        self.assertNotIn("autoCreatePR", body)
        self.assertIn(f"#7", body["prompt"]["text"])
        self.assertIn(HEAD, body["prompt"]["text"])
        self.assertNotIn("{repository}", body["prompt"]["text"], "every placeholder is substituted")
        self.assertEqual("Bearer key_fixture", launch["headers"]["Authorization"])

    def test_the_review_is_posted_on_the_head_with_inline_comments_only_inside_the_diff(self):
        transport = FakeTransport()
        run(transport)
        review = posted_review(transport)
        self.assertEqual(HEAD, review["commit_id"])
        self.assertEqual("COMMENT", review["event"])
        self.assertEqual(1, len(review["comments"]))
        self.assertEqual({"path": "src/a.ts", "line": 2, "side": "RIGHT"}, {k: review["comments"][0][k] for k in ("path", "line", "side")})
        self.assertIn("**consider** — Off by one", review["comments"][0]["body"])
        self.assertIn("`src/a.ts:999`", review["body"], "the unanchored finding is listed in the body")
        self.assertIn("1 posted inline", review["body"])
        self.assertIn("<!-- cursor-review ", review["body"])
        self.assertIn("pnpm -s test -> passed", review["body"])

    def test_a_blocking_finding_requests_changes_whatever_the_verdict_says(self):
        transport = FakeTransport(result=review_json(verdict="approve", findings=[{"severity": "blocking", "path": "src/a.ts", "line": 42, "title": "Drops rows", "body": "Deletes without a where."}]))
        run(transport)
        review = posted_review(transport)
        self.assertEqual("REQUEST_CHANGES", review["event"])
        self.assertEqual(42, review["comments"][0]["line"])
        self.assertFalse(any(c["method"] == "PUT" for c in transport.calls), "a blocking review dismisses nothing")

    def test_a_clean_verdict_approves_and_dismisses_the_bots_stale_request_changes(self):
        own = f"<!-- {cr.REVIEW_MARKER} " + json.dumps({"agentId": "bc-x", "headSha": "c" * 40, "verdict": "request_changes", "event": "REQUEST_CHANGES"}) + " -->"
        prior = [
            {"id": 1, "state": "CHANGES_REQUESTED", "user": {"login": cr.BOT_LOGIN}, "body": "**Cursor review**\n" + own},
            {"id": 2, "state": "CHANGES_REQUESTED", "user": {"login": "loganrenz"}, "body": "no"},
            {"id": 3, "state": "COMMENTED", "user": {"login": cr.BOT_LOGIN}, "body": own},
            # Another workflow's blocking review under the SAME shared bot login.
            {"id": 4, "state": "CHANGES_REQUESTED", "user": {"login": cr.BOT_LOGIN}, "body": "<!-- agent-review-disposition {} -->\nBlocking concerns"},
        ]
        transport = FakeTransport(result=review_json(verdict="approve", findings=[]), prior_reviews=prior)
        run(transport)
        self.assertEqual("APPROVE", posted_review(transport)["event"])
        dismissals = [c for c in transport.calls if c["method"] == "PUT"]
        self.assertEqual(1, len(dismissals))
        self.assertTrue(dismissals[0]["url"].endswith("/pulls/7/reviews/1/dismissals"), "only this workflow's own request-changes review is dismissed; a human's and another workflow's stay")

    def test_approve_on_clean_false_downgrades_to_comment(self):
        transport = FakeTransport(result=review_json(verdict="approve", findings=[]))
        run(transport, APPROVE_ON_CLEAN="false")
        self.assertEqual("COMMENT", posted_review(transport)["event"])

    def test_a_self_authored_pull_request_only_comments(self):
        transport = FakeTransport(result=review_json(verdict="request_changes", findings=[{"severity": "blocking", "path": "src/a.ts", "line": 2, "title": "t", "body": "b"}]), pr_author=cr.BOT_LOGIN)
        run(transport)
        self.assertEqual("COMMENT", posted_review(transport)["event"])

    def test_a_previous_heads_running_agent_is_cancelled_before_the_new_launch(self):
        marker = {"id": 77, "user": {"login": cr.BOT_LOGIN}, "body": cr.agent_marker("bc-old", "c" * 40) + "\nin progress"}
        transport = FakeTransport(marker_comment=marker)
        run(transport)
        self.assertEqual(["bc-old"], transport.cancelled)
        order = [c["url"].rsplit("/", 1)[-1] for c in transport.calls if c["method"] == "POST" and cr.CURSOR_API in c["url"]]
        self.assertEqual(["cancel", "agents"], order, "cancel happens before the new launch")
        patches = [c for c in transport.calls if c["method"] == "PATCH" and c["url"].endswith("/issues/comments/77")]
        self.assertTrue(patches, "the one marker comment is edited in place, not duplicated")
        self.assertFalse(any(c["method"] == "POST" and c["url"].endswith("/issues/7/comments") for c in transport.calls))

    def test_a_head_that_moved_while_the_agent_ran_posts_nothing_and_stays_green(self):
        transport = FakeTransport(head_now="d" * 40)
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("moved to", out)
        self.assertFalse(any(c["url"].endswith("/pulls/7/reviews") and c["method"] == "POST" for c in transport.calls))

    def test_a_refused_inline_anchor_falls_back_to_a_body_only_review(self):
        transport = FakeTransport(review_status=422)
        run(transport)
        posts = [c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/pulls/7/reviews")]
        self.assertEqual(2, len(posts))
        self.assertEqual([], posts[1]["body"]["comments"])
        self.assertIn("`src/a.ts:2`", posts[1]["body"]["body"])

    def test_an_errored_run_makes_the_job_red_and_records_it_on_the_marker(self):
        transport = FakeTransport(run_statuses=["RUNNING", "ERROR"])
        with self.assertRaisesRegex(cr.ReviewError, "ended ERROR"):
            run(transport)
        last_patch = [c for c in transport.calls if c["method"] == "PATCH"][-1]
        self.assertIn("did not complete", last_patch["body"]["body"])
        self.assertFalse(any(c["url"].endswith("/pulls/7/reviews") and c["method"] == "POST" for c in transport.calls))

    def test_the_wait_budget_ends_the_run_as_an_error(self):
        transport = FakeTransport(run_statuses=["RUNNING"])
        with self.assertRaisesRegex(cr.ReviewError, "wait budget"):
            run(transport, WAIT_MINUTES="1")

    def test_a_launch_response_without_an_agent_is_an_error_that_shows_the_shape(self):
        transport = FakeTransport()
        real = transport.cursor
        transport.cursor = lambda m, p, b: (201, {"ok": True}) if m == "POST" and p.endswith("/v1/agents") else real(m, p, b)
        with self.assertRaisesRegex(cr.ReviewError, r'no agent id \(response: \{"ok": true\}\)'):
            run(transport)

    def test_an_answer_without_the_json_block_is_an_error(self):
        transport = FakeTransport(result="I approve this.")
        with self.assertRaisesRegex(cr.ReviewError, "no fenced json"):
            run(transport)

    def test_main_maps_a_review_error_to_exit_1(self):
        out = io.StringIO()
        with (
            mock.patch.dict(cr.os.environ, env(PR_HEAD_SHA="nope"), clear=True),
            mock.patch.object(cr, "urllib_transport", FakeTransport()),
            contextlib.redirect_stdout(out),
        ):
            code = cr.main()
        self.assertEqual(1, code)
        self.assertIn("::error::PR_HEAD_SHA is not a full commit sha", out.getvalue())

    def test_a_non_anchor_error_does_not_take_the_body_only_fallback(self):
        transport = FakeTransport(review_status=403)
        with self.assertRaisesRegex(cr.ReviewError, "HTTP 403"):
            run(transport)
        posts = [c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/pulls/7/reviews")]
        self.assertEqual(1, len(posts), "a 403 is a real failure, not a bad inline anchor")


class ParserTests(unittest.TestCase):
    def test_parse_result_takes_the_last_block_and_normalizes(self):
        text = "```json\n{\"verdict\": \"approve\", \"summary\": \"old\"}\n```\nthen\n" + review_json(verdict="Request_Changes")
        result = cr.parse_result(text)
        self.assertEqual("request_changes", result["verdict"])
        self.assertEqual(2, len(result["findings"]))
        self.assertEqual("src/a.ts", result["findings"][0]["path"])

    def test_parse_result_keeps_dotfile_paths_and_drops_only_a_dot_slash_prefix(self):
        text = review_json(findings=[
            {"severity": "nit", "path": "./.github/workflows/ci.yml", "line": 3, "title": "a", "body": "b"},
            {"severity": "nit", "path": "/src/x.ts", "line": 1, "title": "a", "body": "b"},
        ])
        paths = [f["path"] for f in cr.parse_result(text)["findings"]]
        self.assertEqual([".github/workflows/ci.yml", "src/x.ts"], paths)

    def test_parse_result_rejects_bad_vocabulary(self):
        for bad in (review_json(verdict="lgtm"), review_json(findings=[{"severity": "high", "title": "x"}]), review_json(summary=""), "```json\n[1]\n```"):
            with self.subTest(bad[:40]), self.assertRaises(cr.ReviewError):
                cr.parse_result(bad)

    def test_hunks_cover_context_lines_and_zero_length_hunks(self):
        self.assertEqual({1, 2, 3, 4, 41, 42, 43, 44, 45, 46}, cr.new_side_lines(PATCH))
        self.assertEqual(set(), cr.new_side_lines("@@ -1,2 +1,0 @@\n-a\n-b"))
        self.assertEqual({5}, cr.new_side_lines("@@ -5 +5 @@\n-a\n+b"))
        self.assertEqual(set(), cr.new_side_lines(None))

    def test_inline_comments_are_capped(self):
        findings = [{"severity": "nit", "path": "src/a.ts", "line": 2, "title": f"t{i}", "body": "", "suggestion": None} for i in range(cr.MAX_INLINE_COMMENTS + 5)]
        placed, unplaced = cr.place_findings(findings, {"src/a.ts": {2}})
        self.assertEqual(cr.MAX_INLINE_COMMENTS, len(placed))
        self.assertEqual(5, len(unplaced))

    def test_agent_marker_round_trips_and_ignores_prose(self):
        body = cr.agent_marker("bc-1", HEAD) + "\nprose"
        self.assertEqual({"agentId": "bc-1", "headSha": HEAD}, cr.parse_agent_marker(body))
        self.assertIsNone(cr.parse_agent_marker("text " + cr.agent_marker("bc-1", HEAD)))
        self.assertIsNone(cr.parse_agent_marker("<!-- cursor-review-agent {not json} -->"))


class WorkflowShapeTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.doc = yaml.safe_load(self.text)
        self.job = self.doc["jobs"]["cursor-review"]

    def test_disabled_by_default_and_secret_optional(self):
        inputs = self.doc["on"]["workflow_call"]["inputs"] if "on" in self.doc else self.doc[True]["workflow_call"]["inputs"]
        self.assertIs(inputs["enabled"]["default"], False)
        self.assertEqual("grok-4.6", inputs["model"]["default"])
        self.assertEqual("xhigh", inputs["effort"]["default"])
        secrets = (self.doc.get("on") or self.doc.get(True))["workflow_call"]["secrets"]
        self.assertIs(secrets["CURSOR_CLOUD_AGENTS_API_KEY"]["required"], False)
        self.assertEqual("${{ inputs.enabled }}", self.job["if"])

    def test_permissions_are_exactly_what_posting_a_review_needs(self):
        self.assertEqual({"contents": "read", "pull-requests": "write"}, self.doc["permissions"])
        self.assertEqual({"contents": "read", "pull-requests": "write"}, self.job["permissions"])

    def test_the_wait_budget_fits_inside_the_job_timeout(self):
        inputs = (self.doc.get("on") or self.doc.get(True))["workflow_call"]["inputs"]
        self.assertLess(inputs["wait-minutes"]["default"], self.job["timeout-minutes"])

    def test_the_script_step_wires_every_env_the_script_reads(self):
        step = self.job["steps"][-1]
        source = (ROOT / "scripts" / "cursor_review.py").read_text(encoding="utf-8")
        read = set(re.findall(r'env(?:\.get)?\(\s*"([A-Z_]+)"', source)) | set(re.findall(r'env\["([A-Z_]+)"\]', source))
        read -= {"GITHUB_STEP_SUMMARY", "GITHUB_API_URL", "CURSOR_API_URL", "GITHUB_REPOSITORY"}
        missing = sorted(read - set(step["env"]))
        self.assertEqual([], missing, f"script reads env the step never sets: {missing}")
        self.assertEqual("${{ secrets.CURSOR_CLOUD_AGENTS_API_KEY }}", step["env"]["CURSOR_CLOUD_AGENTS_API_KEY"])
        self.assertIn("cursor_review.py", step["run"])

    def test_the_checkout_pins_the_callables_own_commit(self):
        checkout = self.job["steps"][0]
        self.assertEqual("narduk-enterprises/workflows", checkout["with"]["repository"])
        self.assertEqual("${{ fromJSON(toJSON(github)).job_workflow_sha }}", checkout["with"]["ref"])
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_concurrency_is_job_level_and_keyed_by_pull_request(self):
        self.assertNotIn("concurrency", self.doc)
        self.assertIn("github.event.pull_request.number", self.job["concurrency"]["group"])
        self.assertIs(self.job["concurrency"]["cancel-in-progress"], True)

    def test_the_brief_contracts_the_output_shape_the_parser_reads(self):
        brief = BRIEF.read_text(encoding="utf-8")
        for token in ('"verdict"', '"findings"', '"severity"', '"checks_run"', "approve | comment | request_changes", "{head_sha}", "{base_sha}", "{context_repos}"):
            self.assertIn(token, brief)
        self.assertIn("UNTRUSTED", brief)


if __name__ == "__main__":
    unittest.main()
