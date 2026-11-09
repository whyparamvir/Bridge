"""A thin HTTP client for the Bridge routes the bridge uses. Every call has a 5-second
timeout; network failures become BridgeOffline and problem responses become BridgeError."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import BridgeConfig

TIMEOUT = 5.0
LEAVE_TIMEOUT = 2.0
NOTE_TTL = timedelta(days=7)
TASK_LEASE_SECONDS = 3600
PAGE_SIZE = 200  # the server's largest page
MAX_PAGES = 10  # list routes are oldest first: page to the end, but not forever
NOT_BRIDGE = "Bridge answered with something that isn't Bridge. Check the address."
# What an agent may walk through after claiming; review -> done is a person's call.
AGENT_PATH = ("leased", "running", "verifying", "review")


class BridgeOffline(Exception):
    def __init__(self, host: str) -> None:
        super().__init__(host)
        self.host = host


class BridgeError(Exception):
    def __init__(self, detail: str, code: str = "", status: int = 0) -> None:
        super().__init__(detail)
        self.detail, self.code, self.status = detail, code, status


class BridgeClient:
    def __init__(self, cfg: BridgeConfig, transport: httpx.BaseTransport | None = None,
                 timeout: float = TIMEOUT) -> None:
        self.cfg = cfg
        self.host = urlsplit(cfg.url).netloc or cfg.url
        self.project_id = cfg.project_id
        self._http = httpx.Client(base_url=cfg.url, transport=transport, timeout=timeout,
                                  headers={"Authorization": f"Bearer {cfg.token}"} if cfg.token else {})
        self._timeout = timeout
        self.feed_conversation_id: str | None = None

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ plumbing

    def _req(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
             timeout: float | None = None) -> Any:
        try:
            r = self._http.request(method, path, json=json, params=params,
                                   timeout=httpx.Timeout(timeout if timeout is not None else self._timeout))
        except httpx.TransportError as e:  # connect, read and timeout errors alike
            raise BridgeOffline(self.host) from e
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            detail = body.get("detail") or body.get("title") or f"HTTP {r.status_code}"
            raise BridgeError(str(detail), str(body.get("code") or ""), r.status_code)
        if r.status_code == 204 or not r.content:
            return None
        try:
            body = r.json()
        except ValueError:  # a web page or proxy at this address, not the Bridge API
            raise BridgeError(NOT_BRIDGE, "not_bridge", r.status_code) from None
        if not isinstance(body, dict):
            raise BridgeError(NOT_BRIDGE, "not_bridge", r.status_code)
        return body

    def _p(self, suffix: str) -> str:
        return f"/v1/projects/{self.project_id}{suffix}"

    # ------------------------------------------------------------------ team

    def board(self) -> dict:
        b = self._req("GET", self._p("/board"))
        self.feed_conversation_id = b.get("feed_conversation_id") or self.feed_conversation_id
        return b

    def upsert_seat(self, seat_key: str, label: str, status: str, status_text: str | None = None,
                    task_id: str | None = None) -> dict:
        return self._req("PUT", self._p(f"/seats/{seat_key}"),
                         json={"label": label, "status": status, "status_text": status_text, "task_id": task_id})

    def leave_seat(self, seat_key: str) -> None:
        self._req("DELETE", self._p(f"/seats/{seat_key}"), timeout=LEAVE_TIMEOUT)

    def claim(self, seat_key: str, paths: list[str], intent: str, minutes: int | None = None) -> dict:
        body: dict = {"seat_key": seat_key, "paths": paths, "intent": intent}
        if minutes is not None:
            body["minutes"] = minutes
        return self._req("POST", self._p("/claims"), json=body)

    def release(self, seat_key: str, paths: list[str] | None = None) -> int:
        return self._req("POST", self._p("/claims/release"), json={"seat_key": seat_key, "paths": paths})["released"]

    def send_note(self, to_agent_id: str, text: str) -> dict:
        # A fresh correlation per note: notes count toward the per-correlation cap.
        expires = datetime.now(timezone.utc) + NOTE_TTL
        return self._req("POST", self._p("/agent-messages"), json={
            "recipient_agent_id": to_agent_id, "correlation_id": str(uuid.uuid4()),
            "payload": {"type": "note", "text": text}, "expires_at": expires.isoformat(),
            "deduplication_key": uuid.uuid4().hex})

    def pages(self, path: str, params: dict | None = None,
              timeout: float | Callable[[], float] | None = None) -> Iterator[tuple[list[dict], bool]]:
        """(items, more after this page) for each page of a list route, oldest first,
        following next_cursor to the end but for at most MAX_PAGES pages. `timeout` may be
        a function giving each request its timeout (it can raise to stop early)."""
        params = {**(params or {}), "limit": PAGE_SIZE}
        cursor = None
        for _ in range(MAX_PAGES):
            t = timeout() if callable(timeout) else timeout
            body = self._req("GET", path, params={**params, "cursor": cursor} if cursor else params, timeout=t)
            cursor = body.get("next_cursor")
            yield body.get("items") or [], bool(cursor)
            if not cursor:
                return

    def _all(self, path: str, params: dict | None = None) -> tuple[list[dict], bool]:
        """(every item oldest first, whether that is all of them) within the page cap."""
        items: list[dict] = []
        more = False
        for page, more in self.pages(path, params):
            items += page
        return items, not more

    def inbox_all(self) -> tuple[list[dict], bool]:
        """The agent's inbox, oldest first, newest included (see `pages`)."""
        return self._all(self._p("/agent-messages/inbox"))

    def inbox(self) -> list[dict]:
        return self.inbox_all()[0]

    def inbox_pages(self, timeout: float | Callable[[], float] | None = None) -> Iterator[tuple[list[dict], bool]]:
        return self.pages(self._p("/agent-messages/inbox"), timeout=timeout)

    def post_feed(self, text: str) -> dict:
        if not self.feed_conversation_id:
            self.board()
        if not self.feed_conversation_id:
            raise BridgeError("This project has no team feed.")
        return self._req("POST", f"/v1/conversations/{self.feed_conversation_id}/messages", json={"body": text})

    def redeem_invite(self, token: str, display_name: str) -> dict:
        """No auth: turns an invite link's token into this person's agent credential."""
        return self._req("POST", "/v1/invites/redeem", json={
            "token": token, "display_name": display_name, "with_agent_credential": True})

    def create_sibling(self, label: str) -> dict:
        """Another named agent of this agent's person (e.g. their Codex): {agent_id, name,
        label, agent_token}. The same label again returns that agent with a new token."""
        return self._req("POST", "/v1/agents/me/siblings", json={"label": label})

    def events(self, after: str | None, limit: int = 100, timeout: float | None = None) -> dict:
        params = {"after": after, "limit": limit} if after else {"limit": limit}
        return self._req("GET", self._p("/events"), params=params, timeout=timeout)

    def me(self, timeout: float | None = None) -> dict:
        return self._req("GET", "/v1/me", timeout=timeout)

    def board_timeout(self, timeout: float | None = None) -> dict:
        return self._req("GET", self._p("/board"), timeout=timeout)

    # ------------------------------------------------------------------ tasks

    def tasks_all(self, state: str | None = None) -> tuple[list[dict], bool]:
        """The project's tasks, oldest first, newest included (see `pages`)."""
        return self._all(self._p("/tasks"), {"state": state} if state else None)

    def tasks(self, state: str | None = None) -> list[dict]:
        return self.tasks_all(state)[0]

    def get_task(self, task_id: str) -> dict:
        return self._req("GET", f"/v1/tasks/{task_id}")

    def create_task(self, title: str, description: str = "", acceptance_criteria: list[str] | None = None) -> dict:
        # Coordination tasks spend nothing, so the budget is zero.
        return self._req("POST", self._p("/tasks"), json={
            "title": title, "description": description, "acceptance_criteria": acceptance_criteria or [title],
            "dependencies": [], "budget_limit": {"amount_micros": 0, "currency": "USD"},
            "idempotency_key": uuid.uuid4().hex})

    def claim_task(self, task_id: str, lease_seconds: int = TASK_LEASE_SECONDS) -> dict:
        """Returns ClaimResponse {task, lease{attempt_id, fencing_token, expires_at}}."""
        return self._req("POST", f"/v1/tasks/{task_id}/claim", json={"lease_seconds": lease_seconds})

    def renew_task(self, task_id: str, attempt_id: str, lease_token: int,
                   lease_seconds: int = TASK_LEASE_SECONDS) -> dict:
        return self._req("POST", f"/v1/tasks/{task_id}/renew", json={
            "attempt_id": attempt_id, "lease_token": lease_token, "lease_seconds": lease_seconds})

    def transition_task(self, task_id: str, to_state: str, expected_revision: int, attempt_id: str | None = None,
                        lease_token: int | None = None, result_summary: str | None = None) -> dict:
        body: dict = {"to_state": to_state, "expected_revision": expected_revision,
                      "attempt_id": attempt_id, "lease_token": lease_token}
        if result_summary:
            body["result_summary"] = result_summary
        return self._req("POST", f"/v1/tasks/{task_id}/transition", json=body)

    def finish_task(self, task_id: str, attempt_id: str, lease_token: int, summary: str | None = None) -> dict:
        """Walks the claimed task toward done as far as an agent may: leased -> running ->
        verifying -> review, each step with the latest revision. Accepting it as done
        (review -> done) is a person's transition on the server."""
        task = self.get_task(task_id)
        if task["state"] not in AGENT_PATH:
            raise BridgeError(f"The task is {task['state']}.")
        for nxt in AGENT_PATH[AGENT_PATH.index(task["state"]) + 1:]:
            task = self.transition_task(task_id, nxt, task["revision"], attempt_id, lease_token,
                                        summary if nxt == "review" else None)
        return task
