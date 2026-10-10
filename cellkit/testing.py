"""Test support for cells: a throwaway PostgreSQL, a fake platform, token minting and sample records.

A cell's ``tests/conftest.py`` imports the fixtures from here; the contract
tests in ``cellkit.contract`` then run against that cell's own spec, so every
generated cell proves CRUD, authorization, isolation, idempotency, imports and
the outbox for each of its resources.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cellkit.app import create_app
from cellkit.auth import TokenVerifier
from cellkit.db import LocalCluster, Names, migrate
from cellkit.platform import Action, Denied, PlatformUnavailable
from cellkit.spec import Cell, Field, Resource, load

ISSUER = "empire-platform-test"


@dataclass
class FakePlatform:
    """Grants by actor; records every call like the real platform would."""

    grants: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    down: bool = False
    executions: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    keys: dict[str, uuid.UUID] = field(default_factory=dict)
    published: list[dict[str, Any]] = field(default_factory=list)
    fail_publish: bool = False
    lose_finish: bool = False

    @staticmethod
    def _actor(token: str) -> uuid.UUID:
        return uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sub"])

    def decide(self, token: str, channel: str, action: Action) -> bool:
        if self.down:
            raise PlatformUnavailable("down")
        return action.action in self.grants.get(self._actor(token), set())

    def begin(self, token, channel, action, idempotency_key, correlation_id, also=None):
        if self.down:
            raise PlatformUnavailable("down")
        if idempotency_key and idempotency_key in self.keys:
            execution = self.keys[idempotency_key]
            return execution, True, self.executions[execution]["status"]
        for needed in [action.action, *(also or [])]:
            if not self.decide(token, channel, Action(needed, action.resource_type, action.resource_id)):
                raise Denied(403, "deny", "no_permission")
        execution = uuid.uuid4()
        self.executions[execution] = {"permission": action.action, "bound_to": action.resource_id, "status": "running",
                                      "actor": self._actor(token)}
        if idempotency_key:
            self.keys[idempotency_key] = execution
        return execution, False, "running"

    def finish(self, token, channel, execution, outcome):
        if self.lose_finish:
            raise PlatformUnavailable("lost")
        self.executions[execution].update(outcome)

    def publish(self, token, event):
        if self.fail_publish:
            raise PlatformUnavailable("down")
        self.published.append(event)
        return "applied"


def spec_from(root: Path) -> Cell:
    return load(root / "cell.yaml")


def sample(f: Field, n: int = 0) -> Any:
    """A valid value for a field (refs are filled in by the caller)."""
    if f.type == "enum":
        return f.values[n % len(f.values)]
    if f.type == "text":
        return f"{f.name}-{uuid.uuid4().hex[:8]}"[: f.max_length or 200]
    if f.type in ("int", "bigint", "real"):
        low = f.min if f.min is not None else 1
        high = f.max if f.max is not None else low + 10
        value = min(high, low + n)
        return float(value) if f.type == "real" else int(value)
    if f.type == "bool":
        return n % 2 == 0
    if f.type == "date":
        return f"2026-10-{10 + n % 10:02d}"
    if f.type == "timestamp":
        return f"2026-10-{10 + n % 10:02d}T08:00:00+00:00"
    if f.type == "json":
        return [n] if f.json_kind == "array" else {"n": n}
    raise ValueError(f.type)


def body(r: Resource, refs: dict[str, int], n: int = 0, *, everything: bool = False) -> dict[str, Any]:
    """A create body: required fields (all fields with ``everything``), refs pointing at ``refs[target]``."""
    out: dict[str, Any] = {}
    for f in r.fields:
        if not (f.required or everything):
            continue
        if f.type == "ref":
            if f.to in refs:
                out[f.name] = refs[f.to]
        else:
            out[f.name] = sample(f, n)
    return out


@dataclass
class Harness:
    cell: Cell
    cluster: LocalCluster
    private_key: str
    public_key: str

    def token(self, sub: uuid.UUID, ws: uuid.UUID, *, ttl: int = 600, audience: Any = None) -> dict[str, str]:
        now = int(time.time())
        aud = audience or [self.cell.audience, "platform-api"]
        claims = {"iss": ISSUER, "aud": aud, "sub": str(sub), "ws": str(ws), "iat": now, "exp": now + ttl,
                  "jti": uuid.uuid4().hex}
        return {"Authorization": f"Bearer {jwt.encode(claims, self.private_key, algorithm='RS256')}"}

    def client(self, platform: FakePlatform) -> TestClient:
        return TestClient(create_app(self.cell, lambda: self.cluster.connect(self.cell.role("api")),
                                     TokenVerifier(self.public_key, ISSUER, self.cell.audience), platform))

    def superuser(self, query: str, params: tuple = ()) -> list[tuple]:
        with self.cluster.connect("postgres", self.cell.schema) as conn:
            return conn.execute(query, params).fetchall()


@pytest.fixture(scope="session")
def harness(request) -> Iterator[Harness]:
    """Uses the ``cell_root`` fixture of the cell's conftest (the repository root)."""
    root: Path = request.getfixturevalue("cell_root")
    cell = spec_from(root)
    tmp = Path(tempfile.mkdtemp(prefix="ck-"))
    cluster = LocalCluster(Names(cell), tmp / "data", tmp / "sock")
    cluster.init()
    cluster.start()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    try:
        cluster.bootstrap()
        migrate(cluster)
        yield Harness(cell, cluster, private, public)
    finally:
        cluster.stop()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def owner(harness, platform) -> dict[str, str]:
    actor, ws = uuid.uuid4(), uuid.uuid4()
    platform.grants[actor] = set(harness.cell.permissions())
    return harness.token(actor, ws)


@pytest.fixture
def client(harness, platform) -> TestClient:
    return harness.client(platform)
