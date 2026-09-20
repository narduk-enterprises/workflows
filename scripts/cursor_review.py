#!/usr/bin/env python3
"""Review one pull request with a Cursor Cloud agent and post a real GitHub review.

Runs inside the `cursor-review.yml` callable. Stdlib only; every network call
goes through one injectable `transport` so the whole flow is testable offline.
Idempotent reads (GET) survive a transient gateway failure: `send` retries a
502/503/504 or a dropped connection up to four attempts with exponential
backoff. No write is ever retried.

Flow (agent-infrastructure#1564):

  1. Skip, exit 0, with a `::notice::`, when this event is not a same-repo,
     non-draft pull request, when the PR carries the `no-ai-review` label, or
     when the caller was never given `CURSOR_CLOUD_AGENTS_API_KEY`.
  1b. Skip when a `labeled` event added a label that is not a review
     re-request, and skip when the priority class is P2. The class rule is
     Logan's answer of 2026-09-19 to the reviewer-untangle round, verbatim:
     "No numeric cap, only the P0/P1/P2 class rule". P0 (workflows, the
     operating manual, routed agent docs, or an explicit `review-p0` label)
     always launches; P1 launches once per open/ready/re-request; P2
     (automation authors, release branches, chore/docs-marked titles,
     metadata-only diffs, or an explicit `review-p2` label) never launches an
     agent at all. `review-now` overrides every P2 signal.
  2. Cancel the agent run a previous head started, if it is still running.
  3. Launch ONE Cursor Cloud agent with the PR repository attached at the head
     branch plus read-only context repositories, and the review brief.
  4. Poll until the run reaches a terminal status or the wait budget ends.
  5. Read the run's `result` (the agent's final answer), parse the single
     fenced JSON block the brief contracts for, and post a formal pull-request
     review on the reviewed head: APPROVE / COMMENT / REQUEST_CHANGES with one
     inline comment per finding whose file:line is inside the PR diff.
  6. A non-blocking review dismisses this bot's own stale REQUEST_CHANGES.
     A head that moved while the agent ran posts nothing and says so: without
     `synchronize` the new head is reviewed when a lane adds `review-now`, not
     automatically.

Anything the reviewer reads (the PR, the diff, the agent's answer) is
untrusted content. Only the contracted JSON shape is trusted, and only its
vocabulary decides the review event.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

CURSOR_API = "https://api.cursor.com"
BOT_LOGIN = "github-actions[bot]"
OPT_OUT_LABEL = "no-ai-review"
# Priority classes (Logan, 2026-09-19: "No numeric cap, only the P0/P1/P2
# class rule"). A label is the explicit form; everything else is inferred.
REVIEW_NOW_LABEL = "review-now"
CLASS_LABELS = {"review-p0": "P0", "review-p1": "P1", "review-p2": "P2"}
# Which labels, when ADDED, mean "review the current head now". Any other
# label addition (`bot-inbox`, `hold merge`, a triage label) must not launch
# an agent, because the caller now listens for every `labeled` event.
RE_REQUEST_LABELS = frozenset({REVIEW_NOW_LABEL, "review-p0", "review-p1"})
# Heads no human is waiting on: the bot opened them and a lane merges them.
AUTOMATION_AUTHORS = frozenset({"dependabot[bot]", "github-actions[bot]", "renovate[bot]"})
AUTOMATION_HEAD_REFS = ("changeset-release/",)
# Always worth a review whatever the title says: a workflow change can delete
# an `on:` block silently, and the operating manual is estate policy.
ALWAYS_REVIEW_PREFIXES = (".github/workflows/", ".github/actions/", "docs/agents/")
ALWAYS_REVIEW_NAMES = ("AGENTS.md", "CLAUDE.md")
# Everything here is prose: a diff made only of these launches no agent.
METADATA_SUFFIXES = (".md", ".mdx", ".txt", ".rst")
METADATA_NAMES = ("LICENSE", "NOTICE", "CODEOWNERS", ".gitignore", ".gitattributes")
CHANGED_FILE_PAGES = 3
TERMINAL_RUN_STATUSES = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})
ACTIVE_AGENT_STATUSES = frozenset({"CREATING", "RUNNING", "ACTIVE", "PENDING"})
AGENT_MARKER = "cursor-review-agent"
REVIEW_MARKER = "cursor-review"
VERDICTS = {"approve": "APPROVE", "comment": "COMMENT", "request_changes": "REQUEST_CHANGES"}
SEVERITIES = ("blocking", "consider", "nit")
MAX_INLINE_COMMENTS = 40
MAX_COMMENT_CHARS = 6000
MAX_BODY_CHARS = 60000
POLL_SECONDS = 30
RETRY_STATUSES = frozenset({502, 503, 504})
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2.0
FENCE = re.compile(r"```json[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Transport = Callable[[str, str, dict[str, str], Optional[bytes], int], tuple[int, Any]]


class ReviewError(RuntimeError):
    """A failure that must make the job red: the review did not happen."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Skip(Exception):
    """A deliberate no-op: the job stays green and says why."""


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        status = exc.code
        exc.close()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise ReviewError(f"{method} {url.split('?')[0]} transport failed: {type(exc).__name__}") from exc
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return status, None
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def send(
    transport: Transport,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, Any]:
    """One request, with bounded retries for the idempotent half of the API.

    A GET is a read: a single transient 502/503/504 or a dropped connection is
    the gateway's problem, not the pull request's, and retrying it changes
    nothing on the server. Everything else goes exactly once — a retried POST
    could double-post a review or launch a second Cursor agent, and a retried
    PUT could dismiss a review twice.

    After the last attempt the caller sees exactly what it would have seen
    without retries: the final status (so `call` raises its own ReviewError
    carrying that status), or the transport's own ReviewError.
    """
    if method.upper() != "GET":
        return transport(method, url, headers, body, timeout)
    attempt = 1
    delay = RETRY_BACKOFF_SECONDS
    while True:
        final = attempt >= RETRY_ATTEMPTS
        try:
            status, value = transport(method, url, headers, body, timeout)
            if final or status not in RETRY_STATUSES:
                return status, value
            reason = f"HTTP {status}"
        except ReviewError:
            if final:
                raise
            reason = "transport failure"
        print(f"::notice::{method} {url.split('?')[0]} hit {reason}; retry {attempt + 1}/{RETRY_ATTEMPTS} in {delay:g}s")
        sleep(delay)
        delay *= 2
        attempt += 1


def error_detail(value: Any) -> str:
    """Up to 300 characters of a response BODY (never a header) for an error.

    Cursor nests its refusal as `{"error": {"code": ..., "message": ...}}`, so
    the GitHub-shaped `value["message"]` lookup rendered a real answer as the
    literal `None`: on 2026-09-19 a billing quota (`usage_limit_exceeded`) read
    only as `Cursor POST /v1/agents returned HTTP 400: None`, and the job log
    said nothing about why.
    """
    if value is None:
        return "<empty response body>"
    if isinstance(value, str):
        return value[:300]
    try:
        return json.dumps(value, separators=(",", ":"))[:300]
    except (TypeError, ValueError):
        return str(value)[:300]


class GitHub:
    def __init__(self, token: str, repository: str, transport: Transport, api: str = "https://api.github.com", *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.token = token
        self.repository = repository
        self.transport = transport
        self.api = api.rstrip("/")
        self.sleep = sleep

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None, *, ok: tuple[int, ...] = (200, 201)) -> Any:
        url = path if path.startswith("http") else f"{self.api}/repos/{self.repository}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "narduk-cursor-review",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        status, value = send(self.transport, method, url, headers, body, 60, sleep=self.sleep)
        if status not in ok:
            detail = value.get("message") if isinstance(value, dict) else str(value)
            raise ReviewError(f"GitHub {method} {path} returned HTTP {status}: {str(detail)[:300]}", status=status)
        return value

    def pages(self, path: str, *, limit: int = 5) -> list[Any]:
        rows: list[Any] = []
        for page in range(1, limit + 1):
            joiner = "&" if "?" in path else "?"
            value = self.call("GET", f"{path}{joiner}per_page=100&page={page}")
            if not isinstance(value, list):
                raise ReviewError(f"GitHub GET {path} returned a non-list page")
            rows.extend(value)
            if len(value) < 100:
                return rows
        raise ReviewError(f"GitHub GET {path} exceeded the {limit}-page bound")


class Cursor:
    def __init__(self, api_key: str, transport: Transport, api: str = CURSOR_API, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.api_key = api_key
        self.transport = transport
        self.api = api.rstrip("/")
        self.sleep = sleep

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None, *, ok: tuple[int, ...] = (200, 201)) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "narduk-cursor-review", "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        status, value = send(self.transport, method, f"{self.api}{path}", headers, body, 60, sleep=self.sleep)
        if status not in ok:
            raise ReviewError(f"Cursor {method} {path} returned HTTP {status}: {error_detail(value)}", status=status)
        return value


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def notice(message: str) -> None:
    print(f"::notice::{message}")


def parse_result(text: str) -> dict[str, Any]:
    """The LAST fenced ```json block in the agent's final answer, validated."""
    blocks = FENCE.findall(text or "")
    if not blocks:
        raise ReviewError("the agent's final answer carries no fenced json review block")
    try:
        value = json.loads(blocks[-1])
    except ValueError as exc:
        raise ReviewError(f"the review block is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ReviewError("the review block is not a JSON object")
    verdict = str(value.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        raise ReviewError(f"verdict {verdict!r} is not one of {sorted(VERDICTS)}")
    summary = str(value.get("summary") or "").strip()
    if not summary:
        raise ReviewError("the review block has no summary")
    findings: list[dict[str, Any]] = []
    raw_findings = value.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        raise ReviewError("findings must be a list")
    for index, row in enumerate(raw_findings):
        if not isinstance(row, dict):
            raise ReviewError(f"finding {index} is not an object")
        severity = str(row.get("severity", "")).strip().lower()
        if severity not in SEVERITIES:
            raise ReviewError(f"finding {index} severity {severity!r} is not one of {SEVERITIES}")
        path = str(row.get("path") or "").strip()
        # Only a literal "./" prefix goes; lstrip("./") would eat the dot of
        # ".github/..." and no finding there could ever anchor inline.
        while path.startswith("./"):
            path = path[2:]
        path = path.lstrip("/")
        line = row.get("line")
        line = int(line) if isinstance(line, (int, str)) and str(line).strip().isdigit() else None
        title = str(row.get("title") or "").strip()
        body = str(row.get("body") or "").strip()
        if not title and not body:
            raise ReviewError(f"finding {index} has neither title nor body")
        suggestion = row.get("suggestion")
        findings.append(
            {
                "severity": severity,
                "path": path,
                "line": line,
                "title": title or body.splitlines()[0][:120],
                "body": body,
                "suggestion": str(suggestion).rstrip() if isinstance(suggestion, str) and suggestion.strip() else None,
            }
        )
    checks = value.get("checks_run")
    checks_run = [str(item)[:300] for item in checks] if isinstance(checks, list) else []
    return {"verdict": verdict, "summary": summary[:2000], "findings": findings, "checks_run": checks_run[:20]}


def new_side_lines(patch: str | None) -> set[int]:
    """Every new-side line a hunk of this patch covers (context lines included)."""
    lines: set[int] = set()
    for match in HUNK.finditer(patch or ""):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        lines.update(range(start, start + count))
    return lines


def diff_index(files: list[dict[str, Any]]) -> dict[str, set[int]]:
    return {str(row.get("filename")): new_side_lines(row.get("patch")) for row in files if row.get("filename")}


def review_event(result: dict[str, Any], *, approve_on_clean: bool, self_authored: bool) -> str:
    blocking = any(f["severity"] == "blocking" for f in result["findings"])
    event = "REQUEST_CHANGES" if blocking else VERDICTS[result["verdict"]]
    if event == "APPROVE" and not approve_on_clean:
        event = "COMMENT"
    if self_authored and event != "COMMENT":
        # GitHub refuses APPROVE and REQUEST_CHANGES from the PR's own author.
        event = "COMMENT"
    return event


def inline_comment(finding: dict[str, Any]) -> str:
    parts = [f"**{finding['severity']}** — {finding['title']}"]
    if finding["body"] and finding["body"] != finding["title"]:
        parts.append(finding["body"])
    if finding["suggestion"]:
        parts.append(f"Proposed change:\n\n```\n{finding['suggestion']}\n```")
    return "\n\n".join(parts)[:MAX_COMMENT_CHARS]


def place_findings(findings: list[dict[str, Any]], index: dict[str, set[int]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Findings that can be inline comments, and those that stay in the body."""
    placed: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    for finding in findings:
        lines = index.get(finding["path"], set())
        if finding["line"] in lines and len(placed) < MAX_INLINE_COMMENTS:
            placed.append({"path": finding["path"], "line": finding["line"], "side": "RIGHT", "body": inline_comment(finding)})
        else:
            unplaced.append(finding)
    return placed, unplaced


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "duration unknown"
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def review_body(result: dict[str, Any], event: str, context: dict[str, Any], unplaced: list[dict[str, Any]], placed_count: int) -> str:
    counts = {severity: sum(1 for f in result["findings"] if f["severity"] == severity) for severity in SEVERITIES}
    lines = [
        f"**Cursor review** · `{context['model']}` (effort {context['effort']}, fast {str(context['fast']).lower()}) · "
        f"class {context.get('priority', 'P1')} · agent [{context['agent_id']}]({context['agent_url']}) · "
        f"{duration_text(context.get('elapsed'))} · head `{context['head_sha'][:12]}`",
        "",
        result["summary"],
        "",
        f"Findings: {counts['blocking']} blocking, {counts['consider']} consider, {counts['nit']} nit ({placed_count} posted inline).",
    ]
    if unplaced:
        lines += ["", "Findings the diff could not anchor (file or line outside this PR's changes):", ""]
        for finding in unplaced:
            where = f"`{finding['path']}:{finding['line']}`" if finding["path"] else "(no location)"
            lines.append(f"- **{finding['severity']}** {where} — {finding['title']}" + (f"\n  {finding['body']}" if finding["body"] and finding["body"] != finding["title"] else ""))
    if result["checks_run"]:
        lines += ["", "Checks the reviewer ran:", ""] + [f"- {item}" for item in result["checks_run"]]
    lines += [
        "",
        "How to respond: fix and push, or reply in the thread with "
        "`disposition: <accept|reject|defer> - <reason>` and resolve it. A push no longer re-reviews on its own — "
        f"add the `{REVIEW_NOW_LABEL}` label when the new head needs another review. A blocking finding requests "
        "changes; the merge gate honours GitHub's review decision.",
        "",
        f"<!-- {REVIEW_MARKER} " + json.dumps({"agentId": context["agent_id"], "headSha": context["head_sha"], "verdict": result["verdict"], "event": event}, sort_keys=True) + " -->",
    ]
    return "\n".join(lines)[:MAX_BODY_CHARS]


def render_brief(template: str, context: dict[str, Any]) -> str:
    text = template
    for key, value in context.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def agent_marker(agent_id: str, head_sha: str) -> str:
    return f"<!-- {AGENT_MARKER} " + json.dumps({"agentId": agent_id, "headSha": head_sha}, sort_keys=True) + " -->"


def parse_agent_marker(body: str) -> dict[str, Any] | None:
    match = re.match(rf"^<!-- {AGENT_MARKER} (\{{.*?\}}) -->", body or "")
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("agentId"), str) else None


def env_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------


def label_names(env: dict[str, str]) -> set[str]:
    """Case-folded label names from `toJSON(...labels.*.name)`.

    The workflow hands this over as a JSON array of strings. A comma or
    newline separated string is accepted too so the script stays runnable by
    hand, and an unparseable value is treated as "no labels" rather than as an
    error: a label list is an optimisation, never the thing that decides
    whether the pull request is safe.
    """
    raw = (env.get("PR_LABELS") or "").strip()
    if not raw:
        return set()
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw.replace("\n", ",").split(",")
    if not isinstance(value, list):
        return set()
    names = set()
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.add(name.strip().casefold())
    return names


def is_always_review(path: str) -> bool:
    return path.startswith(ALWAYS_REVIEW_PREFIXES) or path.rsplit("/", 1)[-1] in ALWAYS_REVIEW_NAMES


def is_metadata(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.lower().endswith(METADATA_SUFFIXES) or name in METADATA_NAMES


def changed_paths(github: GitHub, number: str) -> list[str] | None:
    """Every changed file, or None when that cannot be read cheaply.

    None means UNKNOWN, and unknown always reviews: a transport failure, a
    GitHub error, or a diff past the page bound must never be the reason a
    pull request silently loses its reviewer.
    """
    try:
        rows = github.pages(f"pulls/{number}/files", limit=CHANGED_FILE_PAGES)
    except ReviewError as exc:
        notice(f"could not read the changed files ({exc}); classifying this pull request as reviewable")
        return None
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # A rename carries its old path in `previous_filename`. Without it,
        # moving `.github/workflows/ci.yml` to `docs/old-ci.md` reads as a
        # metadata-only diff -- the deleted-`on:`-block mismatch again, this
        # time wearing the new name.
        for key in ("filename", "previous_filename"):
            value = row.get(key)
            if value:
                paths.append(str(value))
    return paths or None


def review_class(env: dict[str, str], paths: list[str] | None) -> tuple[str, str]:
    """The pull request's priority class: P0, P1, or a `Skip` for P2.

    Logan, 2026-09-19, choosing this over a numeric cap: "No numeric cap, only
    the P0/P1/P2 class rule". So nothing here counts launches; the class alone
    decides whether an agent starts.

      P0  merge-blocking work: `.github/workflows/**`, `.github/actions/**`,
          `docs/agents/**`, `AGENTS.md`/`CLAUDE.md`, or the `review-p0` label.
          Never deferred.
      P1  ordinary code work. One review at open / reopen / ready, and another
          only when a lane asks by adding `review-now`.
      P2  nits: an automation author, a release branch, a chore/docs/nit
          title, a metadata-only diff, or the `review-p2` label. P2 never
          launches an agent; the orchestrating session self-reviews it.

    `review-now` is the override: it defeats every inferred P2 signal and the
    explicit `review-p2` label, because a lane adding it has asked for this
    exact head to be reviewed.
    """
    labels = label_names(env)
    requested = REVIEW_NOW_LABEL in labels
    critical = [path for path in (paths or []) if is_always_review(path)]

    if "review-p2" in labels and not requested:
        raise Skip(f"class P2 (label 'review-p2'); add '{REVIEW_NOW_LABEL}' to review this head anyway")
    if "review-p0" in labels:
        return "P0", "label 'review-p0'"
    if not requested:
        author = (env.get("PR_AUTHOR") or "").strip()
        if author.casefold() in AUTOMATION_AUTHORS:
            raise Skip(f"class P2 (author {author} is an automation account); add '{REVIEW_NOW_LABEL}' to review it anyway")
        head_ref = env.get("PR_HEAD_REF") or ""
        if head_ref.startswith(AUTOMATION_HEAD_REFS):
            raise Skip(f"class P2 (head {head_ref} is a release branch); add '{REVIEW_NOW_LABEL}' to review it anyway")
    if critical:
        return "P0", f"touches {critical[0]}"
    if "review-p1" in labels:
        return "P1", "label 'review-p1'"
    # The diff, never the title. A conventional `chore:`/`docs:` prefix on an
    # ordinary code change would be an author-controlled skip of the
    # merge-gating reviewer, and estate lanes use those prefixes on code pull
    # requests every day; `review-p2` is the honest way to say "nit". Unknown
    # paths never skip either: a "chore:" title on a workflow change is exactly
    # the mismatch that hides a deleted `on:` block.
    if not requested and paths is not None:
        if all(is_metadata(path) for path in paths):
            raise Skip(f"class P2 (all {len(paths)} changed files are metadata); add '{REVIEW_NOW_LABEL}' to review it anyway")
    return "P1", f"label '{REVIEW_NOW_LABEL}' re-request" if requested else "code change"


def clear_review_now(github: GitHub, number: str, env: dict[str, str]) -> None:
    """Remove `review-now` so the next re-request is a fresh `labeled` event.

    Adding a label that is already present raises no event, so leaving it on
    would make the second re-request silently do nothing.
    """
    if REVIEW_NOW_LABEL not in label_names(env):
        return
    try:
        github.call("DELETE", f"issues/{number}/labels/{REVIEW_NOW_LABEL}", ok=(200, 204, 404))
        notice(f"cleared the '{REVIEW_NOW_LABEL}' label; add it again to re-request a review")
    except ReviewError as exc:
        notice(f"could not clear the '{REVIEW_NOW_LABEL}' label: {exc}")


def preconditions(env: dict[str, str]) -> None:
    number = env.get("PR_NUMBER", "").strip()
    if not number.isdigit():
        raise Skip("this event carries no pull request")
    if env_bool(env.get("PR_DRAFT")):
        raise Skip(f"PR #{number} is a draft; mark it ready for review to get a review")
    if env_bool(env.get("PR_OPTED_OUT")):
        raise Skip(f"opted out (label '{OPT_OUT_LABEL}' on PR #{number})")
    if (env.get("PR_EVENT_ACTION") or "").strip() == "labeled":
        added = (env.get("PR_EVENT_LABEL") or "").strip()
        if added.casefold() not in RE_REQUEST_LABELS:
            raise Skip(f"label '{added or 'unknown'}' is not a review re-request; add '{REVIEW_NOW_LABEL}' to review the current head")
    if env.get("PR_HEAD_REPOSITORY", "") != env.get("GITHUB_REPOSITORY", ""):
        raise Skip(f"PR #{number} head is a fork ({env.get('PR_HEAD_REPOSITORY') or 'unknown'}); same-repo heads only")
    if not env.get("CURSOR_CLOUD_AGENTS_API_KEY", "").strip():
        raise Skip("CURSOR_CLOUD_AGENTS_API_KEY is not available to this caller")
    for key in ("PR_HEAD_SHA", "PR_BASE_SHA"):
        if not COMMIT_SHA.fullmatch(env.get(key, "")):
            raise ReviewError(f"{key} is not a full commit sha")
    if not REPOSITORY.fullmatch(env.get("GITHUB_REPOSITORY", "")):
        raise ReviewError("GITHUB_REPOSITORY is malformed")
    if not env.get("GITHUB_TOKEN", "").strip():
        raise ReviewError("GITHUB_TOKEN is unavailable")


def context_repos(env: dict[str, str], repository: str) -> list[str]:
    repos: list[str] = []
    for item in (env.get("CONTEXT_REPOS") or "").replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if not REPOSITORY.fullmatch(item):
            raise ReviewError(f"context repository {item!r} is malformed")
        if item.casefold() != repository.casefold() and item not in repos:
            repos.append(item)
    return repos


def find_marker_comment(github: GitHub, number: str) -> dict[str, Any] | None:
    for comment in github.pages(f"issues/{number}/comments"):
        if (comment.get("user") or {}).get("login") == BOT_LOGIN and parse_agent_marker(comment.get("body") or ""):
            return comment
    return None


def cancel_previous(cursor: Cursor, marker: dict[str, Any] | None, head_sha: str) -> None:
    record = parse_agent_marker((marker or {}).get("body") or "")
    if not record or record.get("headSha") == head_sha:
        return
    try:
        agent = cursor.call("GET", f"/v1/agents/{record['agentId']}")
        if agent.get("status") in ACTIVE_AGENT_STATUSES and agent.get("latestRunId"):
            cursor.call("POST", f"/v1/agents/{record['agentId']}/runs/{agent['latestRunId']}/cancel", {}, ok=(200, 201, 202, 204))
            notice(f"cancelled the previous head's agent run ({record['agentId']})")
    except ReviewError as exc:
        notice(f"could not cancel the previous agent run: {exc}")


def upsert_marker(github: GitHub, number: str, marker: dict[str, Any] | None, body: str) -> dict[str, Any]:
    if marker:
        return github.call("PATCH", f"issues/comments/{marker['id']}", {"body": body})
    return github.call("POST", f"issues/{number}/comments", {"body": body})


def launch(cursor: Cursor, env: dict[str, str], brief: str, repository: str, repos: list[str]) -> dict[str, Any]:
    fast = env_bool(env.get("CURSOR_FAST", "false"))
    body = {
        "prompt": {"text": brief},
        "repos": [{"url": f"https://github.com/{repository}", "startingRef": env["PR_HEAD_REF"]}]
        + [{"url": f"https://github.com/{name}"} for name in repos],
        "model": {
            "id": env.get("CURSOR_MODEL") or "grok-4.6",
            "params": [{"id": "effort", "value": env.get("CURSOR_EFFORT") or "xhigh"}, {"id": "fast", "value": "true" if fast else "false"}],
        },
        "name": f"review {repository}#{env['PR_NUMBER']} @{env['PR_HEAD_SHA'][:7]}",
        "workOnCurrentBranch": True,
    }
    value = cursor.call("POST", "/v1/agents", body)
    # Verified live 2026-09-18: POST /v1/agents answers 201 with the agent
    # wrapped as {"agent": {...}}, while GET /v1/agents/{id} returns it bare.
    agent = value.get("agent") if isinstance(value, dict) and isinstance(value.get("agent"), dict) else value
    agent_id = agent.get("id") if isinstance(agent, dict) else None
    if not isinstance(agent_id, str) or not agent_id.startswith("bc-"):
        shape = json.dumps(value)[:300] if not isinstance(value, str) else value[:300]
        raise ReviewError(f"Cursor launch returned no agent id (response: {shape})")
    return agent


def wait_for_run(cursor: Cursor, agent_id: str, *, deadline: float, sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    last = None
    while True:
        agent = cursor.call("GET", f"/v1/agents/{agent_id}")
        run_id = agent.get("latestRunId")
        if run_id:
            run = cursor.call("GET", f"/v1/agents/{agent_id}/runs/{run_id}")
            status = run.get("status")
            if status != last:
                print(f"agent {agent_id} run {run_id}: {status}")
                last = status
            if status in TERMINAL_RUN_STATUSES:
                return run
        if clock() >= deadline:
            raise ReviewError(f"agent {agent_id} did not finish within the wait budget (last status {last!r})")
        sleep(POLL_SECONDS)


def is_own_review(review: dict[str, Any]) -> bool:
    """A review THIS workflow posted: the shared `github-actions[bot]` login is
    not enough, because every Actions job in the estate posts under it, and a
    blocking review from another workflow must never be dismissed here."""
    return (review.get("user") or {}).get("login") == BOT_LOGIN and f"<!-- {REVIEW_MARKER} " in (review.get("body") or "")


def dismiss_stale_request_changes(github: GitHub, number: str, head_sha: str) -> int:
    dismissed = 0
    for review in github.pages(f"pulls/{number}/reviews"):
        if is_own_review(review) and review.get("state") == "CHANGES_REQUESTED":
            github.call("PUT", f"pulls/{number}/reviews/{review['id']}/dismissals", {"message": f"Superseded by the Cursor review of {head_sha[:12]}.", "event": "DISMISS"}, ok=(200,))
            dismissed += 1
    return dismissed


def post_review(github: GitHub, env: dict[str, str], result: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], str]:
    number = env["PR_NUMBER"]
    pull = github.call("GET", f"pulls/{number}")
    if pull.get("head", {}).get("sha") != env["PR_HEAD_SHA"]:
        raise Skip(f"PR #{number} moved to {str(pull.get('head', {}).get('sha'))[:12]} while the review ran; a push no longer re-reviews, so add the '{REVIEW_NOW_LABEL}' label to review the new head")
    self_authored = (pull.get("user") or {}).get("login") == BOT_LOGIN
    event = review_event(result, approve_on_clean=env_bool(env.get("APPROVE_ON_CLEAN", "true")), self_authored=self_authored)
    index = diff_index(github.pages(f"pulls/{number}/files"))
    placed, unplaced = place_findings(result["findings"], index)
    payload = {
        "commit_id": env["PR_HEAD_SHA"],
        "event": event,
        "body": review_body(result, event, context, unplaced, len(placed)),
        "comments": placed,
    }
    try:
        review = github.call("POST", f"pulls/{number}/reviews", payload)
    except ReviewError as exc:
        if placed and exc.status == 422:
            # One rejected anchor sinks the whole review; fall back to body-only.
            notice(f"inline comments were refused ({exc}); posting the findings in the review body instead")
            payload["comments"] = []
            payload["body"] = review_body(result, event, context, result["findings"], 0)
            review = github.call("POST", f"pulls/{number}/reviews", payload)
        else:
            raise
    if event != "REQUEST_CHANGES":
        dismissed = dismiss_stale_request_changes(github, number, env["PR_HEAD_SHA"])
        if dismissed:
            notice(f"dismissed {dismissed} stale request-changes review(s) from this bot")
    return review, event


def step_summary(env: dict[str, str], lines: list[str]) -> None:
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def run(env: dict[str, str], transport: Transport, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic, brief_template: str | None = None) -> int:
    try:
        preconditions(env)
    except Skip as skip:
        notice(f"review skipped: {skip}")
        return 0
    repository = env["GITHUB_REPOSITORY"]
    number = env["PR_NUMBER"]
    github = GitHub(env["GITHUB_TOKEN"], repository, transport, env.get("GITHUB_API_URL") or "https://api.github.com", sleep=sleep)
    cursor = Cursor(env["CURSOR_CLOUD_AGENTS_API_KEY"], transport, env.get("CURSOR_API_URL") or CURSOR_API, sleep=sleep)
    try:
        priority, why = review_class(env, changed_paths(github, number))
    except Skip as skip:
        notice(f"review skipped: {skip}")
        return 0
    notice(f"review class {priority} ({why})")
    repos = context_repos(env, repository)
    if brief_template is None:
        with open(env["BRIEF_PATH"], encoding="utf-8") as handle:
            brief_template = handle.read()
    brief = render_brief(
        brief_template,
        {
            "repository": repository,
            "pull_request": number,
            "pr_title": env.get("PR_TITLE", ""),
            "pr_url": env.get("PR_URL", f"https://github.com/{repository}/pull/{number}"),
            "head_sha": env["PR_HEAD_SHA"],
            "head_ref": env["PR_HEAD_REF"],
            "base_sha": env["PR_BASE_SHA"],
            "base_ref": env["PR_BASE_REF"],
            "context_repos": ", ".join(repos) or "none",
        },
    )

    marker = find_marker_comment(github, number)
    cancel_previous(cursor, marker, env["PR_HEAD_SHA"])
    started = clock()
    agent = launch(cursor, env, brief, repository, repos)
    agent_id = agent["id"]
    agent_url = agent.get("url") or f"https://cursor.com/agents/{agent_id}"
    print(f"launched Cursor agent {agent_id} for {repository}#{number} @ {env['PR_HEAD_SHA'][:12]}")
    marker = upsert_marker(github, number, marker, f"{agent_marker(agent_id, env['PR_HEAD_SHA'])}\nCursor review of `{env['PR_HEAD_SHA'][:12]}` (class {priority}) in progress: agent [{agent_id}]({agent_url}).")
    clear_review_now(github, number, env)

    wait_minutes = float(env.get("WAIT_MINUTES") or 30)
    context = {
        "agent_id": agent_id,
        "agent_url": agent_url,
        "model": env.get("CURSOR_MODEL") or "grok-4.6",
        "effort": env.get("CURSOR_EFFORT") or "xhigh",
        "fast": env_bool(env.get("CURSOR_FAST", "false")),
        "head_sha": env["PR_HEAD_SHA"],
        "priority": priority,
    }
    try:
        finished = wait_for_run(cursor, agent_id, deadline=started + wait_minutes * 60, sleep=sleep, clock=clock)
        context["elapsed"] = clock() - started
        if finished.get("status") != "FINISHED":
            raise ReviewError(f"agent run ended {finished.get('status')}: {str(finished.get('result') or '')[:300]}")
        result = parse_result(str(finished.get("result") or ""))
        review, event = post_review(github, env, result, context)
    except Skip as skip:
        notice(f"review skipped: {skip}")
        upsert_marker(github, number, marker, f"{agent_marker(agent_id, env['PR_HEAD_SHA'])}\nCursor review of `{env['PR_HEAD_SHA'][:12]}` was superseded by a newer head (agent [{agent_id}]({agent_url})). A push no longer re-reviews: add the `{REVIEW_NOW_LABEL}` label to review the new head.")
        return 0
    except ReviewError as exc:
        upsert_marker(github, number, marker, f"{agent_marker(agent_id, env['PR_HEAD_SHA'])}\nCursor review of `{env['PR_HEAD_SHA'][:12]}` did not complete: {str(exc)[:500]} (agent [{agent_id}]({agent_url})). The job is red; re-run it or push a new head.")
        raise
    review_url = review.get("html_url") or ""
    upsert_marker(github, number, marker, f"{agent_marker(agent_id, env['PR_HEAD_SHA'])}\nCursor review of `{env['PR_HEAD_SHA'][:12]}`: {event} — {review_url} (agent [{agent_id}]({agent_url}), {duration_text(context.get('elapsed'))}).")
    counts = {severity: sum(1 for f in result["findings"] if f["severity"] == severity) for severity in SEVERITIES}
    step_summary(
        env,
        [
            "### Cursor review",
            "",
            f"- PR: {repository}#{number} @ `{env['PR_HEAD_SHA'][:12]}`",
            f"- Class: **{priority}** ({why})",
            f"- Agent: [{agent_id}]({agent_url}) on `{context['model']}` (effort {context['effort']}, fast {str(context['fast']).lower()}), {duration_text(context.get('elapsed'))}",
            f"- Review: **{event}** — {review_url}",
            f"- Findings: {counts['blocking']} blocking, {counts['consider']} consider, {counts['nit']} nit",
        ],
    )
    print(f"posted {event} review {review_url}")
    return 0


def main() -> int:
    env = dict(os.environ)
    try:
        return run(env, urllib_transport)
    except ReviewError as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
