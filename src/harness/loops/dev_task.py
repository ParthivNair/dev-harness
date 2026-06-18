"""The real dev/test loop — the harness's "junior dev".

    claim_issue -> generate -> build -> test -> verify_gate -> open_pr -> finish

It pulls a ``harness:queued`` issue (claimed via the lease in
:mod:`harness.application.coordination`), runs Claude on the project's
``dev_task`` prompt, builds and tests (looping back with the failure log as
context on any failure), gates the human to *perceive* the result, then opens a
**draft** PR and flips labels. The merge is structurally never ours.

Iteration accounting: ``start_step="claim_issue"``. Every failure / reject path
returns to ``claim_issue`` (the start step), so each loop-back ticks
``loop_count`` and is bounded by ``max_iterations``. ``claim_issue`` is
idempotent — on re-entry it re-asserts the lease and the ``in-progress`` state,
so a loop-back is cheap and the board stays accurate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from harness.application import coordination as co
from harness.application.action_guard import ActionGuard, ActionRequest, GateRequired
from harness.application.loop_runner import LoopDefinition, RunContext, StepOutcome
from harness.config.models import ProjectConfig
from harness.ports.executor import Executor
from harness.ports.github import GitHubAdapter

DEV_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["approved"],
    "additionalProperties": False,
}

DEFAULT_DEV_PROMPT = (
    "Implement the change described in the linked issue. Run the project's tests. "
    "Open a draft PR. Do not touch `main`; do not force-push."
)

_MAX_LOG = 4000  # cap failure logs fed back into the next prompt


def _read_prompt(project_root: Path, project: ProjectConfig) -> str:
    """The project's dev_task prompt from disk, or a sane built-in fallback (so the
    echo/fake path and tests work without a real prompt file)."""
    rel = project.prompts.dev_task
    if rel:
        path = Path(project_root) / rel
        if path.is_file():
            return path.read_text("utf-8")
    return DEFAULT_DEV_PROMPT


def _compose_prompt(base_prompt: str, issue_title: str, issue_body: str,
                    last_failure: Optional[dict]) -> str:
    parts = [base_prompt, "", f"## Task (issue): {issue_title}", issue_body or "(no description)"]
    if last_failure:
        phase = last_failure.get("phase", "?")
        parts += ["", f"## Previous attempt failed at: {phase}", "Fix it. Details:"]
        for key in ("stderr", "stdout", "notes"):
            val = last_failure.get(key)
            if val:
                parts.append(f"[{key}]\n{val}")
    return "\n".join(parts)


def build_dev_loop(
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

    def _repo_and_number(ctx: RunContext) -> tuple[str, int]:
        return ctx.data["repo"], int(ctx.data["issue_number"])

    def claim_issue(ctx: RunContext) -> StepOutcome:
        repo, number = _repo_and_number(ctx)
        if co.owns_issue(github, repo=repo, number=number, instance_id=instance_id):
            # already ours (scheduler pre-claimed, or a loop-back): re-assert in-progress
            co.transition(github, repo=repo, number=number, to_state=co.IN_PROGRESS)
            return StepOutcome(next_step="generate")
        result = co.claim(github, repo=repo, number=number, instance_id=instance_id)
        if not result.ok:
            # lost the lease / owned elsewhere -> finish as a clean no-op (no spend)
            return StepOutcome(next_step=None, output={"claim_lost": True, "reason": result.reason})
        return StepOutcome(next_step="generate", output={"claimed": number})

    def generate(ctx: RunContext) -> StepOutcome:
        repo, number = _repo_and_number(ctx)
        issue = github.get_issue(repo=repo, number=number)
        prompt = _compose_prompt(base_prompt, issue.title, issue.body, ctx.data.get("last_failure"))
        result = executor.run_claude_task(project=project, prompt=prompt)
        ctx.record_cost(result.total_cost_usd, result.input_tokens, result.output_tokens)
        return StepOutcome(
            next_step="build",
            state_patch={
                "branch": f"harness/{instance_id}/issue-{number}",
                "issue_title": issue.title,
                "session_id": result.session_id,
                "claude_result": result.result_text,
            },
            output={"session_id": result.session_id, "cost_usd": result.total_cost_usd},
        )

    def build(ctx: RunContext) -> StepOutcome:
        result = executor.run_build(project=project)
        if result.ok:
            return StepOutcome(
                next_step="test",
                state_patch={"build_stdout": result.stdout},
                output={"exit_code": result.exit_code},
            )
        return StepOutcome(
            next_step="claim_issue",  # loop back (new iteration) with the failure log
            state_patch={"last_failure": {
                "phase": "build", "exit_code": result.exit_code,
                "stderr": result.stderr[:_MAX_LOG], "stdout": result.stdout[:_MAX_LOG]}},
            output={"exit_code": result.exit_code, "failed": True},
        )

    def test(ctx: RunContext) -> StepOutcome:
        result = executor.run_test(project=project)
        if not result.ok:
            return StepOutcome(
                next_step="claim_issue",
                state_patch={"last_failure": {
                    "phase": "test", "exit_code": result.exit_code,
                    "stderr": result.stderr[:_MAX_LOG], "stdout": result.stdout[:_MAX_LOG]}},
                output={"exit_code": result.exit_code, "failed": True},
            )
        # Write a perceptual artifact: what Claude did + the test output, for the human.
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifacts_dir / f"{ctx.run_id}_iter{ctx.loop_count}.txt"
        artifact.write_text(
            f"branch: {ctx.data.get('branch')}\n\n"
            f"=== claude result ===\n{ctx.data.get('claude_result', '')}\n\n"
            f"=== test output ===\n{result.stdout}\n",
            encoding="utf-8",
        )
        return StepOutcome(
            next_step="verify_gate",
            state_patch={"test_stdout": result.stdout, "artifact_path": str(artifact)},
            output={"exit_code": result.exit_code},
        )

    def verify_gate(ctx: RunContext) -> StepOutcome:
        answer = ctx.answer_for(ctx.step_id)
        if answer is None:
            repo, number = _repo_and_number(ctx)
            co.transition(github, repo=repo, number=number, to_state=co.NEEDS_VERIFICATION)
            branch = ctx.data.get("branch")
            ctx.require_verification(
                prompt=(
                    f"Issue #{number}: {ctx.data.get('issue_title', '')}\n"
                    f"Branch `{branch}` built and tested green. Pull it, run the app, and "
                    f"confirm the change behaves correctly (perceive what tests cannot). "
                    f"Approve to open a draft PR; reject with notes to iterate."
                ),
                answer_schema=DEV_ANSWER_SCHEMA,
                artifact_path=ctx.data.get("artifact_path"),
                default_answer={"approved": False, "notes": "timed out: rejected by default"},
                timeout_seconds=86_400,
            )
        notes = answer.answer.get("notes", "")
        if answer.approved:
            return StepOutcome(next_step="publish", state_patch={"verify_notes": notes}, output=answer.answer)
        return StepOutcome(
            next_step="claim_issue",  # loop back; human notes become next-iteration context
            state_patch={"last_failure": {"phase": "verify", "notes": notes}},
            output=answer.answer,
        )

    def publish(ctx: RunContext) -> StepOutcome:
        # Commit the working-tree edits and push the feature branch so the PR can
        # reference it. The Executor refuses trunks / force-pushes; a human merges.
        _repo_and_number(ctx)
        number = int(ctx.data["issue_number"])
        branch = ctx.data.get("branch", f"harness/{instance_id}/issue-{number}")
        title = ctx.data.get("issue_title") or f"issue #{number}"
        result = executor.publish_branch(
            project=project, branch=branch, commit_message=f"harness: {title} (#{number})"
        )
        return StepOutcome(
            next_step="open_pr",
            state_patch={"publish_stdout": result.stdout},
            output={"branch": branch},
        )

    def open_pr(ctx: RunContext) -> StepOutcome:
        repo, number = _repo_and_number(ctx)
        # Consult the autonomy policy. open_draft_pr is autonomous by default; if a
        # project demoted it to gated, escalate to a human gate (re-entry pattern).
        try:
            guard.admit(ActionRequest("open_draft_pr", project.id))
        except GateRequired:
            answer = ctx.answer_for(ctx.step_id)
            if answer is None:
                ctx.require_verification(
                    prompt=f"Open the draft PR for issue #{number}?",
                    answer_schema=DEV_ANSWER_SCHEMA,
                    default_answer={"approved": False},
                    timeout_seconds=86_400,
                )
            if not answer.approved:
                return StepOutcome(next_step=None, output={"pr_declined": True})

        branch = ctx.data.get("branch", f"harness/{instance_id}/issue-{number}")
        title = ctx.data.get("issue_title") or f"Resolve issue #{number}"
        body = (
            f"Resolves #{number}.\n\n"
            f"{ctx.data.get('claude_result', '')}\n\n"
            f"---\nDraft opened autonomously by the harness (`{instance_id}`). "
            f"A human reviews and merges — the harness cannot."
        )
        pr = github.open_draft_pr(repo=repo, head=branch, base="main", title=title, body=body)
        co.transition(github, repo=repo, number=number, to_state=co.PR_OPEN)
        return StepOutcome(
            next_step="finish",
            state_patch={"pr_url": pr.url, "pr_number": pr.number},
            output={"pr_url": pr.url, "pr_number": pr.number},
        )

    def finish(ctx: RunContext) -> StepOutcome:
        return StepOutcome(
            next_step=None,
            state_patch={"completed": True},
            output={"pr_url": ctx.data.get("pr_url")},
        )

    return LoopDefinition(
        name="dev_task",
        start_step="claim_issue",
        steps={
            "claim_issue": claim_issue,
            "generate": generate,
            "build": build,
            "test": test,
            "verify_gate": verify_gate,
            "publish": publish,
            "open_pr": open_pr,
            "finish": finish,
        },
    )
