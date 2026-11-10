"""Relational schema (SQLAlchemy Core). PostgreSQL is the production target; SQLite is a
local development fallback (see docs/PERSISTENCE.md).

Concurrency rule used everywhere: every contended mutation is a single conditional
UPDATE ("compare-and-set") whose WHERE clause encodes the invariant, and the caller
checks rowcount. This is atomic on PostgreSQL (row lock + re-evaluation under READ
COMMITTED) and on SQLite (single writer), so no dialect-specific locking is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeDecorator

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class UTCDateTime(TypeDecorator):
    """Always stores/returns timezone-aware UTC (SQLite has no tz support)."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID = sa.Uuid
TS = UTCDateTime


def _id() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True)


def _fk(name: str, target: str, nullable: bool = False, **kw) -> sa.Column:  # noqa: ANN003
    return sa.Column(name, UUID, sa.ForeignKey(target, ondelete=kw.pop("ondelete", None)),
                     nullable=nullable, **kw)


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, TS, nullable=nullable)


def _money(name: str) -> sa.Column:
    return sa.Column(name, sa.BigInteger, nullable=False, server_default="0")


# ---------------------------------------------------------------- identity & tenancy

users = sa.Table(
    "users", metadata, _id(),
    sa.Column("email", sa.String(320), nullable=True, unique=True),  # null: local install owner
    sa.Column("display_name", sa.String(200), nullable=False),
    sa.Column("password_hash", sa.String(300), nullable=True),
    _ts("created_at"),
    _ts("disabled_at", True),
)

credentials = sa.Table(
    "credentials", metadata, _id(),
    sa.Column("kind", sa.String(16), nullable=False),  # session | agent | worker | executor
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    _fk("user_id", "users.id"),
    _fk("agent_id", "agent_profiles.id", True),
    _fk("worker_id", "workers.id", True),
    _fk("binding_id", "repository_bindings.id", True),
    _ts("created_at"), _ts("expires_at", True), _ts("revoked_at", True),
)

workspaces = sa.Table(
    "workspaces", metadata, _id(),
    sa.Column("name", sa.String(200), nullable=False),
    _fk("created_by", "users.id"),
    _ts("created_at"),
)

workspace_members = sa.Table(
    "workspace_members", metadata,
    _fk("workspace_id", "workspaces.id", primary_key=True),
    _fk("user_id", "users.id", primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),
    _ts("created_at"), _ts("revoked_at", True),
)

workspace_invites = sa.Table(
    "workspace_invites", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    _fk("workspace_id", "workspaces.id"),
    _fk("created_by", "users.id"),
    _ts("expires_at"),
    # Set: redeeming also grants editor on this project.
    _fk("project_id", "projects.id", True),
    # Each redemption is one compare-and-set `uses = uses + 1 WHERE uses < max_uses`.
    sa.Column("max_uses", sa.Integer, nullable=False, server_default="10"),
    sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
)

agent_seats = sa.Table(
    "agent_seats", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("agent_id", "agent_profiles.id"),
    sa.Column("seat_key", sa.String(64), nullable=False),
    sa.Column("label", sa.String(200), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),  # working | idle | away
    sa.Column("status_text", sa.String(200), nullable=True),
    _fk("task_id", "tasks.id", True),
    _ts("last_seen_at"), _ts("created_at"),
    sa.UniqueConstraint("agent_id", "seat_key", name="uq_agent_seats_agent_key"),
)

# Exclusive file claims. Active while released_at is null, expires_at is ahead and the
# seat (agent_id, seat_key) was seen within 15 minutes. Overlap checks serialize on the
# project row, so they are plain reads here.
file_claims = sa.Table(
    "file_claims", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("agent_id", "agent_profiles.id"),
    sa.Column("seat_key", sa.String(64), nullable=False),
    sa.Column("path_key", sa.String(300), nullable=False),  # normalized, lowercase
    sa.Column("path", sa.String(400), nullable=False),  # as given
    sa.Column("intent", sa.String(200), nullable=False),
    _ts("created_at"), _ts("expires_at"), _ts("released_at", True),
    sa.Index("ix_file_claims_project_released", "project_id", "released_at"),
)

projects = sa.Table(
    "projects", metadata, _id(),
    _fk("workspace_id", "workspaces.id"),
    sa.Column("slug", sa.String(63), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("objective", sa.Text, nullable=False, server_default=""),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    _money("budget_limit_micros"), _money("budget_reserved_micros"), _money("budget_spent_micros"),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    # Per-project event counter. Incremented by UPDATE inside each writing transaction, so
    # the row lock orders commits: a reader never sees seq N+1 before N commits.
    sa.Column("event_seq", sa.BigInteger, nullable=False, server_default="0"),
    _ts("created_at"),
    sa.UniqueConstraint("workspace_id", "slug"),
    # Reservations enforce the cap (conditional UPDATE). Spent may exceed it after
    # reconciliation, because real provider usage must always be recorded.
    sa.CheckConstraint("budget_reserved_micros >= 0 AND budget_spent_micros >= 0", name="budget_nonneg"),
)

memberships = sa.Table(
    "memberships", metadata, _id(),
    _fk("workspace_id", "workspaces.id"), _fk("project_id", "projects.id"), _fk("user_id", "users.id"),
    sa.Column("role", sa.String(16), nullable=False),
    _ts("created_at"), _ts("revoked_at", True),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint("project_id", "user_id"),
)

agent_profiles = sa.Table(
    "agent_profiles", metadata, _id(),
    _fk("workspace_id", "workspaces.id"), _fk("owner_user_id", "users.id"),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    _fk("lead_model_endpoint_id", "model_endpoints.id", True),
    sa.Column("max_delegation_depth", sa.Integer, nullable=False, server_default="2"),
    sa.Column("skills", JSON, nullable=False, default=list),
    _ts("created_at"),
)

agent_numbers = sa.Table(
    "agent_numbers", metadata,
    sa.Column("number", sa.Integer, primary_key=True),
    sa.Column("agent_id", sa.Uuid, sa.ForeignKey("agent_profiles.id", ondelete="CASCADE"),
              nullable=False, unique=True),
    sqlite_autoincrement=True,
)

agent_consents = sa.Table(
    "agent_consents", metadata,
    _fk("agent_id", "agent_profiles.id", primary_key=True),
    _fk("project_id", "projects.id", primary_key=True),
    sa.Column("accept_delegations", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("allowed_skills", JSON, nullable=False, default=list),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("updated_at"),
)

agent_presence = sa.Table(
    "agent_presence", metadata,
    _fk("agent_id", "agent_profiles.id", primary_key=True),
    _fk("project_id", "projects.id", primary_key=True),
    sa.Column("worker_ids", JSON, nullable=False, default=list),
    _ts("last_seen_at"),
)

provider_connections = sa.Table(
    "provider_connections", metadata, _id(),
    _fk("workspace_id", "workspaces.id"), _fk("owner_user_id", "users.id", True),
    sa.Column("billing_mode", sa.String(16), nullable=False),
    sa.Column("provider", sa.String(63), nullable=False),
    sa.Column("display_name", sa.String(200), nullable=False),
    sa.Column("secret_ref", sa.String(200), nullable=True),
    sa.Column("base_url", sa.String(500), nullable=True),
    _ts("created_at"), _ts("disabled_at", True),
)

payer_grants = sa.Table(
    "payer_grants", metadata, _id(),
    _fk("connection_id", "provider_connections.id"), _fk("project_id", "projects.id"),
    _fk("grantor_user_id", "users.id"), _fk("grantee_user_id", "users.id"),
    sa.Column("allowed_model_endpoint_ids", JSON, nullable=False, default=list),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    _money("cap_micros"), _money("reserved_micros"), _money("spent_micros"),
    _ts("expires_at"), _ts("revoked_at", True), _ts("created_at"),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    sa.CheckConstraint("reserved_micros >= 0 AND spent_micros >= 0", name="grant_nonneg"),
)

# --------------------------------------------------------------------------- work

conversations = sa.Table(
    "conversations", metadata, _id(),
    _fk("project_id", "projects.id"),
    sa.Column("scope", sa.String(16), nullable=False),
    _fk("owner_user_id", "users.id", True),
    _ts("created_at"),
)

# Self-run team server (BRIDGE_ENV=local): exactly one row, written once by local.bootstrap.
installation = sa.Table(
    "installation", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    _fk("owner_user_id", "users.id"), _fk("workspace_id", "workspaces.id"),
    _fk("project_id", "projects.id"), _fk("feed_conversation_id", "conversations.id"),
    _ts("created_at"),
    sa.CheckConstraint("id = 1", name="single_row"),
)

messages = sa.Table(
    "messages", metadata, _id(),
    _fk("conversation_id", "conversations.id"), _fk("project_id", "projects.id"),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("author_kind", sa.String(16), nullable=False),
    sa.Column("author_id", UUID, nullable=True),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("artifact_refs", JSON, nullable=False, default=list),
    _ts("created_at"),
    sa.Index("ix_messages_conv_created", "conversation_id", "created_at"),
)

runs = sa.Table(
    "runs", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("conversation_id", "conversations.id"),
    _fk("requested_by_user_id", "users.id"), _fk("orchestrator_agent_id", "agent_profiles.id"),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("goal", sa.Text, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    _money("budget_limit_micros"), _money("budget_spent_micros"),
    _fk("budget_reservation_id", "budget_reservations.id", True),
    sa.Column("checkpoint", JSON, nullable=False, default=dict),
    sa.Column("runner_id", sa.String(64), nullable=True),
    _ts("runner_lease_until", True),
    sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("blocked_reason", sa.String(200), nullable=True),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"), _ts("finished_at", True),
    sa.UniqueConstraint("requested_by_user_id", "idempotency_key"),
)

run_attempts = sa.Table(
    "run_attempts", metadata, _id(),
    _fk("run_id", "runs.id"),
    sa.Column("attempt_number", sa.Integer, nullable=False),
    sa.Column("runner_id", sa.String(64), nullable=False),
    sa.Column("outcome", sa.String(24), nullable=False),
    sa.Column("error_code", sa.String(64), nullable=True),
    _ts("started_at"), _ts("ended_at", True),
    sa.UniqueConstraint("run_id", "attempt_number"),
)

tasks = sa.Table(
    "tasks", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("run_id", "runs.id", True),
    _fk("creator_user_id", "users.id"),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("acceptance_criteria", JSON, nullable=False),
    _fk("owner_agent_id", "agent_profiles.id", True),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("blocked_reason", sa.String(200), nullable=True),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    # lease, inline: fence is monotonic per task and never reset
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("attempt_id", UUID, nullable=True),
    _ts("lease_expires_at", True),
    sa.Column("retries", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_retries", sa.Integer, nullable=False, server_default="2"),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    _money("budget_limit_micros"),
    _fk("payer_user_id", "users.id", True),
    sa.Column("visibility", sa.String(16), nullable=False, server_default="project"),
    sa.Column("artifact_refs", JSON, nullable=False, default=list),
    sa.Column("result_summary", sa.Text, nullable=True),
    _ts("created_at"), _ts("updated_at"),
    sa.Index("ix_tasks_project_state", "project_id", "state"),
)

task_dependencies = sa.Table(
    "task_dependencies", metadata,
    _fk("task_id", "tasks.id", primary_key=True),
    _fk("depends_on_task_id", "tasks.id", primary_key=True),
)

agent_messages = sa.Table(
    "agent_messages", metadata, _id(),
    _fk("project_id", "projects.id"),
    _fk("sender_agent_id", "agent_profiles.id"), _fk("recipient_agent_id", "agent_profiles.id"),
    _fk("task_id", "tasks.id", True),
    sa.Column("correlation_id", UUID, nullable=False),
    sa.Column("type", sa.String(16), nullable=False),
    sa.Column("payload", JSON, nullable=False),
    sa.Column("depth", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"), _ts("expires_at"),
    sa.Column("deduplication_key", sa.String(128), nullable=False),
    sa.UniqueConstraint("sender_agent_id", "deduplication_key"),
)

decisions = sa.Table(
    "decisions", metadata, _id(),
    _fk("project_id", "projects.id"),
    sa.Column("key", sa.String(200), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("author_kind", sa.String(16), nullable=False),
    sa.Column("author_id", UUID, nullable=True),
    sa.Column("source_refs", JSON, nullable=False, default=list),
    _fk("supersedes_id", "decisions.id", True),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"),
    sa.Index("ix_decisions_project_key", "project_id", "key"),
)

artifacts = sa.Table(
    "artifacts", metadata, _id(),
    _fk("project_id", "projects.id"),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("media_type", sa.String(120), nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("visibility", sa.String(16), nullable=False),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    _fk("supersedes_id", "artifacts.id", True),
    sa.Column("provenance", JSON, nullable=False),
    sa.Column("storage_key", sa.String(300), nullable=False),
    _fk("created_by_user_id", "users.id"),
    _ts("created_at"),
)

repository_bindings = sa.Table(
    "repository_bindings", metadata, _id(),
    _fk("project_id", "projects.id"),
    sa.Column("provider", sa.String(16), nullable=False),
    sa.Column("owner", sa.String(200), nullable=False),
    sa.Column("repo", sa.String(200), nullable=False),
    sa.Column("installation_id", sa.BigInteger, nullable=False),
    sa.Column("default_branch", sa.String(200), nullable=False),
    _ts("created_at"),
    sa.Column("repository_id", sa.BigInteger, nullable=True),  # GitHub numeric id (required by the API)
    _fk("created_by_user_id", "users.id", True),
    _ts("revoked_at", True),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
)

repository_operations = sa.Table(
    "repository_operations", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("task_id", "tasks.id"),
    _fk("binding_id", "repository_bindings.id"), _fk("requested_by_user_id", "users.id"),
    sa.Column("kind", sa.String(24), nullable=False),  # prepare_branch | open_draft_pr
    sa.Column("operation_key", sa.String(128), nullable=False),
    sa.Column("params", JSON, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("attempt_id", UUID, nullable=True),
    _ts("lease_expires_at", True),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
    sa.Column("reconcile_required", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("evidence", JSON, nullable=True),
    sa.Column("error", sa.String(200), nullable=True),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"), _ts("updated_at"),
    sa.UniqueConstraint("binding_id", "operation_key"),
    sa.Index("ix_repo_ops_binding_state", "binding_id", "state"),
)

# ---------------------------------------------------------------------- compute

workers = sa.Table(
    "workers", metadata, _id(),
    _fk("workspace_id", "workspaces.id"), _fk("owner_user_id", "users.id"),
    sa.Column("display_name", sa.String(200), nullable=False),
    sa.Column("os", sa.String(16), nullable=False),
    sa.Column("arch", sa.String(16), nullable=False),
    sa.Column("isolation", sa.String(32), nullable=False),
    sa.Column("timezone", sa.String(64), nullable=False),
    sa.Column("runtime_images", JSON, nullable=False, default=list),
    sa.Column("gpu_model", sa.String(120), nullable=True),
    sa.Column("limit_cpu", sa.Integer, nullable=False), sa.Column("limit_ram", sa.Integer, nullable=False),
    sa.Column("limit_vram", sa.Integer, nullable=False), sa.Column("limit_disk", sa.Integer, nullable=False),
    # committed totals of live reservations, maintained by conditional UPDATEs
    sa.Column("used_cpu", sa.Integer, nullable=False, server_default="0"),
    sa.Column("used_ram", sa.Integer, nullable=False, server_default="0"),
    sa.Column("used_vram", sa.Integer, nullable=False, server_default="0"),
    sa.Column("used_disk", sa.Integer, nullable=False, server_default="0"),
    sa.Column("used_slots", sa.Integer, nullable=False, server_default="0"),
    sa.Column("on_ac", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("battery_percent", sa.Integer, nullable=True),
    sa.Column("interactive_use", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("heartbeat_seq", sa.BigInteger, nullable=False, server_default="0"),
    _ts("enrolled_at"), _ts("last_heartbeat_at", True), _ts("revoked_at", True),
    sa.CheckConstraint("used_cpu >= 0 AND used_ram >= 0 AND used_vram >= 0 AND used_disk >= 0 "
                       "AND used_slots >= 0", name="used_nonneg"),
)

enrollment_grants = sa.Table(
    "enrollment_grants", metadata, _id(),
    _fk("workspace_id", "workspaces.id"), _fk("user_id", "users.id"),
    sa.Column("grant_hash", sa.String(64), nullable=False, unique=True),
    _ts("expires_at"), _ts("used_at", True), _ts("created_at"),
)

sharing_policies = sa.Table(
    "sharing_policies", metadata,
    _fk("worker_id", "workers.id", primary_key=True),
    _fk("owner_user_id", "users.id"),
    sa.Column("share_compute", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("accept_delegated_tasks", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("share_local_inference", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("paused", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("body", JSON, nullable=False),  # full SharingPolicy document
    sa.Column("shared_cpu", sa.Integer, nullable=False, server_default="0"),
    sa.Column("shared_ram", sa.Integer, nullable=False, server_default="0"),
    sa.Column("shared_vram", sa.Integer, nullable=False, server_default="0"),
    sa.Column("shared_disk", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_concurrent_jobs", sa.Integer, nullable=False, server_default="0"),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("updated_at"),
)

model_endpoints = sa.Table(
    "model_endpoints", metadata, _id(),
    _fk("workspace_id", "workspaces.id"), _fk("owner_user_id", "users.id"),
    sa.Column("body", JSON, nullable=False),  # full ModelEndpoint document
    sa.Column("runtime", sa.String(32), nullable=False),
    sa.Column("execution_location", sa.String(16), nullable=False),
    _fk("worker_id", "workers.id", True), _fk("connection_id", "provider_connections.id", True),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"),
)

model_endpoint_projects = sa.Table(
    "model_endpoint_projects", metadata,
    _fk("model_endpoint_id", "model_endpoints.id", primary_key=True),
    _fk("project_id", "projects.id", primary_key=True),
)

resource_offers = sa.Table(
    "resource_offers", metadata,
    _fk("worker_id", "workers.id", primary_key=True),
    _fk("project_id", "projects.id", primary_key=True),
    sa.Column("body", JSON, nullable=False),
    _ts("reported_at"),
)

resource_reservations = sa.Table(
    "resource_reservations", metadata, _id(),
    _fk("worker_id", "workers.id"), _fk("project_id", "projects.id"),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("subject_id", UUID, nullable=False),
    _fk("requester_user_id", "users.id"),
    sa.Column("cpu", sa.Integer, nullable=False), sa.Column("ram", sa.Integer, nullable=False),
    sa.Column("vram", sa.Integer, nullable=False), sa.Column("disk", sa.Integer, nullable=False),
    sa.Column("slots", sa.Integer, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("fencing_token", sa.Integer, nullable=False),
    _ts("created_at"), _ts("expires_at"),
    sa.Index("ix_reservations_worker_state", "worker_id", "state"),
)

jobs = sa.Table(
    "jobs", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("task_id", "tasks.id"),
    _fk("requested_by_user_id", "users.id"),
    sa.Column("job_type", sa.String(63), nullable=False),
    sa.Column("body", JSON, nullable=False),  # input_hashes, image, resources, network...
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("idempotent", sa.Boolean, nullable=False),
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("attempt_id", UUID, nullable=True),
    _fk("worker_id", "workers.id", True),
    _fk("reservation_id", "resource_reservations.id", True),
    _ts("lease_expires_at", True),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
    sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("result", JSON, nullable=True),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"),
    sa.Index("ix_jobs_state_type", "state", "job_type"),
)

job_attempts = sa.Table(
    "job_attempts", metadata, _id(),
    _fk("job_id", "jobs.id"), _fk("worker_id", "workers.id"),
    sa.Column("fencing_token", sa.Integer, nullable=False),
    _fk("reservation_id", "resource_reservations.id"),
    sa.Column("state", sa.String(16), nullable=False),
    _ts("started_at"), _ts("last_heartbeat_at", True), _ts("ended_at", True),
)

delegations = sa.Table(
    "delegations", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("task_id", "tasks.id"),
    _fk("parent_delegation_id", "delegations.id", True),
    sa.Column("mode", sa.String(24), nullable=False),
    _fk("requester_agent_id", "agent_profiles.id"), _fk("requester_user_id", "users.id"),
    _fk("recipient_agent_id", "agent_profiles.id", True),
    sa.Column("requested_recipient", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("body", JSON, nullable=False),  # criteria, inputs, resources, payer, bounds
    sa.Column("state", sa.String(24), nullable=False),
    _fk("worker_id", "workers.id", True),
    _fk("reservation_id", "resource_reservations.id", True),
    _fk("budget_reservation_id", "budget_reservations.id", True),
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("attempt_id", UUID, nullable=True),
    sa.Column("token_hash", sa.String(64), nullable=True),
    _ts("token_expires_at", True),
    _ts("lease_expires_at", True),
    _ts("deadline"),
    sa.Column("blocked_reason", sa.String(200), nullable=True),
    sa.Column("declined_agent_ids", JSON, nullable=False, default=list),
    sa.Column("decline_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_alternates", sa.Integer, nullable=False, server_default="2"),
    sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("result", JSON, nullable=True),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"),
    sa.UniqueConstraint("requester_user_id", "idempotency_key"),
)

# ------------------------------------------------------------------- money & audit

budget_reservations = sa.Table(
    "budget_reservations", metadata, _id(),
    _fk("project_id", "projects.id"),
    _fk("payer_user_id", "users.id", True), _fk("payer_grant_id", "payer_grants.id", True),
    sa.Column("subject_kind", sa.String(16), nullable=False),
    sa.Column("subject_id", UUID, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    _money("estimate_micros"), _money("consumed_micros"),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"),
)

usage_entries = sa.Table(
    "usage_entries", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("reservation_id", "budget_reservations.id", True),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("model_endpoint_id", UUID, nullable=True),
    sa.Column("provider_model_id", sa.String(200), nullable=True),
    sa.Column("input_tokens", sa.Integer, nullable=True),
    sa.Column("output_tokens", sa.Integer, nullable=True),
    sa.Column("runtime_ms", sa.Integer, nullable=True),
    sa.Column("cost_micros", sa.BigInteger, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    sa.Column("provider_reported", sa.Boolean, nullable=False),
    sa.Column("operation_key", sa.String(200), nullable=True, unique=True),
    _ts("recorded_at"),
)

audit_events = sa.Table(
    "audit_events", metadata, _id(),
    _fk("workspace_id", "workspaces.id", True), _fk("project_id", "projects.id", True),
    sa.Column("actor_kind", sa.String(16), nullable=False),
    sa.Column("actor_id", UUID, nullable=True),
    sa.Column("action", sa.String(200), nullable=False),
    sa.Column("target_id", UUID, nullable=True),
    sa.Column("detail", JSON, nullable=False, default=dict),
    _ts("created_at"),
)

# -------------------------------------------------------- reliability infrastructure

idempotency_records = sa.Table(
    "idempotency_records", metadata,
    sa.Column("scope", sa.String(200), primary_key=True),  # principal + route
    sa.Column("key", sa.String(128), primary_key=True),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("status_code", sa.Integer, nullable=False),
    sa.Column("response", JSON, nullable=False),
    _ts("created_at"),
)

events = sa.Table(
    "events", metadata,
    sa.Column("seq", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True,
              autoincrement=True),
    sa.Column("event_id", UUID, nullable=False, unique=True),
    _fk("project_id", "projects.id"),
    sa.Column("project_seq", sa.BigInteger, nullable=False),
    sa.Column("entity_id", UUID, nullable=False),
    sa.Column("entity_version", sa.Integer, nullable=False),
    sa.Column("type", sa.String(40), nullable=False),
    sa.Column("payload", JSON, nullable=False),
    sa.Column("audience_user_id", UUID, nullable=True),  # set = private to one user
    _ts("created_at"),
    sa.UniqueConstraint("project_id", "project_seq"),
)

outbox = sa.Table(
    "outbox", metadata,
    sa.Column("seq", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True,
              autoincrement=True),
    sa.Column("topic", sa.String(64), nullable=False),
    sa.Column("subject_id", UUID, nullable=False),
    sa.Column("payload", JSON, nullable=False, default=dict),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
    _ts("available_at"),
    sa.Column("claimed_by", sa.String(64), nullable=True),
    _ts("claimed_until", True),
    _ts("done_at", True),
    sa.Column("last_error", sa.String(500), nullable=True),
    _ts("created_at"),
    sa.Index("ix_outbox_pending", "done_at", "available_at"),
)

operations = sa.Table(
    "operations", metadata,
    sa.Column("key", sa.String(200), primary_key=True),  # e.g. run:<id>:task:<n>:artifact
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),  # started | succeeded | failed
    sa.Column("result", JSON, nullable=True),
    _ts("created_at"), _ts("updated_at"),
)


inference_requests = sa.Table(
    "inference_requests", metadata, _id(),
    _fk("project_id", "projects.id"), _fk("model_endpoint_id", "model_endpoints.id"),
    _fk("worker_id", "workers.id"), _fk("requester_user_id", "users.id"),
    sa.Column("purpose", sa.String(16), nullable=False),  # task | probe
    sa.Column("body", JSON, nullable=False),  # prompt, max_tokens, model_id, runtime
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("attempt_id", UUID, nullable=True),
    _fk("reservation_id", "resource_reservations.id", True),
    _ts("lease_expires_at", True),
    _ts("deadline"),
    sa.Column("result", JSON, nullable=True),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    _ts("created_at"),
    sa.Index("ix_inference_worker_state", "worker_id", "state"),
)
