"""Client for the empire-platform HTTP entry points used by this cell.

The caller's own bearer token is forwarded, so the platform decides, records,
and attributes every action for the real caller. Fails closed: any transport
error or unexpected answer is treated as "not allowed".
"""

from __future__ import annotations

import ipaddress
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class PlatformUnavailable(Exception):
    pass


class Denied(Exception):
    def __init__(self, status: int, decision: str, reason: str) -> None:
        super().__init__(f"{decision}: {reason}")
        self.status = status
        self.decision = decision
        self.reason = reason


@dataclass(frozen=True)
class Action:
    action: str
    resource_type: str
    resource_id: str
    approval_id: uuid.UUID | None = None


class Platform(Protocol):
    def decide(self, token: str, channel: str, action: Action) -> bool: ...

    def begin(self, token: str, channel: str, action: Action, idempotency_key: str | None,
              correlation_id: str | None, also: list[str] | None = None) -> tuple[uuid.UUID, bool, str]: ...

    def finish(self, token: str, channel: str, execution: uuid.UUID, outcome: dict[str, Any]) -> None: ...

    def publish(self, token: str, event: dict[str, Any]) -> str: ...


def _checked_base(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError("EMPIRE_PLATFORM_URL must be https (http only on loopback)")
    return url.rstrip("/")


class HttpPlatform:
    def __init__(self, base_url: str, timeout: float = 10.0, client: httpx.Client | None = None) -> None:
        self.base = _checked_base(base_url)
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def _post(self, path: str, token: str, channel: str, body: dict[str, Any]) -> httpx.Response:
        try:
            return self.client.post(f"{self.base}{path}", json=body, headers={
                "Authorization": f"Bearer {token}", "X-Empire-Channel": channel})
        except httpx.HTTPError as exc:
            raise PlatformUnavailable(type(exc).__name__) from None

    @staticmethod
    def _body(action: Action) -> dict[str, Any]:
        body: dict[str, Any] = {"action": action.action, "resource_type": action.resource_type,
                                "resource_id": action.resource_id}
        if action.approval_id:
            body["approval_id"] = str(action.approval_id)
        return body

    def decide(self, token: str, channel: str, action: Action) -> bool:
        response = self._post("/v1/authz/decide", token, channel, self._body(action))
        if response.status_code == 401:
            raise Denied(401, "unauthenticated", "platform_refused_token")
        if response.status_code != 200:
            raise PlatformUnavailable(f"decide {response.status_code}")
        return response.json().get("decision") == "allow"

    def begin(self, token: str, channel: str, action: Action, idempotency_key: str | None,
              correlation_id: str | None, also: list[str] | None = None) -> tuple[uuid.UUID, bool, str]:
        """(execution, replayed, status); every permission in ``action`` and ``also`` is enforced together."""
        body = self._body(action)
        if also:
            body["also"] = sorted(set(also))
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        if correlation_id:
            body["correlation_id"] = correlation_id
        response = self._post("/v1/executions", token, channel, body)
        if response.status_code in (401, 403, 409):
            detail = response.json().get("detail", {}) if response.content else {}
            raise Denied(response.status_code, str(detail.get("error", "deny")), str(detail.get("reason", "")))
        if response.status_code != 201:
            raise PlatformUnavailable(f"begin {response.status_code}")
        data = response.json()
        return uuid.UUID(data["execution_id"]), bool(data.get("replayed")), str(data.get("status", "running"))

    def finish(self, token: str, channel: str, execution: uuid.UUID, outcome: dict[str, Any]) -> None:
        response = self._post(f"/v1/executions/{execution}/finish", token, channel, outcome)
        if response.status_code != 200:
            raise PlatformUnavailable(f"finish {response.status_code}")

    def publish(self, token: str, event: dict[str, Any]) -> str:
        response = self._post("/v1/graph/events", token, "event", event)
        if response.status_code != 200:
            raise PlatformUnavailable(f"publish {response.status_code}")
        return str(response.json().get("outcome"))
