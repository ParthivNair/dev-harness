"""The triage loop — the harness's "backlog groomer".

    scan -> apply -> finish      (labels refined)
    scan -> finish               (nothing queued / no judgements)

A bounded, JSON-Schema-constrained LLM pass over the open ``harness:queued`` issues
(sibling of :mod:`harness.loops.arch_review`). It asks Claude to judge each queued
issue's severity + effort (and any declared dependencies), then applies that
judgement to the GitHub board as ``sev:*`` / ``effort:*`` labels — the very labels
:func:`harness.application.coordination.find_claimable` orders work by. Judgement
(the LLM) and ordering (the deterministic claimer) stay cleanly separated: triage
only *labels*; it never claims, opens PRs, or edits issue bodies.

Its blast radius is "refines labels + an optional one-line rationale comment", so
it runs at the ``set_labels`` autonomy tier like arch_review's ``file_issue``. It
skips any issue not currently ``harness:queued`` (already claimed / resolved), and
a malformed/echoing model result applies nothing (a clean no-op exit).
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.application import coordination as co
from harness.application.action_guard import ActionGuard, ActionRequest
from harness.application.loop_runner import LoopDefinition, RunContext, StepOutcome
from harness.config.models import ProjectConfig
from harness.ports.executor import Executor
from harness.ports.github import GitHubAdapter, IssueRef

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "severity": {"enum": ["low", "med", "high"]},
                    "effort": {"enum": ["s", "m", "l"]},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    "rationale": {"type": "string"},
                },
                "required": ["number", "severity", "effort"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}

DEFAULT_PROMPT = (
    "Triage the queued backlog below. For EACH issue, judge its severity "
    "(low|med|high) and the effort to resolve it (s|m|l = small|medium|large), and "
    "list any issues it depends on by number. Be decisive and balanced — prefer "
    "quick, high-value wins. Return one judgement per issue; do not invent issues."
)


def _read_prompt(project_root: Path, project: ProjectConfig) -> str:
    """The project's triage prompt from disk, or a sane built-in fallback (so the
    echo/fake path and tests work without a real prompt file)."""
    rel = project.prompts.triage
    if rel:
        path = Path(project_root) / rel
        if path.is_file():
            return path.read_text("utf-8")
    return DEFAULT_PROMPT


def _queued_block(issues: list[IssueRef]) -> str:
    """A compact, deterministic rendering of the queue for the model to judge."""
    lines = []
    for i in issues:
        lines.append(f"### #{i.number}: {i.title}")
        lines.append((i.body or "(no description)").strip())
        lines.append("")
    return "\n".join(lines)


def _parse_judgements(result_text: str) -> list[dict]:
    """Tolerant parse: a non-JSON / malformed result means 'no judgements' (clean
    exit), so the echo/fake path and a model that declines both end cleanly."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    judgements = data.get("judgements")
    return judgements if isinstance(judgements, list) else []


def _retag(labels: tuple[str, ...], prefix: str, value: str) -> list[str]:
    """Drop any existing ``<prefix>*`` label and set ``<prefix><value>`` — a
    set-or-refine so a re-triage moves an issue's sev/effort without piling up."""
    kept = [label for label in labels if not label.startswith(prefix)]
    kept.append(f"{prefix}{value}")
    return list(dict.fromkeys(kept))


def build_triage_loop(
    *,
    executor: Executor,
    github: GitHubAdapter,
    guard: ActionGuard,
    project: ProjectConfig,
    project_root: Path,
) -> LoopDefinition:
    base_prompt = _read_prompt(project_root, project)
    repo = project.repo

    def scan(ctx: RunContext) -> StepOutcome:
        issues = github.list_issues(repo=repo, state="open", labels=[co.QUEUED])
        if not issues:
            return StepOutcome(next_step="finish", output={"queued": 0})
        prompt = f"{base_prompt}\n\n{_queued_block(issues)}"
        result = executor.run_claude_task(
            project=project, prompt=prompt, json_schema=TRIAGE_SCHEMA
        )
        ctx.record_cost(result.total_cost_usd, result.input_tokens, result.output_tokens)
        judgements = _parse_judgements(result.result_text)
        if not judgements:
            return StepOutcome(next_step="finish", output={"queued": len(issues), "judged": 0})
        return StepOutcome(
            next_step="apply",
            state_patch={"judgements": judgements},
            output={"queued": len(issues), "judged": len(judgements)},
        )

    def apply(ctx: RunContext) -> StepOutcome:
        judgements = ctx.data.get("judgements") or []
        labelled: list[int] = []
        skipped: list[int] = []
        for j in judgements:
            number = j.get("number")
            if not isinstance(number, int):
                continue
            try:
                issue = github.get_issue(repo=repo, number=number)
            except Exception:  # noqa: BLE001 — a vanished/renumbered issue is just skipped
                skipped.append(number)
                continue
            # Only triage issues still on the queue; an issue claimed/resolved since
            # the scan is left untouched (its labels are the dev loop's now).
            if co.state_of(issue) != co.QUEUED:
                skipped.append(number)
                continue
            guard.admit(ActionRequest("set_labels", project.id))  # set_labels tier
            labels = _retag(issue.labels, co.SEV_PREFIX, j["severity"])
            labels = _retag(tuple(labels), co.EFFORT_PREFIX, j["effort"])
            github.set_labels(repo=repo, number=number, labels=labels)
            rationale = j.get("rationale")
            if rationale:
                github.comment_on_issue(
                    repo=repo, number=number, body=f"triage: {rationale}"
                )
            labelled.append(number)
        return StepOutcome(
            next_step="finish",
            state_patch={"labelled": labelled, "skipped": skipped},
            output={"labelled": labelled, "skipped": skipped},
        )

    def finish(ctx: RunContext) -> StepOutcome:
        return StepOutcome(next_step=None, output={"done": True})

    return LoopDefinition(
        name="triage",
        start_step="scan",
        steps={"scan": scan, "apply": apply, "finish": finish},
    )
