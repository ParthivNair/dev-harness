"""The research loop — the harness's "product researcher who writes the backlog".

    scan -> assess -> file_findings -> finish     (issues filed)
    scan -> assess -> finish                       (nothing worth filing)

Sibling of :mod:`harness.loops.arch_review`, but goals-driven rather than
rubric-driven. Where arch_review measures the code against a fixed architecture
rubric, research takes the OWNER'S GOALS/CONTEXT (the ``goals`` argument) and asks
Claude to research THIS repo and emit a prioritized backlog of small,
independently-shippable, TESTABLE issues. It files a ``harness:queued`` issue per
finding — which becomes the dev loop's work queue.

It never opens PRs or touches code: its blast radius is "creates issues", the
safest write tier. Findings are deduped by title against the open queue so a
repeated research pass never spams duplicates.
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
from harness.util.prompts import load_bundled_prompt

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"enum": ["low", "med", "high"]},
                    "body": {"type": "string"},
                },
                "required": ["title", "severity", "body"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

# Inline fallback used when the bundled ``research`` prompt is missing (so the
# echo/fake path and tests work without the packaged template). Mirrors the shape
# of prompts/research.md: research first, one focused change per issue, testable
# acceptance criteria, dedupe, no placeholders — with a {{goals}} placeholder.
DEFAULT_PROMPT = (
    "Research this repository and file a prioritized backlog of small, well-scoped "
    "issues — one focused change each — for an autonomous dev loop to claim and "
    "implement. Do not implement anything in this pass.\n\n"
    "## Goals & context\n\n{{goals}}\n\n"
    "Each issue must be a single, independently-shippable change with TESTABLE "
    "acceptance criteria a human can verify. For each, give a short imperative title, "
    "a severity (low|med|high), and a body with what/why + acceptance criteria + file "
    "pointers. Research first (README/docs, TODO/FIXME, thin spots, existing issues); "
    "dedupe against the current backlog; no placeholder issues. If nothing is worth "
    "filing, return an empty findings list."
)

# Used when ``goals`` is empty — a generic "find the most valuable improvements"
# brief so the loop still produces a sensible backlog with no owner context.
GENERIC_GOALS = (
    "(No explicit goals were provided.) Find the most valuable improvements for this "
    "repository: correctness, missing tests, rough edges, and obvious gaps."
)


def _build_prompt(goals: str) -> str:
    """The bundled ``research`` template with ``{{goals}}`` filled in, or the inline
    default if the template is unavailable. An empty ``goals`` falls back to a
    generic 'find the most valuable improvements' brief."""
    template = load_bundled_prompt("research") or DEFAULT_PROMPT
    return template.replace("{{goals}}", goals.strip() or GENERIC_GOALS)


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


def build_research_loop(
    *,
    executor: Executor,
    github: GitHubAdapter,
    guard: ActionGuard,
    project: ProjectConfig,
    project_root: Path,
    goals: str,
) -> LoopDefinition:
    prompt = _build_prompt(goals)
    repo = project.repo

    def scan(ctx: RunContext) -> StepOutcome:
        result = executor.run_claude_task(
            project=project, prompt=prompt, json_schema=RESEARCH_SCHEMA
        )
        ctx.record_cost(result.total_cost_usd, result.input_tokens, result.output_tokens)
        findings = _parse_findings(result.result_text)
        return StepOutcome(
            next_step="assess",
            state_patch={"findings": findings},
            output={"count": len(findings)},
        )

    def assess(ctx: RunContext) -> StepOutcome:
        findings = ctx.data.get("findings") or []
        if not findings:
            return StepOutcome(next_step="finish", output={"no_action": True})
        return StepOutcome(next_step="file_findings")

    def file_findings(ctx: RunContext) -> StepOutcome:
        findings = ctx.data.get("findings") or []
        # Provision the labels we apply, so research is self-sufficient and works even
        # if `labels-init` wasn't run first (idempotent, order-independent). The full
        # coordination set is still provisioned by `labels-init`; here we only need ours.
        github.ensure_labels(repo=repo, labels=[co.QUEUED, "sev:high", "sev:med", "sev:low"])
        # Dedupe by title against the open queue so repeated research doesn't pile up.
        existing = {i.title for i in github.list_issues(repo=repo, state="open", labels=[co.QUEUED])}
        filed: list[int] = []
        skipped: list[str] = []
        for f in findings:
            title = f.get("title", "untitled finding")
            if title in existing:
                skipped.append(title)
                continue
            guard.admit(ActionRequest("file_issue", project.id))  # autonomous tier
            sev = f.get("severity", "low")
            issue = github.create_issue(
                repo=repo,
                title=title,
                body=f.get("body", ""),
                labels=[co.QUEUED, f"sev:{sev}"],
            )
            filed.append(issue.number)
            existing.add(title)
        return StepOutcome(
            next_step="finish",
            state_patch={"filed": filed, "skipped": skipped},
            output={"filed": filed, "skipped": skipped},
        )

    def finish(ctx: RunContext) -> StepOutcome:
        return StepOutcome(next_step=None, output={"done": True})

    return LoopDefinition(
        name="research",
        start_step="scan",
        steps={"scan": scan, "assess": assess, "file_findings": file_findings, "finish": finish},
    )
