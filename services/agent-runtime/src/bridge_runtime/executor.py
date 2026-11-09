"""Durable run executor: authenticated request -> run -> bounded plan -> tasks ->
specialist execution -> verified artifacts -> synthesis -> needs_review.

Durability: the runner holds a lease on the run; every step is checkpointed. Model
calls are recorded as operations keyed by (run, step), so a restarted runner replays
recorded results instead of calling (and paying) again. Artifacts use deterministic IDs.
The runtime acts AS the orchestrator agent through application services, so it gets
exactly the requester's permissions and nothing more.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from bridge_control.contracts import api as A
from bridge_control.contracts.artifacts import Provenance
from bridge_control.contracts.common import ActorKind, ActorRef, Visibility
from bridge_control.contracts.errors import DomainError, ErrorCode
from bridge_control.contracts.work import MessageRole, RunStatus, TaskState
from bridge_control.core import Principal
from bridge_control.db import tables as t
from bridge_control.reliability import operation
from bridge_control.services import Ctx, artifacts, budget, models, runs, work
from bridge_control.services.artifacts import BlobStore

from .planner import PLAN_TOOL, planning_prompt, topo_order, validate_plan, PlanProposal
from .providers.base import (
    CancelToken, ChatMessage, ErrorKind, GenerateRequest, GenerateResponse, ProviderAdapter, ProviderError, ToolSpec,
)
from .providers.fake import FakeProvider
from .providers.registry import AdapterRegistry

MAX_PROVIDER_RETRIES = 2
MAX_VERIFY_ROUNDS = 2
NS = uuid.UUID("6f1d3c52-2b8e-4c8e-9a51-0d6c3f7a9e11")

WORK_TOOL = ToolSpec("submit_work", "Return the deliverable for this task.", {
    "type": "object", "additionalProperties": False, "required": ["content", "summary"],
    "properties": {"content": {"type": "string", "maxLength": 200_000}, "summary": {"type": "string", "maxLength": 200},
                   "media_type": {"type": "string", "enum": ["text/markdown", "text/plain", "application/json", "text/csv"]}}})
VERIFY_TOOL = ToolSpec("verify_work", "Judge whether the deliverable meets every criterion.", {
    "type": "object", "additionalProperties": False, "required": ["passed", "notes"],
    "properties": {"passed": {"type": "boolean"}, "notes": {"type": "string", "maxLength": 1000}}})


class Blocked(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class ModelChoice:
    adapter: ProviderAdapter
    model: str
    endpoint_id: UUID | None
    is_fake: bool


@dataclass
class RunExecutor:
    ctx: Ctx
    registry: AdapterRegistry
    store: BlobStore
    runner_id: str = field(default_factory=lambda: f"runner-{uuid.uuid4().hex[:8]}")
    sleep: Callable[[float], None] = time.sleep

    # --------------------------------------------------------------- driver

    def run_pending(self) -> list[UUID]:
        with self.ctx.db.tx() as conn:
            ids = runs.runnable(conn, self.ctx.now())
        for rid in ids:
            self.execute(rid)
        return ids

    def execute(self, run_id: UUID) -> None:
        with self.ctx.db.tx() as conn:
            row = runs.claim_for_runner(conn, self.ctx.now(), run_id, self.runner_id)
        if row is None:
            return
        p = Principal(kind="agent", user_id=row.requested_by_user_id, agent_id=row.orchestrator_agent_id)
        try:
            choice = self._choose_model(row, p)
            plan = self._plan(row, choice)
            keys = self._create_tasks(row, p, plan)
            for key in topo_order(plan):
                self._execute_task(row, p, choice, plan, key, keys[key])
            self._synthesize(row, p, plan, keys, choice)
        except Cancelled:
            self._end(run_id, "cancelled")
        except Blocked as e:
            self._block(run_id, str(e))
            self._end(run_id, "failed_permanent", "blocked")
        except ProviderError as e:
            self._block(run_id, f"provider {e.kind.value}: {e.detail}"[:200])
            self._end(run_id, "failed_permanent", e.kind.value)
        except DomainError as e:
            if e.code is ErrorCode.stale_attempt:  # another runner took over: stop quietly
                self._end(run_id, "fenced", "stale_attempt")
                return
            if e.code is ErrorCode.budget_exhausted:
                self._block(run_id, "budget exhausted")
                self._end(run_id, "failed_permanent", "budget_exhausted")
                return
            if self._row(run_id).cancel_requested:
                self._end(run_id, "cancelled")
                return
            raise

    # ------------------------------------------------------------- helpers

    def _row(self, run_id: UUID):  # noqa: ANN202
        with self.ctx.db.tx() as conn:
            return conn.execute(sa.select(t.runs).where(t.runs.c.id == run_id)).one()

    def _check_cancel(self, run_id: UUID) -> None:
        if self._row(run_id).cancel_requested:
            raise Cancelled()

    def _save(self, run_id: UUID, **state: Any) -> dict:
        with self.ctx.db.tx() as conn:
            return runs.checkpoint(conn, self.ctx.now(), run_id, self.runner_id, state).checkpoint

    def _progress(self, run_id: UUID, summary: str, task_id: UUID | None = None, endpoint_id: UUID | None = None) -> None:
        with self.ctx.db.tx() as conn:
            runs.progress(conn, self.ctx.now(), runs._row(conn, run_id), summary, task_id=task_id,
                          model_endpoint_id=endpoint_id)

    def _block(self, run_id: UUID, reason: str) -> None:
        with self.ctx.db.tx() as conn:
            row = runs._row(conn, run_id)
            if row.status == "working":
                runs.set_status(conn, self.ctx.now(), run_id, RunStatus.blocked, f"Blocked: {reason}"[:200],
                                blocked_reason=reason[:200])

    def _end(self, run_id: UUID, outcome: str, code: str | None = None) -> None:
        with self.ctx.db.tx() as conn:
            runs.end_attempt(conn, self.ctx.now(), run_id, self.runner_id, outcome, code)

    # -------------------------------------------------------- model choice

    def _choose_model(self, row, p: Principal) -> ModelChoice:  # noqa: ANN001
        eid = row.checkpoint.get("model_endpoint_id")
        with self.ctx.db.tx() as conn:
            if eid is None:
                agent = conn.execute(sa.select(t.agent_profiles).where(t.agent_profiles.c.id == row.orchestrator_agent_id)).one()
                eid = agent.lead_model_endpoint_id
            if eid is None:
                if self.ctx.settings.is_dev:
                    return ModelChoice(self.registry.build("fake", fake_key="dev"), "bridge-fake-lead", None, True)
                raise Blocked("no lead model configured for this agent")
            ep = models.endpoint_contract_for(conn, p, UUID(str(eid)))
            if not ep.orchestrator_qualified:
                raise Blocked("lead model has not passed tool-calling and JSON-schema probes")
            if ep.execution_location.value == "device":
                raise Blocked("device models are text-only over the worker bridge and cannot lead a run")
            c = models.connection_row(conn, ep.connection_id)
            if c is None or c.disabled_at is not None:
                raise Blocked("provider connection unavailable")
            if c.billing_mode == "personal" and c.owner_user_id != row.requested_by_user_id:
                raise Blocked("run would spend another member's credentials; use a payer grant")
        adapter = self.registry.build(ep.runtime.value, base_url=c.base_url, secret_ref=c.secret_ref)
        return ModelChoice(adapter, ep.model_id, ep.id, isinstance(adapter, FakeProvider))

    # ------------------------------------------------------------ model call

    def _call(self, row, choice: ModelChoice, step: str, req: GenerateRequest, tool: str) -> dict:  # noqa: ANN001
        """Budget-checked, retried, cancellable, recorded. Returns the tool arguments."""
        key = f"run:{row.id}:call:{step}"

        def do() -> dict:
            last: ProviderError | None = None
            for attempt in range(MAX_PROVIDER_RETRIES + 1):
                self._check_cancel(row.id)
                prompt_chars = sum(len(m.content) for m in req.messages)
                est = self.registry.estimate(choice.adapter, choice.model, prompt_chars, req.max_tokens)
                with self.ctx.db.tx() as conn:
                    if budget.remaining(conn, row.budget_reservation_id) < est:
                        raise DomainError(ErrorCode.budget_exhausted, "run budget would be exceeded")
                cancel = CancelToken()
                try:
                    resp = choice.adapter.generate(req, cancel)
                except ProviderError as e:
                    last = e
                    if not e.retryable or attempt == MAX_PROVIDER_RETRIES:
                        raise
                    self._progress(row.id, f"Provider {e.kind.value}; retrying ({attempt + 1}/{MAX_PROVIDER_RETRIES})")
                    self.sleep(min(30.0, e.retry_after if e.retry_after is not None else 2.0 ** attempt))
                    continue
                # One usage record per REAL provider call: a call repeated after a crash is
                # charged again (it was paid again), never silently deduplicated.
                self._charge(row, choice, resp, f"{key}:a{attempt}:{uuid.uuid4().hex[:12]}")
                calls = [c for c in resp.tool_calls if c.name == tool]
                if not calls:
                    last = ProviderError(ErrorKind.invalid_tool_arguments, f"model did not call {tool}")
                    if attempt == MAX_PROVIDER_RETRIES:
                        raise last
                    continue
                return {"args": calls[0].arguments, "provider_model_id": resp.provider_model_id}
            raise last or ProviderError(ErrorKind.unavailable, "exhausted")

        return operation(self.ctx.db.tx, self.ctx.now, key, "model_call", do)

    def _charge(self, row, choice: ModelChoice, resp: GenerateResponse, op_key: str) -> None:  # noqa: ANN001
        cost, known = self.registry.cost(choice.adapter, choice.model, resp.usage)
        with self.ctx.db.tx() as conn:
            ok = budget.consume(conn, self.ctx.now(), row.budget_reservation_id, cost, kind="model_tokens",
                                operation_key=op_key, model_endpoint_id=choice.endpoint_id,
                                provider_model_id=resp.provider_model_id, input_tokens=resp.usage.input_tokens,
                                output_tokens=resp.usage.output_tokens, provider_reported=known and not choice.is_fake)
        if not ok:
            raise DomainError(ErrorCode.budget_exhausted, "actual usage exceeded the run reservation")

    # ------------------------------------------------------------------ plan

    def _plan(self, row, choice: ModelChoice) -> PlanProposal:  # noqa: ANN001
        cp = self._row(row.id).checkpoint
        if "plan" in cp:
            return PlanProposal.model_validate(cp["plan"])
        max_tasks = int(cp.get("max_tasks", 5))
        req = GenerateRequest(model=choice.model, messages=[
            ChatMessage("system", "You plan bounded work for a team workspace."),
            ChatMessage("user", planning_prompt(row.goal, max_tasks))], tools=[PLAN_TOOL],
            tool_choice="propose_plan", max_tokens=2048)
        for attempt in range(MAX_PROVIDER_RETRIES + 1):
            out = self._call(row, choice, f"plan:{attempt}", req, "propose_plan")
            try:
                plan = validate_plan(out["args"], max_tasks)
                break
            except ProviderError as e:
                if attempt == MAX_PROVIDER_RETRIES:
                    raise Blocked(f"planner produced an invalid plan: {e.detail}") from None
        self._save(row.id, plan=plan.model_dump(), planner_model=out["provider_model_id"])
        self._progress(row.id, f"Planned {len(plan.tasks)} tasks", endpoint_id=choice.endpoint_id)
        return plan

    def _create_tasks(self, row, p: Principal, plan: PlanProposal) -> dict[str, UUID]:  # noqa: ANN001
        cp = self._row(row.id).checkpoint
        if "task_ids" in cp:
            return {k: UUID(v) for k, v in cp["task_ids"].items()}
        conv = self._conv(row)
        private = conv.scope == "private" and not cp.get("publish_tasks", False)
        vis = Visibility.private if private else Visibility.project
        per_task = max(1, row.budget_limit_micros // max(1, len(plan.tasks)))
        ids: dict[str, UUID] = {}
        with self.ctx.db.tx() as conn:  # tasks + checkpoint commit atomically
            for key in topo_order(plan):
                pt = next(x for x in plan.tasks if x.key == key)
                task = work.create_task(conn, self.ctx.now(), p, row.project_id, A.CreateTask(
                    title=pt.title, description=pt.description, acceptance_criteria=pt.acceptance_criteria,
                    dependencies=[ids[d] for d in pt.depends_on],
                    budget_limit={"amount_micros": per_task, "currency": row.currency}), run_id=row.id, visibility=vis)
                ids[key] = task.id
            runs.checkpoint(conn, self.ctx.now(), row.id, self.runner_id, {"task_ids": {k: str(v) for k, v in ids.items()}})
        self._progress(row.id, f"Created {len(ids)} {'private' if private else 'project'} tasks")
        return ids

    def _conv(self, row):  # noqa: ANN001, ANN202
        with self.ctx.db.tx() as conn:
            return conn.execute(sa.select(t.conversations).where(t.conversations.c.id == row.conversation_id)).one()

    # ------------------------------------------------------------- execute

    def _task(self, p: Principal, task_id: UUID):  # noqa: ANN202
        with self.ctx.db.tx() as conn:
            return work.task_contract(conn, work.readable_task(conn, p, task_id))

    def _execute_task(self, row, p: Principal, choice: ModelChoice, plan: PlanProposal, key: str, task_id: UUID) -> None:  # noqa: ANN001
        done = self._row(row.id).checkpoint.get("done", {})
        if key in done:
            return
        self._check_cancel(row.id)
        task = self._task(p, task_id)
        if task.state in (TaskState.review, TaskState.done):  # crashed after review, before checkpoint
            self._save(row.id, done={**done, key: [str(a) for a in task.artifact_refs]})
            return
        if task.state in (TaskState.leased, TaskState.running, TaskState.verifying):
            with self.ctx.db.tx() as conn:  # fence out the dead runner's attempt
                work.system_transition(conn, self.ctx.now(), task_id, TaskState.ready)
        with self.ctx.db.tx() as conn:
            work.promote_ready(conn, self.ctx.now(), row.project_id, run_id=row.id)
        task = self._task(p, task_id)
        if task.state is not TaskState.ready:
            raise Blocked(f"task {key} is {task.state.value}, not ready")
        pt = next(x for x in plan.tasks if x.key == key)
        for round_ in range(MAX_VERIFY_ROUNDS + 1):
            with self.ctx.db.tx() as conn:
                claim = work.claim_task(conn, self.ctx.now(), p, task_id, A.ClaimTask(lease_seconds=600))
            lease = {"attempt_id": claim.lease.attempt_id, "lease_token": claim.lease.fencing_token}
            t1 = self._transition(p, task_id, TaskState.running, claim.task.revision, **lease)
            self._progress(row.id, f"Working on: {pt.title}"[:200], task_id=task_id, endpoint_id=choice.endpoint_id)
            criteria = "\n".join(f"- {c}" for c in pt.acceptance_criteria)
            out = self._call(row, choice, f"work:{key}:{round_}", GenerateRequest(model=choice.model, messages=[
                ChatMessage("system", "You are a specialist. Produce the deliverable only."),
                ChatMessage("user", f"<title>{pt.title}</title>\n<description>{pt.description}</description>\n"
                                    f"<criteria>\n{criteria}\n</criteria>")],
                tools=[WORK_TOOL], tool_choice="submit_work", max_tokens=4096), "submit_work")
            content = str(out["args"].get("content", ""))
            summary = str(out["args"].get("summary", ""))[:200] or pt.title
            media = out["args"].get("media_type") or "text/markdown"
            art_id = uuid.uuid5(NS, f"{row.id}:{key}:{round_}")
            with self.ctx.db.tx() as conn:
                art = artifacts.store_internal(
                    conn, self.ctx.now(), self.store, project_id=row.project_id, user_id=row.requested_by_user_id,
                    name=f"{key}-{pt.title[:60]}.md", media_type=media, data=content.encode(),
                    provenance=Provenance(produced_by=ActorRef(kind=ActorKind.agent, id=row.orchestrator_agent_id),
                                          task_id=task_id, model_endpoint_id=choice.endpoint_id,
                                          model_revision=out["provider_model_id"], trusted_runner=True),
                    visibility=Visibility(task.visibility.value), artifact_id=art_id)
            t2 = self._transition(p, task_id, TaskState.verifying, t1.revision, **lease)
            ok, notes = self._verify(row, choice, key, round_, pt, content)
            if ok:
                self._transition(p, task_id, TaskState.review, t2.revision, result_summary=f"{summary} ({notes})"[:4000],
                                 artifact_refs=[art.id], **lease)
                self._save(row.id, done={**self._row(row.id).checkpoint.get("done", {}), key: [str(art.id)]})
                return
            self._transition(p, task_id, TaskState.ready, t2.revision, reason=notes[:200])
            self._progress(row.id, f"Verification failed for {pt.title}: {notes}"[:200], task_id=task_id)
        raise Blocked(f"task {key} failed verification {MAX_VERIFY_ROUNDS + 1} times")

    def _transition(self, p: Principal, task_id: UUID, state: TaskState, revision: int, **kw: Any):  # noqa: ANN202
        with self.ctx.db.tx() as conn:
            return work.transition_task(conn, self.ctx.now(), p, task_id,
                                        A.TransitionTask(to_state=state, expected_revision=revision, **kw))

    def _verify(self, row, choice: ModelChoice, key: str, round_: int, pt, content: str) -> tuple[bool, str]:  # noqa: ANN001
        """Deterministic checks first; then a model judgment. Both must pass."""
        if not content.strip():
            return False, "empty deliverable"
        if len(content) > 200_000:
            return False, "deliverable too large"
        criteria = "\n".join(f"- {c}" for c in pt.acceptance_criteria)
        out = self._call(row, choice, f"verify:{key}:{round_}", GenerateRequest(model=choice.model, messages=[
            ChatMessage("system", "You are a strict reviewer."),
            ChatMessage("user", f"<criteria>\n{criteria}\n</criteria>\n<deliverable>{content}</deliverable>")],
            tools=[VERIFY_TOOL], tool_choice="verify_work", max_tokens=512), "verify_work")
        return bool(out["args"].get("passed")), str(out["args"].get("notes", ""))[:500]

    # ------------------------------------------------------------ synthesis

    def _synthesize(self, row, p: Principal, plan: PlanProposal, keys: dict[str, UUID], choice: ModelChoice) -> None:  # noqa: ANN001
        cp = self._row(row.id).checkpoint
        done = cp.get("done", {})
        refs = [UUID(a) for k in topo_order(plan) for a in done.get(k, [])]

        def post() -> dict:
            lines = [f"Run finished: {len(keys)} tasks verified and awaiting your review."]
            if choice.is_fake:
                lines.append("Note: produced by the deterministic FAKE provider (development mode), not a real model.")
            for k in topo_order(plan):
                pt = next(x for x in plan.tasks if x.key == k)
                lines.append(f"- {pt.title}: artifact {', '.join(done.get(k, [])) or 'none'}")
            with self.ctx.db.tx() as conn:
                msg = work.post_message(conn, self.ctx.now(), p, row.conversation_id,
                                        A.PostMessage(body="\n".join(lines), artifact_refs=refs), role=MessageRole.agent)
            return {"message_id": str(msg.id)}

        operation(self.ctx.db.tx, self.ctx.now, f"run:{row.id}:synthesis", "post_message", post)
        with self.ctx.db.tx() as conn:
            runs.set_status(conn, self.ctx.now(), row.id, RunStatus.needs_review,
                            f"Needs review: {len(refs)} artifacts", runner_id=self.runner_id)
            runs.end_attempt(conn, self.ctx.now(), row.id, self.runner_id, "succeeded")
