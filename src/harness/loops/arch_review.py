"""The architecture-review loop — the harness's "tech lead who writes tickets".

    scan -> assess -> file_findings -> finish     (findings)
    scan -> assess -> finish                       (no action needed)

Bounded and rubric-driven. It asks Claude for *structured* findings against the
project's ``arch_review`` rubric (a JSON-Schema-constrained call), then files a
``harness:queued`` issue per finding — which becomes the dev loop's work queue.
It never opens PRs or touches code: its blast radius is "creates issues", the
safest write tier. Findings are deduped by title against the open queue so a
repeated review never spams duplicates.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.application import coordination as co
from harness.application.action_guard import ActionGuard, ActionRequest
from harness.application.loop_runner import LoopDefinition, RunContext, StepOutcome
from harness.config.models import ProjectConfig
from harness.ports.executor import Executor
from harness.ports.github import GitHubAdapter

ARCH_FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"enum": ["low", "med", "high"]},
                    "rationale": {"type": "string"},
                },
                "required": ["title", "severity", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

# Severity ordering for the per-run cap: high files before med before low.
_SEVERITY_RANK = {"low": 1, "med": 2, "high": 3}

DEFAULT_RUBRIC = (
    "Review this codebase against its architecture rubric. Report concrete, "
    "actionable findings only (no praise, no speculation). For each, give a short "
    "title, a severity (low|med|high), and a one-paragraph rationale. If nothing "
    "needs action, return an empty findings list."
)


def _read_rubric(project_root: Path, project: ProjectConfig) -> str:
    rel = project.prompts.arch_review
    if rel:
        path = Path(project_root) / rel
        if path.is_file():
            return path.read_text("utf-8")
    return DEFAULT_RUBRIC


def _parse_findings(result_text: str) -> list[dict]:
    """Tolerant parse: a non-JSON / malformed result means 'no findings' (clean
    exit), so the echo/fake path and a model that declines both end cleanly."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    findings = data.get("findings")
    return findings if isinstance(findings, list) else []


def build_arch_review_loop(
    *,
    executor: Executor,
    github: GitHubAdapter,
    guard: ActionGuard,
    project: ProjectConfig,
    project_root: Path,
) -> LoopDefinition:
    rubric = _read_rubric(project_root, project)
    repo = project.repo
    max_per_run = project.scheduling.max_findings_per_run

    def scan(ctx: RunContext) -> StepOutcome:
        result = executor.run_claude_task(
            project=project, prompt=rubric, json_schema=ARCH_FINDINGS_SCHEMA
        )
        ctx.record_cost(result.total_cost_usd, result.input_tokens, result.output_tokens)
        findings = _parse_findings(result.result_text)
        return StepOutcome(
            next_step="assess",
            state_patch={"findings": findings, "raw_scan_result": result.result_text},
            output={"count": len(findings)},
        )

    def assess(ctx: RunContext) -> StepOutcome:
        findings = ctx.data.get("findings") or []
        if not findings:
            return StepOutcome(next_step="finish", output={"no_action": True})
        return StepOutcome(next_step="file_findings")

    def file_findings(ctx: RunContext) -> StepOutcome:
        findings = ctx.data.get("findings") or []
        # Dedupe by title against the open queue so repeated reviews don't pile up.
        existing = {i.title for i in github.list_issues(repo=repo, state="open", labels=[co.QUEUED])}
        skipped: list[str] = []
        new: list[dict] = []
        for f in findings:
            title = f.get("title", "untitled finding")
            if title in existing:
                skipped.append(title)  # "skipped" stays duplicate-title only
                continue
            new.append(f)
            existing.add(title)
        # Cap per run, highest-severity first. A stable sort keeps the model's order
        # within a severity, so it breaks ties. Findings past the cap are deferred
        # (eligible to file, cut by the cap) — a different bucket than dedup skips.
        if max_per_run is not None:
            new.sort(key=lambda f: _SEVERITY_RANK.get(f.get("severity", "low"), 0), reverse=True)
            to_file, deferred_findings = new[:max_per_run], new[max_per_run:]
        else:
            to_file, deferred_findings = new, []
        filed: list[int] = []
        for f in to_file:
            guard.admit(ActionRequest("file_issue", project.id))  # autonomous tier
            sev = f.get("severity", "low")
            issue = github.create_issue(
                repo=repo,
                title=f.get("title", "untitled finding"),
                body=f.get("rationale", ""),
                labels=[co.QUEUED, f"sev:{sev}"],
            )
            filed.append(issue.number)
        deferred = [f.get("title", "untitled finding") for f in deferred_findings]
        return StepOutcome(
            next_step="finish",
            state_patch={"filed": filed, "skipped": skipped, "deferred": deferred},
            output={"filed": filed, "skipped": skipped, "deferred": deferred},
        )

    def finish(ctx: RunContext) -> StepOutcome:
        return StepOutcome(next_step=None, output={"done": True})

    return LoopDefinition(
        name="arch_review",
        start_step="scan",
        steps={"scan": scan, "assess": assess, "file_findings": file_findings, "finish": finish},
    )
