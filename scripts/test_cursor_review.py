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
  * A head that moved while the agent ran posts nothing and tells the lane to
    add `review-now`, and a reviewer error makes the job red, never silently
    green.
  * The callable itself passes the repository's structural rules and wires
    every value the script reads.

Run: python3 scripts/test_cursor_review.py  (needs PyYAML, as every test here does)
"""

from __future__ import annotations

import contextlib
import datetime
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
SELF_CALLER = ROOT / ".github" / "workflows" / "cursor-review-self.yml"
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

    def __init__(self, *, result: str = review_json(), run_statuses: list[str] | None = None, pr_author: str = "loganrenz", head_now: str = HEAD, prior_reviews: list[dict[str, Any]] | None = None, marker_comment: dict[str, Any] | None = None, review_status: int = 200, files: list[dict[str, Any]] | None = None, agent_ledger: list[dict[str, Any]] | None = None, compare_commits: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.run_statuses = list(run_statuses or ["RUNNING", "FINISHED"])
        self.pr_author = pr_author
        self.head_now = head_now
        self.prior_reviews = prior_reviews or []
        self.marker_comment = marker_comment
        self.review_status = review_status
        self.cancelled: list[str] = []
        self.labels_removed: list[str] = []
        self.files = list(files) if files is not None else [{"filename": "src/a.ts", "patch": PATCH, "additions": 40, "deletions": 5}, {"filename": "bin/blob", "patch": None}]
        # `GET /v1/agents` -- the daily-cap ledger. Empty by default, so the
        # cap is exercised on every run and never binds unless a test says so.
        self.agent_ledger = list(agent_ledger or [])
        # `GET /compare/<old>...<new>` -- the delta gate. None means the
        # comparison is unreadable, which must fail open.
        self.compare_files: list[dict[str, Any]] | None = [{"filename": "src/a.ts", "additions": 40, "deletions": 5}]
        # Same endpoint's `commits` array -- the moved-head reviewer-push
        # check (`reviewer_pushed_commits`). Empty by default: no test here
        # exercises a moved head unless it sets this explicitly.
        self.compare_commits = list(compare_commits) if compare_commits is not None else []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int) -> tuple[int, Any]:
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": headers, "body": payload})
        path = url.split("?")[0]
        if url.startswith(cr.CURSOR_API):
            return self.cursor(method, path, payload)
        return self.github(method, path, payload)

    def cursor(self, method: str, path: str, payload: Any) -> tuple[int, Any]:
        if method == "GET" and path.endswith("/v1/agents"):
            # VERIFIED live 2026-09-20: the top-level keys are `items` and
            # `nextCursor`. This fake previously echoed `agents`, the v0 name
            # the launcher also guessed, so the daily cap passed its tests
            # while never binding in production. Keep this key honest.
            return 200, {"items": self.agent_ledger, "nextCursor": None}
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
            return 200, self.files
        if method == "DELETE" and "/issues/7/labels/" in path:
            self.labels_removed.append(path.rsplit("/", 1)[1])
            return 204, None
        if method == "GET" and "/compare/" in path:
            if self.compare_files is None:
                return 500, {"message": "boom"}
            return 200, {"files": self.compare_files, "commits": self.compare_commits}
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
        "PR_AUTHOR": "loganrenz",
        "PR_LABELS": "[]",
        "PR_EVENT_ACTION": "opened",
        "PR_EVENT_LABEL": "",
        "CURSOR_MODEL": "grok-4.6",
        "CURSOR_EFFORT": "xhigh",
        "CURSOR_FAST": "false",
        "CONTEXT_REPOS": "narduk-enterprises/agent-infrastructure,narduk-enterprises/company-hq",
        "WAIT_MINUTES": "30",
        # Exercise optional automatic limits; the production defaults are unlimited.
        "MAX_ROUNDS": "4",
        "DAILY_CAP": "12",
        "APPROVE_ON_CLEAN": "true",
        "BRIEF_PATH": str(BRIEF),
    }
    value.update(overrides)
    return value


def run(transport: FakeTransport, **overrides: str) -> tuple[int, str]:
    clock = iter(range(0, 100000, 10))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cr.run(env(**overrides), transport, sleep=lambda _s: None, clock=lambda: float(next(clock)), now=lambda: NOW)
    return code, out.getvalue()


NOW = 1_790_000_000.0


def ledger(count: int, *, repo: str = REPO, age_hours: float = 1.0) -> list[dict[str, Any]]:
    """`count` review agents for `repo`, all `age_hours` old. Newest-first."""
    stamp = datetime.datetime.fromtimestamp(NOW - age_hours * 3600, datetime.timezone.utc)
    return [{"id": f"bc-{i}", "name": f"review {repo}#{i} @abc1234", "createdAt": stamp.isoformat().replace("+00:00", "Z")} for i in range(count)]


def marker_comment(rounds: int, *, head: str = HEAD, agent: str = "bc-old") -> dict[str, Any]:
    return {"id": 1, "user": {"login": cr.BOT_LOGIN}, "body": cr.agent_marker(agent, head, rounds) + "\nprior"}


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
        self.assertEqual([{"id": "effort", "value": "xhigh"}, {"id": "fast", "value": "false"}], body["model"]["params"])
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

    def test_a_head_moved_by_the_reviewer_itself_fails_the_job_loudly(self):
        """round-4 re-verify MEDIUM finding: the b73f49 incident shape (the
        reviewer pushes a commit onto the PR's own head while its review is
        in flight) must not be silently absorbed as an ordinary supersede --
        it has to fail the job and say so, since `workOnCurrentBranch=False`
        alone is unproven as a real credential boundary."""
        transport = FakeTransport(
            head_now="d" * 40,
            compare_commits=[{"sha": "b73f49" + "0" * 34, "author": {"login": "cursoragent"}, "commit": {"author": {"name": "Cursor Agent"}}}],
        )
        with self.assertRaisesRegex(cr.ReviewError, "b73f49 shape"):
            run(transport)
        self.assertFalse(any(c["url"].endswith("/pulls/7/reviews") and c["method"] == "POST" for c in transport.calls))
        last_patch = [c for c in transport.calls if c["method"] == "PATCH"][-1]
        self.assertIn("did not complete", last_patch["body"]["body"])

    def test_a_head_moved_by_someone_else_still_only_skips(self):
        """The same moved-head shape, but the new commit is NOT the
        reviewer's own -- an ordinary human push race, which must stay a
        quiet Skip exactly as before."""
        transport = FakeTransport(
            head_now="d" * 40,
            compare_commits=[{"sha": "c" * 40, "author": {"login": "loganrenz"}, "commit": {"author": {"name": "Logan Renz"}}}],
        )
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("moved to", out)

    def test_reviewer_pushed_commits_fails_open_when_the_compare_is_unreadable(self):
        github = cr.GitHub("tok", REPO, lambda *a, **k: (500, {"message": "boom"}))
        self.assertEqual([], cr.reviewer_pushed_commits(github, BASE, HEAD))

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
        self.assertIn("add the `review-now` label", last_patch["body"]["body"])
        self.assertNotIn("push a new head", last_patch["body"]["body"])
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



class RetryTests(unittest.TestCase):
    """A single transient 5xx on an idempotent read must not fail the job
    (agent-infrastructure#1569: `GitHub GET pulls/1569 returned HTTP 504`),
    and no write may ever be sent twice."""

    def github(self, replies, slept):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            reply = replies[min(len(calls) - 1, len(replies) - 1)]
            if isinstance(reply, Exception):
                raise reply
            return reply

        return cr.GitHub("t", REPO, transport, sleep=slept.append), calls

    def test_a_get_recovers_after_two_gateway_failures_and_backs_off(self):
        slept = []
        replies = [(504, {"message": "gateway"}), (504, {"message": "gateway"}), (200, {"number": 1569})]
        github, calls = self.github(replies, slept)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual({"number": 1569}, github.call("GET", "pulls/1569"))
        self.assertEqual(3, len(calls))
        self.assertEqual([2, 4], slept)

    def test_a_transport_failure_is_retried_the_same_way(self):
        slept = []
        replies = [cr.ReviewError("GET transport failed: URLError"), (200, {"ok": True})]
        github, calls = self.github(replies, slept)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual({"ok": True}, github.call("GET", "pulls/1569"))
        self.assertEqual(2, len(calls))
        self.assertEqual([2], slept)

    def test_four_gateway_failures_raise_the_same_review_error(self):
        slept = []
        github, calls = self.github([(504, {"message": "gateway"})], slept)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(cr.ReviewError, "GitHub GET pulls/1569 returned HTTP 504") as caught:
                github.call("GET", "pulls/1569")
        self.assertEqual(504, caught.exception.status)
        self.assertEqual(cr.RETRY_ATTEMPTS, len(calls))
        self.assertEqual([2, 4, 8], slept)

    def test_a_write_is_never_retried(self):
        slept = []
        github, calls = self.github([(503, {"message": "unavailable"})], slept)
        with self.assertRaisesRegex(cr.ReviewError, "HTTP 503") as caught:
            github.call("POST", "pulls/7/reviews", {"event": "COMMENT"})
        self.assertEqual(503, caught.exception.status)
        self.assertEqual(1, len(calls), "a retried POST could double-post a review")
        self.assertEqual([], slept)

    def test_a_cursor_refusal_says_why_instead_of_none(self):
        """agent-infrastructure lanes read `HTTP 400: None` for a whole
        afternoon on 2026-09-19; the refusal was a billing quota and the body
        said so."""
        refusal = {"error": {"code": "usage_limit_exceeded", "message": "Usage-based pricing required. Background Agent requires at least $2 remaining until your hard limit."}}
        cursor = cr.Cursor("k", lambda *a: (400, refusal), sleep=lambda _s: None)
        with self.assertRaises(cr.ReviewError) as caught:
            cursor.call("POST", "/v1/agents", {"prompt": {}})
        self.assertIn("usage_limit_exceeded", str(caught.exception))
        self.assertIn("Usage-based pricing required", str(caught.exception))
        self.assertNotIn("None", str(caught.exception))
        self.assertEqual(400, caught.exception.status)

    def test_an_empty_error_body_says_it_was_empty(self):
        cursor = cr.Cursor("k", lambda *a: (400, None), sleep=lambda _s: None)
        with self.assertRaisesRegex(cr.ReviewError, "HTTP 400: <empty response body>"):
            cursor.call("POST", "/v1/agents", {"prompt": {}})

    def test_the_cursor_poll_gets_the_same_retries_and_its_launch_does_not(self):
        slept = []
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            if method == "POST":
                return 502, {"message": "bad gateway"}
            return (502, {"message": "bad gateway"}) if len(calls) < 3 else (200, {"status": "FINISHED"})

        cursor = cr.Cursor("k", transport, sleep=slept.append)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual({"status": "FINISHED"}, cursor.call("GET", "/v1/agents/bc-1"))
        self.assertEqual(3, len(calls))
        self.assertEqual([2, 4], slept)
        with self.assertRaisesRegex(cr.ReviewError, "HTTP 502"):
            cursor.call("POST", "/v1/agents", {"prompt": {}})
        self.assertEqual(4, len(calls), "a retried launch could start a second agent")


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
        self.assertEqual({"agentId": "bc-1", "headSha": HEAD, "rounds": 0}, cr.parse_agent_marker(body))
        self.assertEqual(3, cr.marker_rounds(cr.parse_agent_marker(cr.agent_marker("bc-1", HEAD, 3))))
        # A marker written before `rounds` existed must parse, and must not
        # retro-cap a pull request on a number nobody recorded.
        self.assertEqual(0, cr.marker_rounds({"agentId": "bc-1", "headSha": HEAD}))
        self.assertEqual(0, cr.marker_rounds({"agentId": "bc-1", "rounds": "seven"}))
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
        # Composer 2.5 is in the SAME first-party pool as Grok 4.6 at about a
        # third of the cost per review; `review-deep` buys the stronger model.
        self.assertEqual("composer-2.5", inputs["model"]["default"])
        # EMPTY, not "xhigh": composer-2.5 defines only `fast` and rejects an
        # `effort` parameter it does not have, so a non-empty default here
        # would make every ordinary review fail at launch.
        self.assertEqual("", inputs["effort"]["default"])
        self.assertEqual("grok-4.6", inputs["deep-model"]["default"])
        self.assertEqual("xhigh", inputs["deep-effort"]["default"])
        self.assertEqual(0, cr.DEFAULT_MAX_ROUNDS)
        self.assertEqual(0, cr.DEFAULT_DAILY_CAP)
        # fast=false for reviews: the job already budgets wait-minutes and
        # blocks nobody, so the reviewer is never the latency-critical lane.
        # For composer this is also a COST trap, not just latency: its own
        # default variant is fast=true, billed at 6x base ($3/$0.50/$15),
        # which is more expensive than the grok-4.6 it replaced.
        self.assertIs(inputs["fast"]["default"], False)
        self.assertEqual(0, inputs["max-rounds"]["default"])
        self.assertEqual(0, inputs["daily-cap"]["default"])
        self.assertEqual(20, inputs["min-lines"]["default"])
        self.assertEqual(10, inputs["min-delta-lines"]["default"])
        secrets = (self.doc.get("on") or self.doc.get(True))["workflow_call"]["secrets"]
        self.assertIs(secrets["CURSOR_CLOUD_AGENTS_API_KEY"]["required"], False)
        # `inputs.enabled` alone would let a `bot-inbox` label reach the job,
        # claim its concurrency group, and cancel the in-flight review before
        # the script could print a skip.
        self.assertIn("inputs.enabled", self.job["if"])
        self.assertIn("github.event.action == 'opened'", self.job["if"])
        self.assertNotIn("synchronize", self.job["if"])
        self.assertIn("github.event.pull_request.draft == false", self.job["if"])
        self.assertIn("'no-ai-review'", self.job["if"])
        # The documented caller group must MIRROR that `if:`, or a run-level
        # cancel lands on a job the `if:` then skips -- cancel-then-skip.
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        group = [line for line in readme.splitlines() if "cursor-review-caller-" in line and "group:" in line]
        self.assertTrue(group, "the README caller shape must show its concurrency group")
        for predicate in ("github.event.pull_request.draft == false", "'no-ai-review'", "'opened'", "'reopened'", "'ready_for_review'", '"review-now","review-p0","review-p1"'):
            self.assertIn(predicate, group[0], f"caller group is missing {predicate}")
        for label in ("review-now", "review-p0", "review-p1"):
            self.assertIn(label, self.job["if"])

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


class LabelAllowListTests(unittest.TestCase):
    """The Python label set and every YAML allow-list must not drift apart.

    `review-deep` shipped in RE_REQUEST_LABELS while all three YAML lists still
    named only three labels, so the escape hatch could never start a review:
    the job `if:` skipped the event before any Python ran. Nothing failed --
    the launcher's own tests never read the workflow. This binds them.
    """

    LIST = re.compile(r"fromJSON\('(\[[^']*\])'\)")

    def allow_lists(self):
        found = []
        for path in (WORKFLOW, SELF_CALLER):
            for raw in self.LIST.findall(path.read_text(encoding="utf-8")):
                found.append((path.name, frozenset(json.loads(raw))))
        return found

    def test_every_yaml_allow_list_matches_the_python_label_set(self):
        found = self.allow_lists()
        # The job `if:`, the copy-paste caller template beside it, and the
        # self-caller's own concurrency group.
        self.assertEqual(3, len(found), f"expected three allow-lists, got {[n for n, _ in found]}")
        for name, labels in found:
            self.assertEqual(
                set(cr.RE_REQUEST_LABELS),
                set(labels),
                f"{name} allow-list drifted from RE_REQUEST_LABELS",
            )

    def test_the_deep_label_can_actually_wake_a_skipped_job(self):
        # The specific regression: adding `review-deep` must reach the job.
        self.assertIn(cr.DEEP_LABEL, cr.RE_REQUEST_LABELS)
        for name, labels in self.allow_lists():
            self.assertIn(cr.DEEP_LABEL, labels, f"{name} cannot wake a deep review")

    def test_the_caller_group_mirrors_the_job_condition(self):
        # cancel-in-progress is true and the group splits review from other.
        # A label the job accepts but the group buckets as 'other' gets its
        # live run cancelled by the next stray event, before the job `if:` is
        # evaluated -- which also means `cancel_previous` never runs and the
        # paid Cursor agent behind it keeps going.
        caller = SELF_CALLER.read_text(encoding="utf-8")
        self.assertIn("cancel-in-progress: true", caller)
        job_if = [l for l in WORKFLOW.read_text(encoding="utf-8").splitlines() if l.strip().startswith("if: ${{ inputs.enabled")]
        self.assertEqual(1, len(job_if))
        group = [l for l in caller.splitlines() if l.strip().startswith("group: cursor-review-caller-")]
        self.assertEqual(1, len(group))
        for clause in ("github.event.pull_request.draft == false", "no-ai-review", "ready_for_review"):
            self.assertIn(clause, job_if[0])
            self.assertIn(clause, group[0], f"the caller group lost the {clause!r} clause")


class ClassRuleTests(unittest.TestCase):
    """The P0/P1/P2 rule, Logan 2026-09-19: "No numeric cap, only the P0/P1/P2
    class rule". P2 must never reach `POST /v1/agents`; P0 must always reach
    it; `review-now` must defeat every inferred P2 signal."""

    def launched(self, transport: FakeTransport) -> bool:
        return any(c["method"] == "POST" and c["url"].endswith("/v1/agents") for c in transport.calls)

    def test_p2_never_launches_an_agent(self):
        for label, overrides, files in (
            ("dependabot author", {"PR_AUTHOR": "dependabot[bot]"}, None),
            ("actions author", {"PR_AUTHOR": "github-actions[bot]"}, None),
            ("changeset release head", {"PR_HEAD_REF": "changeset-release/main"}, None),
            ("explicit label", {"PR_LABELS": '["review-p2"]'}, None),
            ("metadata-only diff", {}, [{"filename": "README.md", "patch": PATCH}, {"filename": "LICENSE", "patch": None}]),
        ):
            with self.subTest(label):
                transport = FakeTransport(files=files)
                code, out = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertIn("::notice::review skipped: class P2", out)
                self.assertFalse(self.launched(transport), "a P2 pull request must launch no agent")

    def test_review_now_overrides_every_p2_signal(self):
        for label, overrides, files in (
            ("dependabot author", {"PR_AUTHOR": "dependabot[bot]"}, None),
            ("changeset release head", {"PR_HEAD_REF": "changeset-release/main"}, None),
            ("explicit label", {"PR_LABELS": '["review-p2", "review-now"]'}, None),
            ("metadata-only diff", {}, [{"filename": "README.md", "patch": PATCH}]),
        ):
            with self.subTest(label):
                overrides.setdefault("PR_LABELS", '["review-now"]')
                transport = FakeTransport(files=files)
                code, _ = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertTrue(self.launched(transport), "review-now must defeat the P2 signal")

    def test_a_chore_title_on_a_code_change_is_still_reviewed(self):
        """The title is author-controlled and estate lanes use `chore:` on code
        pull requests; only the diff decides the class."""
        for title in ("chore: bump the lockfile", "docs(readme): typo", "nit: rename a local"):
            with self.subTest(title):
                transport = FakeTransport()
                code, out = run(transport, PR_TITLE=title)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P1", out)
                self.assertTrue(self.launched(transport))

    def test_p0_outranks_every_p2_skip_signal(self):
        """A workflow diff is the deleted-`on:`-block class this reviewer gates,
        so neither an automation author nor a `review-p2` label may skip it."""
        workflow = [{"filename": ".github/workflows/ci.yml", "patch": PATCH}]
        for label, overrides in (
            ("dependabot bumping a pinned action", {"PR_AUTHOR": "dependabot[bot]"}),
            ("github-actions author", {"PR_AUTHOR": "github-actions[bot]"}),
            ("changeset release head", {"PR_HEAD_REF": "changeset-release/main"}),
            ("review-p2 on a workflow change", {"PR_LABELS": '["review-p2"]'}),
        ):
            with self.subTest(label):
                transport = FakeTransport(files=workflow)
                code, out = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)
                self.assertTrue(self.launched(transport), "a P0 diff must launch whatever the P2 signal says")

    def test_the_review_p0_label_outranks_every_p2_skip_signal(self):
        for label, overrides in (
            ("dependabot author", {"PR_AUTHOR": "dependabot[bot]", "PR_LABELS": '["review-p0"]'}),
            ("changeset release head", {"PR_HEAD_REF": "changeset-release/main", "PR_LABELS": '["review-p0"]'}),
            ("review-p2 alongside", {"PR_LABELS": '["review-p2", "review-p0"]'}),
            ("metadata-only diff", {"PR_LABELS": '["review-p0"]'}),
        ):
            with self.subTest(label):
                files = [{"filename": "README.md", "patch": PATCH}] if label == "metadata-only diff" else None
                transport = FakeTransport(files=files)
                code, out = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)
                self.assertTrue(self.launched(transport))

    def test_an_unknown_file_list_defeats_every_p2_skip_signal(self):
        """`changed_paths` returning None means the API could not finish
        listing the diff, so nothing is known about whether a workflow file is
        in there. A dependabot pull request with 300 files is exactly that
        shape, and it is the one that hides a deleted `on:` block."""

        class Broken(FakeTransport):
            """Only the classification read fails; `post_review` still needs a
            diff index to place its inline comments."""

            seen = False

            def github(self, method, path, payload):
                if method == "GET" and path.endswith("/pulls/7/files") and not self.seen:
                    self.seen = True
                    return 500, {"message": "boom"}
                return super().github(method, path, payload)

        for label, overrides in (
            ("dependabot author", {"PR_AUTHOR": "dependabot[bot]"}),
            ("actions author", {"PR_AUTHOR": "github-actions[bot]"}),
            ("changeset release head", {"PR_HEAD_REF": "changeset-release/main"}),
            ("review-p2 label", {"PR_LABELS": '["review-p2"]'}),
        ):
            with self.subTest(label):
                transport = Broken()
                code, out = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertIn("classifying this pull request as reviewable", out)
                self.assertIn("::notice::review class P1 (unknown file list", out)
                self.assertTrue(self.launched(transport), "an unknown file list must never take a P2 skip")

    def test_a_diff_past_the_page_bound_is_reviewed_not_skipped(self):
        """`GitHub.pages` gives up after `cr.CHANGED_FILE_PAGES` full pages; the
        rows already fetched are discarded, so the class must fail open."""

        class Paged(FakeTransport):
            """Three full pages exhaust the bound; the fourth read is
            `post_review` building its diff index and may be short."""

            pages_served = 0

            def github(self, method, path, payload):
                if method == "GET" and path.endswith("/pulls/7/files"):
                    self.pages_served += 1
                    if self.pages_served <= cr.CHANGED_FILE_PAGES:
                        return 200, [{"filename": f"pkg/f{i}.ts", "patch": PATCH} for i in range(100)]
                    return 200, []
                return super().github(method, path, payload)

        transport = Paged()
        code, out = run(transport, PR_AUTHOR="dependabot[bot]")
        self.assertEqual(0, code)
        self.assertIn("::notice::review class P1 (unknown file list", out)
        self.assertTrue(self.launched(transport))

    def test_codeowners_and_ignore_files_are_code_not_prose(self):
        """A CODEOWNERS edit can drop required reviewers and a `.gitignore`
        edit can stop ignoring secret material; neither is a README typo."""
        for path in (".github/CODEOWNERS", "CODEOWNERS", ".gitignore", ".gitattributes"):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH}])
                code, out = run(transport)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P1", out)
                self.assertTrue(self.launched(transport))

    def test_review_p1_wakes_the_reviewer_it_is_allowed_to_wake(self):
        """`review-p1` is in RE_REQUEST_LABELS, the job `if:` and the caller
        concurrency group, so adding it cancels the in-flight waiter. It must
        therefore beat the inferred P2 signals as well, or the add is a
        cancel-then-skip."""
        for label, overrides in (
            ("dependabot author", {"PR_AUTHOR": "dependabot[bot]", "PR_LABELS": '["review-p1"]'}),
            ("actions author", {"PR_AUTHOR": "github-actions[bot]", "PR_LABELS": '["review-p1"]'}),
            ("changeset release head", {"PR_HEAD_REF": "changeset-release/main", "PR_LABELS": '["review-p1"]'}),
            ("review-p2 alongside", {"PR_LABELS": '["review-p2", "review-p1"]'}),
            ("metadata-only diff", {"PR_LABELS": '["review-p1"]'}),
        ):
            with self.subTest(label):
                files = [{"filename": "README.md", "patch": PATCH}] if label == "metadata-only diff" else None
                transport = FakeTransport(files=files)
                code, out = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P1 (label 'review-p1')", out)
                self.assertTrue(self.launched(transport))

    def test_a_skipped_class_still_cancels_the_previous_agent(self):
        """Job concurrency kills this pull request's in-flight waiter before
        the script runs. If the skip returned without cancelling, the agent
        that waiter was watching would keep burning the pool and post
        nothing."""
        marker = {"id": 77, "user": {"login": cr.BOT_LOGIN}, "body": cr.agent_marker("bc-old", "c" * 40) + "\nin progress"}
        transport = FakeTransport(marker_comment=marker, files=[{"filename": "README.md", "patch": PATCH}])
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("::notice::review skipped: class P2", out)
        self.assertFalse(self.launched(transport))
        self.assertEqual(["bc-old"], transport.cancelled, "a skip owes the same cleanup a launch does")

    def test_a_same_head_agent_is_cancelled_too(self):
        """`review-now` IS the same-head re-request, so a SHA comparison would
        exempt exactly the run that most needs the cleanup."""
        marker = {"id": 77, "user": {"login": cr.BOT_LOGIN}, "body": cr.agent_marker("bc-old", HEAD) + "\nin progress"}
        with self.subTest("skip"):
            transport = FakeTransport(marker_comment=marker, files=[{"filename": "README.md", "patch": PATCH}])
            code, _ = run(transport)
            self.assertEqual(0, code)
            self.assertFalse(self.launched(transport))
            self.assertEqual(["bc-old"], transport.cancelled)
        with self.subTest("review-now relaunch"):
            transport = FakeTransport(marker_comment=marker)
            code, _ = run(transport, PR_LABELS='["review-now"]', PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-now")
            self.assertEqual(0, code)
            self.assertTrue(self.launched(transport))
            self.assertEqual(["bc-old"], transport.cancelled)

    def test_a_skip_stays_green_when_the_comments_api_fails(self):
        """The cleanup is best-effort: a skipped class has nothing to report,
        so a comments-API failure must not turn a contracted-green job red."""

        class NoComments(FakeTransport):
            def github(self, method, path, payload):
                if method == "GET" and "/issues/7/comments" in path:
                    return 500, {"message": "boom"}
                return super().github(method, path, payload)

        transport = NoComments(files=[{"filename": "README.md", "patch": PATCH}])
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("could not load the previous agent marker", out)
        self.assertIn("::notice::review skipped: class P2", out)
        self.assertFalse(self.launched(transport))

    def test_policy_markdown_is_never_prose(self):
        """A SKILL.md edit can drop a review-follow-through rule and a
        DECISIONS.md edit can invent an approval; agent-infrastructure is an
        enrolled caller, so these land here.

        This is the invariant the 2026-09-20 P0 narrowing had to preserve.
        `.md` is a metadata suffix, so a policy file that merely stopped being
        P0 would have fallen through to the all-metadata P2 skip and received
        NO review at all -- turning "always reviewed" into "never reviewed" in
        a repository whose product IS its policy prose. Policy paths are P1:
        reviewed, and subject to the spend limits like any other change.
        """
        for path in (
            "skills/repo-hygiene-execute/SKILL.md",
            "harness/claude.md",
            "harness/codex.md",
            "docs/agents/credentials.md",
            "AGENTS.md",
            "nested/dir/CLAUDE.md",
        ):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH}])
                code, out = run(transport)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P1", out)
                self.assertNotIn("class P2", out)
                self.assertTrue(self.launched(transport))

    def test_the_two_self_governing_files_stay_p0(self):
        """Narrowing kept exactly the two that rewrite the rules this reviewer
        runs by: DECISIONS.md can invent an approval the human-approval
        citation gate would then honour, and cursor_review_brief.md is the
        reviewer editing its own brief."""
        for path in ("DECISIONS.md", "scripts/cursor_review_brief.md"):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH}])
                code, out = run(transport)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)
                self.assertTrue(self.launched(transport))

    def test_dependency_and_build_manifests_are_not_prose(self):
        """`requirements.txt` is installable input, not a README."""
        for path in ("requirements.txt", "requirements-dev.txt", "constraints.txt", "CMakeLists.txt"):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH}])
                code, out = run(transport)
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P1", out)
                self.assertTrue(self.launched(transport))

    def test_a_single_label_is_read_whatever_shape_the_splat_takes(self):
        """`PR_LABELS` is a `toJSON` splat. A bare JSON string must read as one
        label, and the `labeled` event's own label counts regardless."""
        for label, overrides in (
            ("json string", {"PR_LABELS": '"review-now"', "PR_AUTHOR": "dependabot[bot]"}),
            ("empty labels, event label only", {"PR_LABELS": "[]", "PR_AUTHOR": "dependabot[bot]", "PR_EVENT_ACTION": "labeled", "PR_EVENT_LABEL": "review-now"}),
            ("malformed labels, event label only", {"PR_LABELS": "not json", "PR_AUTHOR": "dependabot[bot]", "PR_EVENT_ACTION": "labeled", "PR_EVENT_LABEL": "review-now"}),
        ):
            with self.subTest(label):
                transport = FakeTransport()
                code, _ = run(transport, **overrides)
                self.assertEqual(0, code)
                self.assertTrue(self.launched(transport), "review-now must override the automation author")
                self.assertEqual([cr.REVIEW_NOW_LABEL], transport.labels_removed, "and must consume its own event")

    def test_review_now_is_cleared_before_anything_that_can_fail(self):
        """`find_marker_comment` can raise too; if the label outlived it, the
        lane could never re-request."""

        class NoComments(FakeTransport):
            def github(self, method, path, payload):
                if method == "GET" and "/issues/7/comments" in path:
                    return 500, {"message": "boom"}
                return super().github(method, path, payload)

        transport = NoComments()
        with self.assertRaisesRegex(cr.ReviewError, "HTTP 500"):
            run(transport, PR_LABELS='["review-now"]', PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-now")
        self.assertEqual([cr.REVIEW_NOW_LABEL], transport.labels_removed)

    def test_ordinary_prose_is_still_p2(self):
        """The policy names are an exception to the `.md` default, not a
        repeal of it."""
        for path in ("README.md", "docs/architecture.md", "CHANGELOG.md", "LICENSE", "NOTICE", "notes.rst"):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH}])
                code, out = run(transport)
                self.assertEqual(0, code)
                self.assertIn("::notice::review skipped: class P2", out)
                self.assertFalse(self.launched(transport))

    def test_synchronize_is_refused_by_the_script_too(self):
        """Every enrolled caller still carried `synchronize` when this landed.
        A pin-only follow-up must not recreate the push-driven storm."""
        for action in ("synchronize", "edited", "assigned"):
            with self.subTest(action):
                transport = FakeTransport()
                code, out = run(transport, PR_EVENT_ACTION=action)
                self.assertEqual(0, code)
                self.assertIn("does not request a review", out)
                self.assertEqual([], transport.calls, "a refused event must cost no network call")

    def test_the_opening_actions_still_review(self):
        for action in ("opened", "reopened", "ready_for_review", ""):
            with self.subTest(action or "(no action)"):
                transport = FakeTransport()
                code, _ = run(transport, PR_EVENT_ACTION=action)
                self.assertEqual(0, code)
                self.assertTrue(self.launched(transport))

    def test_review_now_is_cleared_even_when_cursor_refuses(self):
        """`usage_limit_exceeded` is the failure this change exists to survive.
        If the label outlived a refused launch, re-adding it would raise no
        `labeled` event and the lane could never ask again."""

        class Refusing(FakeTransport):
            def cursor(self, method, url, payload):
                if method == "POST" and url.endswith("/v1/agents"):
                    return 400, {"error": "usage_limit_exceeded"}
                return super().cursor(method, url, payload)

        transport = Refusing()
        with self.assertRaisesRegex(cr.ReviewError, "usage_limit_exceeded"):
            run(transport, PR_LABELS='["review-now"]', PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-now")
        self.assertEqual([cr.REVIEW_NOW_LABEL], transport.labels_removed, "the labeled event must be consumed even when Cursor refuses")

    def test_a_rename_out_of_an_always_review_path_is_still_p0(self):
        """`.github/workflows/ci.yml` -> `docs/old-ci.md` reads as metadata-only
        unless the rename's previous_filename is counted."""
        transport = FakeTransport(files=[{"filename": "docs/old-ci.md", "previous_filename": ".github/workflows/ci.yml", "patch": PATCH}])
        code, out = run(transport, PR_TITLE="docs: move the old gate")
        self.assertEqual(0, code)
        self.assertIn("::notice::review class P0", out)
        self.assertTrue(self.launched(transport))

    def test_p0_paths_are_reviewed_whatever_the_title_says(self):
        for path in (".github/workflows/ci.yml", ".github/actions/setup/action.yml"):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH}])
                code, out = run(transport, PR_TITLE="docs: routine wording")
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)
                self.assertTrue(self.launched(transport))
                self.assertIn("class P0", posted_review(transport)["body"])

    def test_t2_sensitive_paths_are_always_reviewed(self):
        """narduk-reboot P3-C2 / O-D7: auth, session, payments and
        credential-table paths always get a review, like ALWAYS_REVIEW_PREFIXES,
        whatever the label, author or diff size."""
        for path in (
            "src/routes/auth/login.ts",
            "lib/session/store.ts",
            "app/payments/checkout.vue",
            "Config/nvault-provider-credentials.json",
        ):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 1, "deletions": 0}])
                code, out = run(transport, PR_TITLE="chore: routine wording", PR_AUTHOR="dependabot[bot]")
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)
                self.assertTrue(self.launched(transport))

    def test_t2_marker_substrings_are_not_p0(self):
        """PR #142 review, cursor_review.py:644 (blocking): a bare `marker in
        lowered` substring check made `author`, `authoring` and `repayment`
        all P0. is_t2_sensitive() now matches whole path tokens, so an
        ordinary word that merely contains a T2 marker's letters stays P1
        (and can still be skipped by the P2 signals, unlike a real P0)."""
        for path in (
            "src/author/service.ts",
            "docs/authoring.md",
            "lib/repayment/calc.ts",
        ):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 1, "deletions": 0}])
                code, out = run(transport, PR_TITLE="chore: routine wording")
                self.assertEqual(0, code)
                self.assertNotIn("::notice::review class P0", out)

    def test_t2_camel_case_and_derived_forms_are_p0(self):
        """PR #142 review, second round: whole-TOKEN matching (casefold()
        then split) stopped catching compound identifiers because casefold()
        destroys the camelCase/PascalCase boundary a split needs. Also
        covers the reviewer's own CONSIDER finding (cursor_review.py:119):
        derived forms like "authentication"/"authorization"/"oauth" that
        the original bare word list did not enumerate."""
        for path in (
            "app/composables/useAuth.ts",
            "app/stores/authStore.ts",
            "server/middleware/authMiddleware.ts",
            "server/utils/sessions.ts",
            "lib/oauth/callback.ts",
            "src/authentication/index.ts",
            "src/authorization/policy.ts",
            "SessionStore.swift",
        ):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 1, "deletions": 0}])
                code, out = run(transport, PR_TITLE="chore: routine wording", PR_AUTHOR="dependabot[bot]")
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)

    def test_t2_acronym_prefixed_words_are_p0(self):
        """PR #142 review, third round (LOW): the lower-to-upper camelCase
        split never fires inside a run of capitals, so an acronym glued
        straight onto a T2 word -- `HTTPAuth.ts`, `SSOAuth.ts`,
        `JWTSession.ts` -- fell through as a single unsplit word and stayed
        P1. `_ACRONYM_SPLIT` closes that: it splits the acronym from the
        capitalised word that follows it, same as a plain camelCase word."""
        for path in (
            "src/HTTPAuth.ts",
            "lib/SSOAuth.ts",
            "server/JWTSession.ts",
        ):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 1, "deletions": 0}])
                code, out = run(transport, PR_TITLE="chore: routine wording", PR_AUTHOR="dependabot[bot]")
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)

    def test_t2_derived_false_positive_words_stay_p1(self):
        """The camelCase/prefix widening must not reopen the exact
        false-positive class test_t2_marker_substrings_are_not_p0 closed:
        ordinary English words built on a T2 root stay P1 even when they
        appear as their own path segment or inside a compound identifier."""
        for path in (
            "docs/authorship.md",
            "finance/repayments/schedule.ts",
            "billing/PrepaymentPlan.ts",
        ):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 1, "deletions": 0}])
                code, out = run(transport, PR_TITLE="chore: routine wording")
                self.assertEqual(0, code)
                self.assertNotIn("::notice::review class P0", out)

    def test_t2_matcher_stays_exactly_the_o_velocity_categories(self):
        """PR #142 review, third round (MEDIUM): a round-2 fix widened the
        T2 matcher past O-velocity's path-escalation list (O-velocity.md
        §3.2, PLAN.md row 365, infrastructure.md I1-A: auth, session,
        payments, the credential table) by adding `nvault`, `migration`,
        `wrangler` and a bare `schema` root. `schema` alone forced P0 on
        ordinary JSON Schema config, SEO-schema composables and
        logging-schema files that have nothing to do with a D1 migration --
        infrastructure.md I1-A says migrations and wrangler bindings "are
        guarded by the deploy tool, not by rulesets", and nvault is a
        repo/tier classification, not a path inside an arbitrary caller
        repo. Table-driven: every one of these near-miss paths must now
        classify P1 (reviewed once, but not forced P0), the same as an
        ordinary code change."""
        near_misses = (
            # D1/ORM schema files: legitimate T2 concern, but "schema" is
            # not itself a T2 category -- migrations already covers the
            # literal directory a real D1 change lives under.
            "server/database/schema.ts",
            "drizzle/schema.ts",
            # Generic (non-D1) schema files a bare "schema" root swept in.
            "Config/machine-setup.schema.json",
            "narduk-seo/composables/useWebPageSchema.ts",
            "narduk-logging/schema/log-record.schema.json",
            # nvault: a repo/tier classification, not a path-escalation
            # category for an arbitrary caller repo's own diff.
            "apps/nvault/cmd/main.go",
            "Config/nvault-selectors.json",
            # Migrations and wrangler bindings: guarded by the deploy tool.
            "db/migrations/0007_add_index.sql",
            "wrangler.toml",
            "wrangler.jsonc",
        )
        for path in near_misses:
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 1, "deletions": 0}])
                code, out = run(transport, PR_TITLE="chore: routine wording")
                self.assertEqual(0, code)
                self.assertNotIn("::notice::review class P0", out)

    def test_t2_matcher_still_catches_every_named_category(self):
        """Companion to the near-miss table above: narrowing the roots must
        not have narrowed past the four O-velocity categories themselves.
        One representative path per category, plus the literal credential
        registry file the reboot's own Config/ carries."""
        for path in (
            "src/routes/auth/login.ts",           # auth
            "lib/session/store.ts",               # session
            "app/payments/checkout.vue",          # payments
            "Config/nvault-provider-credentials.json",  # the credential table
        ):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 1, "deletions": 0}])
                code, out = run(transport, PR_TITLE="chore: routine wording", PR_AUTHOR="dependabot[bot]")
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P0", out)

    def test_policy_prose_is_p1_not_p0_so_the_spend_limits_apply(self):
        """The narrowing itself. These were P0 -- the class no cap may skip --
        which made the class rule inert in exactly the two repositories with
        the worst measured multipliers (agent-infrastructure 75% P0 at 3.83
        launches per pull request; workflows 100% P0 at 4.00)."""
        for path in ("docs/agents/review-routing.md", "AGENTS.md", "skills/x/CLAUDE.md"):
            with self.subTest(path):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH}])
                code, out = run(transport, PR_TITLE="docs: routine wording")
                self.assertEqual(0, code)
                self.assertIn("::notice::review class P1", out)
                self.assertTrue(self.launched(transport))

    def test_an_ordinary_code_change_is_p1(self):
        transport = FakeTransport()
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("::notice::review class P1", out)
        self.assertIn("class P1", posted_review(transport)["body"])

    def test_only_a_re_request_label_wakes_the_reviewer(self):
        for added, launches in (("bot-inbox", False), ("hold merge", False), ("review-now", True), ("review-p0", True)):
            with self.subTest(added):
                transport = FakeTransport()
                labels = json.dumps([added]) if added.startswith("review-") else "[]"
                code, out = run(transport, PR_EVENT_ACTION="labeled", PR_EVENT_LABEL=added, PR_LABELS=labels)
                self.assertEqual(0, code)
                self.assertEqual(launches, self.launched(transport))
                if not launches:
                    self.assertIn("is not a review re-request", out)
                    self.assertEqual([], transport.calls, "a non-review label must cost no network call")

    def test_the_review_now_label_is_cleared_so_the_next_add_is_a_fresh_event(self):
        transport = FakeTransport()
        code, _ = run(transport, PR_LABELS='["review-now"]', PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-now")
        self.assertEqual(0, code)
        self.assertEqual(["review-now"], transport.labels_removed)

    def test_an_unreadable_file_list_reviews_rather_than_skips(self):
        class Broken(FakeTransport):
            def github(self, method, path, payload):
                if method == "GET" and path.endswith("/pulls/7/files") and not self.seen:
                    self.seen = True
                    return 500, {"message": "boom"}
                return super().github(method, path, payload)

        transport = Broken()
        transport.seen = False
        code, out = run(transport, PR_TITLE="chore: something")
        self.assertEqual(0, code)
        self.assertIn("classifying this pull request as reviewable", out)
        self.assertTrue(self.launched(transport))

    def test_the_review_body_tells_the_lane_how_to_re_request(self):
        transport = FakeTransport()
        run(transport)
        self.assertIn("A push no longer re-reviews on its own", posted_review(transport)["body"])
        self.assertIn("review-now", posted_review(transport)["body"])


class NoPushTests(unittest.TestCase):
    """narduk-reboot wave 4, P3-C2 fix round: a wave-2 lane merged
    narduk-libs#830, and the Cursor reviewer used the writable branch its
    launch gave it to push a test-loosening "autofix" commit (b73f49) onto
    narduk-libs#837 -- a SECURITY fix PR -- which shipped. The brief's own
    "do not push" HARD RULE existed since the callable's first commit and did
    not stop it, so the control has to be in the launch payload, not just
    the prompt: for a diff touching `.github/workflows/**`, `**/*.test.*`,
    `scripts/ci-runner-routing*`, or a T2-sensitive path, `workOnCurrentBranch`
    must be `False` -- no branch the agent can push onto lands on the
    reviewed pull request -- regardless of what the model does."""

    def launch_body(self, transport: FakeTransport) -> dict[str, Any]:
        launch = next(c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/v1/agents"))
        return launch["body"]

    def test_ordinary_diff_keeps_write_access(self):
        transport = FakeTransport(files=[{"filename": "src/widget.ts", "patch": PATCH, "additions": 40, "deletions": 5}])
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launch_body(transport)["workOnCurrentBranch"])
        self.assertNotIn("no-push mode", out)

    def test_no_push_paths_disable_write_access(self):
        for label, path in (
            ("a workflow file", ".github/workflows/ci.yml"),
            ("a test file", "src/widget.test.ts"),
            ("a nested test file", "server/routes/handler.test.py"),
            ("the ci-runner-routing script", "scripts/ci-runner-routing.py"),
            ("a ci-runner-routing variant", "scripts/ci-runner-routing-v2.py"),
            ("a T2-sensitive path", "src/routes/auth/login.ts"),
        ):
            with self.subTest(label):
                transport = FakeTransport(files=[{"filename": path, "patch": PATCH, "additions": 40, "deletions": 5}])
                code, out = run(transport)
                self.assertEqual(0, code)
                self.assertFalse(self.launch_body(transport)["workOnCurrentBranch"], f"{path} must disable push access")
                self.assertIn("::notice::no-push mode:", out)

    def test_unknown_diff_fails_closed_to_no_push(self):
        """The same UNKNOWN-never-means-safe direction as review_class's own
        P2 handling (test_an_unknown_file_list_defeats_every_p2_skip_signal):
        a diff this reviewer could not enumerate might touch a no-push path,
        so it is treated as if it did."""

        class Broken(FakeTransport):
            seen = False

            def github(self, method, path, payload):
                if method == "GET" and path.endswith("/pulls/7/files") and not self.seen:
                    self.seen = True
                    return 500, {"message": "boom"}
                return super().github(method, path, payload)

        transport = Broken()
        code, out = run(transport, PR_AUTHOR="dependabot[bot]")
        self.assertEqual(0, code)
        self.assertFalse(self.launch_body(transport)["workOnCurrentBranch"])
        self.assertIn("::notice::no-push mode: diff list unavailable", out)

    def test_no_push_brief_tells_the_agent_it_cannot_write(self):
        transport = FakeTransport(files=[{"filename": ".github/workflows/ci.yml", "patch": PATCH, "additions": 40, "deletions": 5}])
        run(transport)
        body = self.launch_body(transport)
        self.assertIn("NO-PUSH MODE", body["prompt"]["text"])
        self.assertIn("WITHOUT write access", body["prompt"]["text"])

    def test_ordinary_diff_brief_carries_no_no_push_notice(self):
        transport = FakeTransport(files=[{"filename": "src/widget.ts", "patch": PATCH, "additions": 40, "deletions": 5}])
        run(transport)
        body = self.launch_body(transport)
        self.assertNotIn("NO-PUSH MODE", body["prompt"]["text"])

    def test_is_no_push_sensitive_table(self):
        """Direct, table-driven unit coverage of the classifier itself, name
        by name -- independent of the end-to-end launch plumbing above."""
        sensitive = (
            ".github/workflows/ci.yml",
            ".github/workflows/nested/reusable.yml",
            ".github/actions/setup/action.yml",  # round-4 re-verify LOW: Actions definitions were a gap
            ".github/actions/nested/composite/action.yaml",
            "Component.test.tsx",
            "server/util.test.d.ts",
            "scripts/test_cursor_review.py",  # round-4 re-verify LOW: this repo's own test convention
            "scripts/test_red_main_listener.py",
            "e2e/login.spec.ts",  # round-4 re-verify LOW: Playwright
            "e2e/checkout.spec.js",
            "pkg/x_test.go",  # round-4 re-verify LOW: Go
            "internal/routing/ci_runner_routing_test.go",
            "Tests/FooTests.swift",  # round-4 re-verify LOW: Swift/XCTest
            "narduk-libs/Sources/App/Tests/SessionServiceTests.swift",
            "scripts/ci-runner-routing.py",
            "scripts/ci-runner-routing-v2/config.json",
            "src/routes/auth/login.ts",
            "app/payments/checkout.vue",
            "Config/nvault-provider-credentials.json",
        )
        for path in sensitive:
            with self.subTest(path):
                self.assertTrue(cr.is_no_push_sensitive(path), f"{path} should be no-push sensitive")
        not_sensitive = (
            "src/widget.ts",
            "docs/agents/review-routing.md",
            "scripts/ci-runner.py",  # not the ci-runner-routing script
            "server/database/schema.ts",  # narrowed off the T2 matcher this same round
            "scripts/testing_helpers.py",  # "testing_", not the `test_*.py` convention
            "src/specimen.ts",  # "spec" is a substring, not the `.spec.` convention
            "cmd/attest/main.go",  # "test" is a substring, not the `_test.go` convention
            "docs/GuestsTests.md",  # "Tests" suffix, but not a `.swift` file
        )
        for path in not_sensitive:
            with self.subTest(path):
                self.assertFalse(cr.is_no_push_sensitive(path), f"{path} should not be no-push sensitive")


class SpendLimitTests(unittest.TestCase):
    """The 2026-09-20 cost controls.

    Measured baseline: 199 review launches over 78 pull requests (2.55 each),
    139 in one Central day, 99 of them between 18:00 and 23:59 -- 13% of the
    month's pool in one evening. Every gate below is fail-open on purpose:
    these exist to stop a runaway evening, not to become a new way for a
    review to vanish quietly.
    """

    def launched(self, transport: FakeTransport) -> bool:
        return any(c["method"] == "POST" and c["url"].endswith("/v1/agents") for c in transport.calls)

    # --- size gate -------------------------------------------------------
    def test_a_tiny_non_p0_diff_is_not_worth_an_agent(self):
        transport = FakeTransport(files=[{"filename": "src/a.ts", "patch": PATCH, "additions": 3, "deletions": 0}])
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("3 changed line(s)", out)
        self.assertFalse(self.launched(transport))

    def test_a_tiny_p0_diff_is_still_reviewed(self):
        """A three-line `.github/workflows/` change is the deleted-`on:`-block
        hazard. Small is not safe there, so P0 is exempt from the size gate."""
        transport = FakeTransport(files=[{"filename": ".github/workflows/ci.yml", "patch": PATCH, "additions": 1, "deletions": 2}])
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_an_unsized_diff_fails_open(self):
        """No row carried additions/deletions: that is UNKNOWN, not empty. A
        zero here would read as a 0-line diff and skip every non-P0 review."""
        transport = FakeTransport(files=[{"filename": "src/a.ts", "patch": PATCH}])
        code, _ = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    # --- round cap -------------------------------------------------------
    def test_the_round_cap_stops_a_fifth_review(self):
        """No `request_changes` verdict in the measured sample arrived after
        round 4; rounds 5-13 cost 251 compute minutes for nothing."""
        transport = FakeTransport(marker_comment=marker_comment(4))
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("already had 4 review(s)", out)
        self.assertFalse(self.launched(transport))

    def test_the_fourth_review_still_runs(self):
        transport = FakeTransport(marker_comment=marker_comment(3, head=BASE))
        code, _ = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_an_explicit_re_request_on_the_same_head_runs(self):
        transport = FakeTransport(marker_comment=marker_comment(1, head=HEAD))
        code, _ = run(transport, PR_LABELS='["review-now"]', PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-now")
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))


    def test_the_round_count_survives_every_later_marker_rewrite(self):
        """The completion rewrite must carry the count forward. Writing the
        marker without it resets the ledger to 0 and the cap never binds."""
        transport = FakeTransport(marker_comment=marker_comment(2, head=BASE))
        code, _ = run(transport)
        self.assertEqual(0, code)
        bodies = [c["body"]["body"] for c in transport.calls if c["method"] in {"POST", "PATCH"} and "/issues/" in c["url"] and "comments" in c["url"]]
        self.assertTrue(bodies)
        for body in bodies:
            self.assertEqual(3, cr.marker_rounds(cr.parse_agent_marker(body)), body[:120])

    # --- delta gate ------------------------------------------------------
    def test_a_re_request_that_barely_moved_is_skipped(self):
        transport = FakeTransport(marker_comment=marker_comment(1, head=BASE))
        transport.compare_files = [{"filename": "src/a.ts", "additions": 1, "deletions": 0}]
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("since the reviewed head", out)
        self.assertFalse(self.launched(transport))

    def test_a_re_request_with_real_movement_is_reviewed(self):
        transport = FakeTransport(marker_comment=marker_comment(1, head=BASE))
        transport.compare_files = [{"filename": "src/a.ts", "additions": 80, "deletions": 20}]
        code, _ = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_an_unreadable_comparison_fails_open(self):
        transport = FakeTransport(marker_comment=marker_comment(1, head=BASE))
        transport.compare_files = None
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("the delta gate is not applied", out)
        self.assertTrue(self.launched(transport))

    # --- daily cap -------------------------------------------------------
    def test_the_daily_cap_stops_a_runaway_evening(self):
        transport = FakeTransport(agent_ledger=ledger(12))
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("launched 12 review(s) in the last 24h", out)
        self.assertFalse(self.launched(transport))

    def test_under_the_daily_cap_still_reviews(self):
        transport = FakeTransport(agent_ledger=ledger(11))
        code, _ = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_the_daily_cap_counts_only_this_repository(self):
        """It is a PER-REPOSITORY allowance, settable per repository: some
        need more oversight than others."""
        transport = FakeTransport(agent_ledger=ledger(30, repo="narduk-enterprises/riverstatus"))
        code, _ = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_agents_older_than_the_window_do_not_count(self):
        transport = FakeTransport(agent_ledger=ledger(30, age_hours=48))
        code, _ = run(transport)
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_the_ledger_reads_the_live_items_field(self):
        # The cap must bind against the shape the API actually returns, with
        # no `agents` key present anywhere in the payload.
        class LiveShape(FakeTransport):
            def cursor(self, method, path, payload):
                if method == "GET" and path.endswith("/v1/agents"):
                    return 200, {"items": self.agent_ledger, "nextCursor": None}
                return super().cursor(method, path, payload)

        transport = LiveShape(agent_ledger=ledger(12))
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("in the last 24h, the cap is 12", out)
        # The skip advises adding `review-deep`; that advice was a dead end
        # until the label reached the job `if:` (LabelAllowListTests).
        self.assertIn(cr.REVIEW_NOW_LABEL, out)
        self.assertFalse(self.launched(transport), "the cap binds on the live shape")

    def test_the_v0_agents_key_is_still_tolerated(self):
        class V0(FakeTransport):
            def cursor(self, method, path, payload):
                if method == "GET" and path.endswith("/v1/agents"):
                    return 200, {"agents": self.agent_ledger}
                return super().cursor(method, path, payload)

        transport = V0(agent_ledger=ledger(12))
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertFalse(self.launched(transport), "the alias still counts")

    def test_an_unrecognised_ledger_shape_fails_open(self):
        class Alien(FakeTransport):
            def cursor(self, method, path, payload):
                if method == "GET" and path.endswith("/v1/agents"):
                    return 200, {"data": {"agents": self.agent_ledger}}
                return super().cursor(method, path, payload)

        transport = Alien(agent_ledger=ledger(99))
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("the daily cap is not applied", out)
        self.assertTrue(self.launched(transport), "an unknown shape never blocks a review")

    def test_a_cursor_ledger_outage_fails_open(self):
        class Blind(FakeTransport):
            def cursor(self, method, path, payload):
                if method == "GET" and path.endswith("/v1/agents"):
                    return 500, {"message": "boom"}
                return super().cursor(method, path, payload)

        transport = Blind()
        code, out = run(transport)
        self.assertEqual(0, code)
        self.assertIn("the daily cap is not applied", out)
        self.assertTrue(self.launched(transport))

    def test_a_limit_set_to_zero_is_disabled(self):
        transport = FakeTransport(agent_ledger=ledger(99), marker_comment=marker_comment(99))
        code, _ = run(transport, DAILY_CAP="0", MAX_ROUNDS="0", MIN_LINES="0", MIN_DELTA_LINES="0")
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_a_junk_limit_uses_the_unlimited_default(self):
        """Invalid input follows the documented unlimited default."""
        transport = FakeTransport(agent_ledger=ledger(12))
        code, out = run(transport, DAILY_CAP="twelve")
        self.assertEqual(0, code)
        self.assertIn("is not a whole number", out)
        self.assertTrue(self.launched(transport))

    # --- review-deep -----------------------------------------------------
    def test_review_deep_bypasses_every_limit_at_once(self):
        transport = FakeTransport(
            agent_ledger=ledger(99),
            marker_comment=marker_comment(99),
            files=[{"filename": "src/a.ts", "patch": PATCH, "additions": 1, "deletions": 0}],
        )
        code, out = run(transport, PR_LABELS='["review-deep"]', PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-deep")
        self.assertEqual(0, code)
        self.assertIn("bypassing every spend limit", out)
        self.assertTrue(self.launched(transport))

    def test_review_now_bypasses_every_gate_without_upgrading_the_model(self):
        transport = FakeTransport(
            agent_ledger=ledger(99), marker_comment=marker_comment(99),
            files=[{"filename": "src/a.ts", "patch": PATCH, "additions": 1, "deletions": 0}],
        )
        code, out = run(transport, PR_LABELS='["review-now", "review-deep"]',
                      PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-now",
                      CURSOR_MODEL="", CURSOR_EFFORT="")
        self.assertEqual(0, code)
        self.assertIn("bypassing every spend limit", out)
        launch = next(c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/v1/agents"))
        self.assertEqual({"id": "composer-2.5", "params": [{"id": "fast", "value": "false"}]}, launch["body"]["model"])
        self.assertEqual(["review-now", "review-deep"], transport.labels_removed)
        self.assertFalse(any(c["method"] == "GET" and c["url"].endswith("/v1/agents") for c in transport.calls))

    def test_deep_request_is_consumed_and_uses_grok_even_on_a_tiny_prose_diff(self):
        transport = FakeTransport(files=[{"filename": "README.md", "additions": 1, "deletions": 0}])
        code, _ = run(transport, PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-deep")
        self.assertEqual(0, code)
        launch = next(c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/v1/agents"))
        self.assertEqual("grok-4.6", launch["body"]["model"]["id"])
        self.assertEqual(["review-deep"], transport.labels_removed)

    def test_reopening_with_a_stale_deep_label_does_not_upgrade_the_model(self):
        transport = FakeTransport()
        code, _ = run(transport, PR_EVENT_ACTION="reopened", PR_LABELS='["review-deep"]', CURSOR_MODEL="", CURSOR_EFFORT="")
        self.assertEqual(0, code)
        launch = next(c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/v1/agents"))
        self.assertEqual("composer-2.5", launch["body"]["model"]["id"])
        self.assertEqual(["review-deep"], transport.labels_removed)

    def test_automatic_reviews_have_no_default_daily_or_round_cap(self):
        transport = FakeTransport(agent_ledger=ledger(99), marker_comment=marker_comment(99))
        code, _ = run(transport, DAILY_CAP=str(cr.DEFAULT_DAILY_CAP), MAX_ROUNDS=str(cr.DEFAULT_MAX_ROUNDS), MIN_DELTA_LINES="0")
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))
        self.assertFalse(any(c["method"] == "GET" and c["url"].endswith("/v1/agents") for c in transport.calls))

    def test_a_gated_head_still_frees_the_review_now_label(self):
        """A cap that left the label on would strand the lane: adding a label
        that is already present raises no event."""
        transport = FakeTransport(marker_comment=marker_comment(9))
        code, _ = run(transport, PR_LABELS='["review-now"]', PR_EVENT_ACTION="labeled", PR_EVENT_LABEL="review-now")
        self.assertEqual(0, code)
        self.assertEqual([cr.REVIEW_NOW_LABEL], transport.labels_removed)

    def test_a_gated_head_cancels_the_previous_agent(self):
        """Job concurrency already killed this pull request's waiter, so an
        agent from the older head would keep burning the pool and post nothing."""
        transport = FakeTransport(marker_comment=marker_comment(9, head=BASE))
        code, _ = run(transport)
        self.assertEqual(0, code)
        self.assertEqual(["bc-old"], transport.cancelled)

    # --- model routing ---------------------------------------------------
    def test_the_default_review_sends_composer_and_no_effort_parameter(self):
        """composer-2.5 defines only `fast`; an `effort` it does not have
        would be rejected at launch."""
        model, params, effort, fast = cr.select_model({}, deep=False)
        self.assertEqual("composer-2.5", model)
        self.assertEqual("", effort)
        self.assertIs(fast, False)
        self.assertEqual([{"id": "fast", "value": "false"}], params)
        self.assertNotIn("effort", [p["id"] for p in params])

    def test_review_deep_sends_the_strong_model_at_xhigh(self):
        model, params, effort, _fast = cr.select_model({}, deep=True)
        self.assertEqual("grok-4.6", model)
        self.assertEqual("xhigh", effort)
        self.assertIn({"id": "effort", "value": "xhigh"}, params)

    def test_composer_is_never_launched_in_fast_mode_by_default(self):
        """Composer's OWN default variant is fast=true at 6x base
        ($3/$0.50/$15) -- more expensive than the grok-4.6 it replaces."""
        _m, params, _e, fast = cr.select_model({}, deep=False)
        self.assertIs(fast, False)
        self.assertIn({"id": "fast", "value": "false"}, params)

    def test_the_launch_payload_carries_the_selected_model(self):
        transport = FakeTransport()
        code, _ = run(transport, CURSOR_MODEL="", CURSOR_EFFORT="")
        self.assertEqual(0, code)
        launch = next(c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/v1/agents"))
        self.assertEqual("composer-2.5", launch["body"]["model"]["id"])
        self.assertEqual([{"id": "fast", "value": "false"}], launch["body"]["model"]["params"])

    # --- telemetry -------------------------------------------------------
    def test_a_completed_review_emits_one_telemetry_line(self):
        """Cursor exposes no token counts, so this line is the only record of
        what reviews cost and whether the cheaper model held up."""
        transport = FakeTransport()
        code, out = run(transport, CURSOR_MODEL="", CURSOR_EFFORT="")
        self.assertEqual(0, code)
        rows = [json.loads(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith("cursor-review-telemetry ")]
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("composer-2.5", row["model"])
        self.assertEqual("comment", row["verdict"])
        self.assertEqual(1, row["round"])
        self.assertIs(row["deep"], False)
        self.assertEqual(45, row["diff_lines"])


class LimitMessageLeverTests(unittest.TestCase):
    """A skip message must name a label that actually clears THAT limit.

    Every automatic skip now offers review-now, which runs with the ordinary
    model. No quota message may direct an agent to the expensive model.
    """

    def launched(self, transport: FakeTransport) -> bool:
        return any(c["method"] == "POST" and c["url"].endswith("/v1/agents") for c in transport.calls)

    # Each scenario trips exactly one limit from a clean state.
    def size_gate(self) -> FakeTransport:
        return FakeTransport(files=[{"filename": "src/a.ts", "patch": PATCH, "additions": 3, "deletions": 0}])

    def round_cap(self) -> FakeTransport:
        return FakeTransport(marker_comment=marker_comment(9))

    def delta_gate(self) -> FakeTransport:
        transport = FakeTransport(marker_comment=marker_comment(1, head=BASE))
        transport.compare_files = [{"filename": "src/a.ts", "additions": 1, "deletions": 0}]
        return transport

    def daily_cap(self) -> FakeTransport:
        return FakeTransport(agent_ledger=ledger(12))

    def scenarios(self) -> dict[str, Any]:
        return {"size gate": self.size_gate, "round cap": self.round_cap,
                "delta gate": self.delta_gate, "daily cap": self.daily_cap}

    def test_every_label_a_skip_message_names_actually_clears_that_limit(self):
        """The binding this class exists for.

        Reads each limit's real message, extracts every `review-*` label it
        offers, and proves each one launches a review in that same scenario. A
        message that advertises a lever the gate does not honour fails here --
        which is the shape of the shipped defect, in reverse.
        """
        for name, build in self.scenarios().items():
            with self.subTest(limit=name):
                transport = build()
                code, out = run(transport)
                self.assertEqual(0, code)
                self.assertFalse(self.launched(transport), f"{name} should have skipped")
                offered = set(re.findall(r"'(review-[a-z0-9-]+)'", out))
                self.assertTrue(offered, f"{name} names no lever at all")
                for label in sorted(offered):
                    with self.subTest(limit=name, label=label):
                        retry = build()
                        code, _ = run(retry, PR_LABELS=json.dumps([label]))
                        self.assertEqual(0, code)
                        self.assertTrue(
                            self.launched(retry),
                            f"the {name} message offers '{label}' but that label does not clear it")

    def test_every_skip_offers_only_the_standard_review_request(self):
        for name, build in self.scenarios().items():
            with self.subTest(limit=name):
                code, out = run(build())
                self.assertEqual(0, code)
                self.assertEqual({cr.REVIEW_NOW_LABEL}, set(re.findall(r"'(review-[a-z0-9-]+)'", out)))

    def test_review_p0_clears_the_size_gate(self):
        transport = self.size_gate()
        code, _ = run(transport, PR_LABELS=json.dumps([cr.P0_LABEL]))
        self.assertEqual(0, code)
        self.assertTrue(self.launched(transport))

    def test_review_p0_clears_nothing_but_the_size_gate(self):
        """The asymmetry that produced the defect, stated as behaviour.

        `priority != "P0"` guards the size gate alone; the round cap, delta
        gate and daily cap have no class exemption. So `review-p0` must NOT be
        advertised as a general escape hatch. An explicit `review-now` event
        bypasses every gate without changing the model.
        """
        for name, build in (("round cap", self.round_cap),
                            ("delta gate", self.delta_gate),
                            ("daily cap", self.daily_cap)):
            with self.subTest(limit=name):
                transport = build()
                code, _ = run(transport, PR_LABELS=json.dumps([cr.P0_LABEL]))
                self.assertEqual(0, code)
                self.assertFalse(
                    self.launched(transport),
                    f"'{cr.P0_LABEL}' must not clear the {name}")


if __name__ == "__main__":
    unittest.main()
