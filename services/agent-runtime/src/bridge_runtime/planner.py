"""Bounded planning. The model PROPOSES a plan through a forced tool call; deterministic
code validates it (schema, size, unique keys, known dependencies, acyclic) before any
task exists. Invalid proposals are retried a bounded number of times, then the run
blocks visibly."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .providers.base import ErrorKind, ProviderError, ToolSpec

MAX_TASKS_HARD = 12


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z0-9_-]{1,20}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=4000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=5)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_TASKS_HARD)


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[PlannedTask] = Field(min_length=1, max_length=MAX_TASKS_HARD)


PLAN_TOOL = ToolSpec(
    name="propose_plan",
    description="Propose a small dependency-aware plan. Each task needs testable acceptance criteria.",
    parameters=PlanProposal.model_json_schema(),
)


def validate_plan(args: dict, max_tasks: int) -> PlanProposal:
    try:
        plan = PlanProposal.model_validate(args)
    except ValidationError as e:
        raise ProviderError(ErrorKind.invalid_tool_arguments, f"plan schema: {e.error_count()} errors") from None
    if len(plan.tasks) > max_tasks:
        raise ProviderError(ErrorKind.invalid_tool_arguments, f"plan has {len(plan.tasks)} tasks > {max_tasks}")
    keys = [t.key for t in plan.tasks]
    if len(set(keys)) != len(keys):
        raise ProviderError(ErrorKind.invalid_tool_arguments, "duplicate task keys")
    known = set(keys)
    for t in plan.tasks:
        if not set(t.depends_on) <= known or t.key in t.depends_on:
            raise ProviderError(ErrorKind.invalid_tool_arguments, f"task {t.key} has unknown/self dependency")
    topo_order(plan)  # raises on cycles
    return plan


def topo_order(plan: PlanProposal) -> list[str]:
    deps = {t.key: set(t.depends_on) for t in plan.tasks}
    order, done = [], set()
    while deps:
        ready = sorted(k for k, d in deps.items() if d <= done)
        if not ready:
            raise ProviderError(ErrorKind.invalid_tool_arguments, "plan dependencies contain a cycle")
        for k in ready:
            order.append(k)
            done.add(k)
            del deps[k]
    return order


def planning_prompt(goal: str, max_tasks: int) -> str:
    return (
        "You are the orchestrator. Break the goal into at most the given number of tasks. "
        "Prefer fewer, independently verifiable tasks. Do not invent requirements.\n"
        f"<goal>{goal}</goal>\n<max_tasks>{max_tasks}</max_tasks>"
    )
