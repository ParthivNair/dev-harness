"""The PR review-and-merge loop — the harness's "reviewer who closes the loop".

    select_pr -> ready_check -> review -> merge   (any step may early-exit, done)

It picks an open PR the harness authored (``harness/<instance>/issue-N``), gates on
**mergeable + green CI** *before* spending on a review, asks Claude for a structured
verdict over the diff, posts that review, and — **per-repo policy** — merges it to
``main``. The merge authority is the autonomy action ``merge_to_main``: ``forbidden``
in the instance default (so non-opted repos keep the draft-PR + human-merge ceiling),
raised to ``gated`` (review -> human gate -> merge) or ``autonomous`` (review -> merge)
in a repo's own ``harness.project.toml``.

Design contract (resolves the adversarial-review blockers):

* **Linear, no loop-back.** ``start_step="select_pr"`` and no step returns
  ``next_step == start_step``; every early exit just returns ``next_step=None`` (done).
  So ``loop_count`` stays 1 and a gated merge's answer correlates to a stable
  ``step_id`` ("merge#1").
* **Idempotent merge.** ``merge`` re-reads the PR first; an already-MERGED PR is treated
  as success. The runner's at-least-once contract (crash after merge, before save) can
  re-enter ``merge`` without double-merging.
* **No wasted spend / no duplicate reviews.** The mergeable + CI gate runs in
  ``ready_check`` *before* the paid Claude review; a PR with pending CI defers (retries
  next tick) without posting anything.
* **No livelock.** A PR the loop won't merge (changes requested / failing CI /
  unmergeable) is tagged ``harness:changes-requested`` and excluded from re-selection.
* **Fail-safe.** A malformed verdict is treated as NOT-approve; a gated merge defaults
  to reject on timeout. The harness never merges ``main`` without a real green signal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from harness.application import coordination as co
from harness.application.action_guard import (
    ActionGuard,
    ActionRequest,
    ForbiddenAction,
    GateRequired,
)
from harness.application.loop_runner import LoopDefinition, RunContext, StepOutcome
from harness.config.models import ProjectConfig
from harness.ports.executor import Executor
from harness.ports.github import ChecksState, GitHubAdapter, PRState, ReviewEvent

PR_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"enum": ["approve", "request_changes"]},
        "summary": {"type": "string"},
        "blocking": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "issue": {"type": "string"},
                },
                "required": ["area", "issue"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["recommendation", "summary", "blocking"],
    "additionalProperties": False,
}

MERGE_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["approved"],
    "additionalProperties": False,
}

DEFAULT_REVIEW_PROMPT = (
    "You are reviewing a pull request for merge into `main`. Judge it on correctness, "
    "scope (does it do what its linked issue asked and nothing risky beyond that), and "
    "test coverage. Return a structured verdict: `recommendation` is \"approve\" only if "
    "you would merge it as-is; otherwise \"request_changes\". List concrete `blocking` "
    "issues (empty if none). Be strict: when unsure, request changes."
)

_MAX_DIFF = 30_000  # cap the diff fed into the review prompt


def _read_prompt(project_root: Path, project: ProjectConfig) -> str:
    rel = project.prompts.pr_review
    if rel:
        path = Path(project_root) / rel
        if path.is_file():
            return path.read_text("utf-8")
    return DEFAULT_REVIEW_PROMPT


def _compose_prompt(base_prompt: str, pr_title: str, diff: str) -> str:
    body = diff or "(empty diff)"
    if len(body) > _MAX_DIFF:
        body = body[:_MAX_DIFF] + "\n[diff truncated]\n"
    return "\n".join([base_prompt, "", f"## Pull request: {pr_title}", "", "## Diff", body])


def _parse_verdict(result_text: str) -> dict:
    """Tolerant, FAIL-SAFE parse: anything malformed becomes 'request_changes' so a
    broken reviewer output can never auto-approve a merge."""
    fallback = {
        "recommendation": "request_changes",
        "summary": "reviewer returned no valid verdict",
        "blocking": [{"area": "review", "issue": "unparseable or missing verdict"}],
    }
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    rec = data.get("recommendation")
    if rec not in ("approve", "request_changes"):
        rec = "request_changes"
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    blocking = data.get("blocking") if isinstance(data.get("blocking"), list) else []
    return {"recommendation": rec, "summary": summary, "blocking": blocking}


def _format_review_body(summary: str, blocking: list, approved: bool) -> str:
    head = "Harness review: **approve**" if approved else "Harness review: **changes requested**"
    parts = [head, "", summary or "(no summary)"]
    if blocking:
        parts.append("")
        parts.append("Blocking:")
        for b in blocking:
            parts.append(f"- ({b.get('area', '?')}) {b.get('issue', '')}")
    parts.append("")
    parts.append("_Posted autonomously by the dev-harness pr_review loop._")
    return "\n".join(parts)


def build_pr_review_loop(
    *,
    executor: Executor,
    github: GitHubAdapter,
    guard: ActionGuard,
    project: ProjectConfig,
    instance_id: str,
    project_root: Path,
    artifacts_dir: Path,
) -> LoopDefinition:
    artifacts_dir = Path(artifacts_dir)
    base_prompt = _read_prompt(project_root, project)
    repo = project.repo

    def _issue_of(head: Optional[str]) -> Optional[int]:
        # Instance-scoped: only THIS instance's harness PRs match, so two machines
        # never race on the same PR and a human's PR is never auto-selected.
        return co.harness_branch_issue(head, instance_id)

    def select_pr(ctx: RunContext) -> StepOutcome:
        explicit = ctx.data.get("pr_number")
        if explicit is not None:
            # Manual override (`harness run pr_review --pr N`): target this PR directly.
            # Still subject to the full bar (CI, mergeable, review, the merge guard).
            pr = github.get_pull(repo=repo, number=int(explicit))
            return StepOutcome(
                next_step="ready_check",
                state_patch={"pr_number": pr.number, "issue_number": _issue_of(pr.head), "head": pr.head},
                output={"pr_number": pr.number, "explicit": True},
            )
        # Auto-select: lowest open harness/<instance>/issue-N PR not already flagged.
        candidates: list[tuple[int, Optional[int]]] = []
        for pr in github.list_pulls(repo=repo, state="open"):
            issue = _issue_of(pr.head)
            if issue is None:
                continue
            if co.CHANGES_REQUESTED in pr.labels:
                continue
            candidates.append((pr.number, issue))
        if not candidates:
            return StepOutcome(next_step=None, output={"no_pr": True})
        pr_number, issue_number = min(candidates)
        return StepOutcome(
            next_step="ready_check",
            state_patch={"pr_number": pr_number, "issue_number": issue_number},
            output={"pr_number": pr_number, "issue_number": issue_number},
        )

    def ready_check(ctx: RunContext) -> StepOutcome:
        pr_number = int(ctx.data["pr_number"])
        pr = github.get_pull(repo=repo, number=pr_number)
        if pr.state is not PRState.OPEN:
            return StepOutcome(next_step=None, output={"already_closed": pr.state.value})
        if pr.mergeable is None:
            # GitHub still computing mergeability — defer (no review, no cost, no label).
            return StepOutcome(next_step=None, output={"deferred": "mergeable pending"})
        checks = github.get_pull_checks(repo=repo, number=pr_number)
        if checks.state is ChecksState.PENDING:
            return StepOutcome(next_step=None, output={"deferred": "ci pending"})
        if checks.state is ChecksState.FAILURE or pr.mergeable is False:
            github.add_labels(repo=repo, number=pr_number, labels=[co.CHANGES_REQUESTED])
            return StepOutcome(
                next_step=None,
                output={"merged": False, "skip": "ci_failure_or_unmergeable",
                        "ci": checks.state.value, "mergeable": pr.mergeable},
            )
        return StepOutcome(
            next_step="review",
            state_patch={"pr_title": pr.title, "pr_draft": pr.draft, "ci": checks.state.value},
        )

    def review(ctx: RunContext) -> StepOutcome:
        pr_number = int(ctx.data["pr_number"])
        # Honour the review_pr tier: forbidden => the repo opts out of automated review
        # (and therefore of automated merge — we have no verdict). Gated review adds no
        # safety over the merge gate, so it is treated as allowed.
        try:
            guard.admit(ActionRequest("review_pr", project.id))
        except ForbiddenAction:
            return StepOutcome(next_step=None, output={"review_forbidden": True, "merged": False})
        except GateRequired:
            pass

        diff = github.get_pull_diff(repo=repo, number=pr_number)
        prompt = _compose_prompt(base_prompt, ctx.data.get("pr_title", ""), diff)
        result = executor.run_claude_task(project=project, prompt=prompt, json_schema=PR_REVIEW_SCHEMA)
        ctx.record_cost(result.total_cost_usd, result.input_tokens, result.output_tokens)

        verdict = _parse_verdict(result.result_text)
        summary, blocking = verdict["summary"], verdict["blocking"]
        approved = verdict["recommendation"] == "approve" and not blocking

        github.review_pull(
            repo=repo, number=pr_number,
            body=_format_review_body(summary, blocking, approved),
            event=ReviewEvent.APPROVE if approved else ReviewEvent.REQUEST_CHANGES,
        )

        # A perceptual artifact for the (gated) human and the dashboard.
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifacts_dir / f"{ctx.run_id}_review.txt"
        artifact.write_text(
            f"PR #{pr_number} on {repo}\nrecommendation: {verdict['recommendation']}\n\n"
            f"{summary}\n\nblocking: {json.dumps(blocking, indent=2)}\n",
            encoding="utf-8",
        )

        if not approved:
            github.add_labels(repo=repo, number=pr_number, labels=[co.CHANGES_REQUESTED])
            return StepOutcome(
                next_step=None,
                state_patch={"review_summary": summary, "verdict": verdict, "artifact_path": str(artifact)},
                output={"recommendation": verdict["recommendation"], "merged": False, "blocking": blocking},
            )
        return StepOutcome(
            next_step="merge",
            state_patch={"review_summary": summary, "verdict": verdict, "artifact_path": str(artifact)},
            output={"recommendation": "approve"},
        )

    def merge(ctx: RunContext) -> StepOutcome:
        pr_number = int(ctx.data["pr_number"])
        pr = github.get_pull(repo=repo, number=pr_number)
        if pr.state is PRState.MERGED:  # idempotent: re-entry after a crash mid-merge
            return StepOutcome(next_step=None, state_patch={"merged": True, "pr_merged_ref": pr.url},
                               output={"merged": True, "idempotent": True})

        # Authorize the merge: autonomous -> proceed; gated -> human gate; forbidden -> stop.
        answer = ctx.answer_for(ctx.step_id)
        if answer is None:
            try:
                guard.admit(ActionRequest("merge_to_main", project.id, {"repo": repo, "pr": pr_number}))
            except ForbiddenAction:
                return StepOutcome(next_step=None, output={"merge_forbidden": True, "merged": False})
            except GateRequired:
                ctx.require_verification(
                    prompt=(
                        f"PR #{pr_number} ({pr.title}) on {repo} passed review + CI and is "
                        f"mergeable.\n\nReview summary:\n{ctx.data.get('review_summary', '')}\n\n"
                        f"Approve to MERGE to main; reject to leave it open."
                    ),
                    answer_schema=MERGE_ANSWER_SCHEMA,
                    artifact_path=ctx.data.get("artifact_path"),
                    default_answer={"approved": False, "notes": "timed out: not merged"},
                    timeout_seconds=86_400,
                )
        elif not answer.approved:
            return StepOutcome(next_step=None, state_patch={"merge_rejected": True},
                               output={"merged": False, "notes": answer.answer.get("notes", "")})

        # Authorized. A harness PR is a draft; marking it ready is a merge prerequisite
        # subsumed by this authorization (no separate gate). Re-fetch to confirm.
        pr = github.get_pull(repo=repo, number=pr_number)
        if pr.draft:
            github.mark_pr_ready(repo=repo, number=pr_number)
            pr = github.get_pull(repo=repo, number=pr_number)
            if pr.draft:
                return StepOutcome(next_step=None, output={"merged": False, "error": "still draft after mark_ready"})

        # Re-verify the FULL bar fresh right before the irreversible merge — a gated approval
        # may arrive up to a day after ready_check, and mark_ready can reset mergeability.
        if github.get_pull_checks(repo=repo, number=pr_number).state is ChecksState.FAILURE:
            github.add_labels(repo=repo, number=pr_number, labels=[co.CHANGES_REQUESTED])
            return StepOutcome(next_step=None, output={"merged": False, "skip": "ci failed before merge"})
        if pr.mergeable is not True:  # False (conflicts) or None (still computing)
            if pr.mergeable is False:
                github.add_labels(repo=repo, number=pr_number, labels=[co.CHANGES_REQUESTED])
            return StepOutcome(next_step=None, output={"merged": False, "skip": "not mergeable before merge",
                                                       "mergeable": pr.mergeable})

        merged = github.merge_pull(repo=repo, number=pr_number, method=project.scheduling.pr_merge_method)

        # Best-effort: flip the linked issue to done (it stays owned by us, so it is not
        # re-claimable). Advisory — a labeling failure never unmerges the PR — but it is
        # RECORDED in the output (not silently swallowed) so a stuck issue is visible to
        # the dashboard / `harness show` and a human can fix the label.
        issue_number = ctx.data.get("issue_number")
        issue_done: Optional[object] = None
        if issue_number is not None:
            try:
                co.transition(github, repo=repo, number=int(issue_number), to_state=co.DONE)
                issue_done = True
            except Exception as exc:  # noqa: BLE001 — advisory; the merge already happened
                issue_done = f"failed: {exc!r}"
        return StepOutcome(
            next_step=None,
            state_patch={"merged": True, "pr_merged_ref": merged.url},
            output={"merged": True, "pr_url": merged.url, "issue": issue_number, "issue_done": issue_done},
        )

    return LoopDefinition(
        name="pr_review",
        start_step="select_pr",
        steps={
            "select_pr": select_pr,
            "ready_check": ready_check,
            "review": review,
            "merge": merge,
        },
    )
