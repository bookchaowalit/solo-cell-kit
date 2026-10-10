"""Contract tests every cell runs against its own spec (``from cellkit.contract import *``).

They are parametrized over the cell's resources and need the fixtures of
``cellkit.testing`` (the cell's conftest provides ``cell_root``).
"""

from __future__ import annotations

import random
import uuid
from typing import Any

import pytest

from cellkit.publisher import publish_pending
from cellkit.spec import LEGACY_LIMIT, Cell, Resource
from cellkit.testing import body, sample

__all__ = ["test_lifecycle", "test_executions_are_bound_to_the_record", "test_no_reference_across_workspaces", "test_authorization", "test_workspace_isolation", "test_idempotent_create",
           "test_import_keeps_the_old_id", "test_validation", "test_version_conflict", "test_missing_reference",
           "test_publisher_delivers_the_outbox", "pytest_generate_tests"]


def pytest_generate_tests(metafunc):
    if "resource" in metafunc.fixturenames:
        from cellkit.testing import spec_from

        import os
        from pathlib import Path

        cell = spec_from(Path(os.environ.get("CELLKIT_ROOT") or metafunc.config.rootpath))
        metafunc.parametrize("resource", list(cell.resources), ids=[r.name for r in cell.resources])


def make(client, headers, cell: Cell, r: Resource, n: int = 0, *, everything: bool = False,
         **extra: Any) -> dict[str, Any]:
    """Create one record of ``r`` and, first, one of every resource its refs point at."""
    refs = {}
    for f in r.refs:
        if (f.required or everything) and f.to != r.name and f.name not in extra:
            refs[f.to] = make(client, headers, cell, cell.resource(f.to), n)["id"]
    payload = {**body(r, refs, n, everything=everything), **extra}
    response = client.post(f"/v1/{r.plural}", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_lifecycle(client, owner, harness, resource):
    cell, r = harness.cell, resource
    record = make(client, owner, cell, r, everything=True)
    assert record["version"] == 1 and record["id"] >= LEGACY_LIMIT
    path = f"/v1/{r.plural}/{record['id']}"
    assert client.get(path, headers=owner).json() == record
    for name in r.filters:
        value = record[name]
        if value is None:
            continue
        listed = client.get(f"/v1/{r.plural}", headers=owner, params={name: str(value).lower()
                                                                      if isinstance(value, bool) else value})
        assert listed.status_code == 200, listed.text
        assert record["id"] in [item["id"] for item in listed.json()["items"]]
    by_id = client.get(f"/v1/{r.plural}", headers=owner, params={"id": record["id"]}).json()["items"]
    assert [item["id"] for item in by_id] == [record["id"]]
    editable = next((f for f in r.fields if f.type not in ("ref",)), None)
    if editable is not None:
        new_value = sample(editable, 1)
        changed = client.patch(path, headers=owner, json={editable.name: new_value})
        assert changed.status_code == 200, changed.text
        expected_version = 1 if changed.json()[editable.name] == record[editable.name] else 2
        assert changed.json()["version"] == expected_version
        again = client.patch(path, headers=owner, json={editable.name: changed.json()[editable.name]}).json()
        assert again["version"] == expected_version  # unchanged writes do not bump the version
    if r.filters:
        groups = client.get(f"/v1/{r.plural}/stats", headers=owner, params={"group_by": r.filters[0]}).json()
        assert sum(groups["groups"].values()) >= 1
    assert client.delete(path, headers=owner).status_code == 204
    assert client.get(path, headers=owner).status_code == 404
    if r.graph is not None:
        types = harness.superuser(f"SELECT type FROM {cell.schema}.outbox WHERE subject = %s ORDER BY occurred_at",
                                  (f"empire:{cell.name}:{r.name}:{record['id']}",))
        assert types[0][0] == f"{cell.name}.{r.name}.created" and types[-1][0] == f"{cell.name}.{r.name}.deleted"


def test_executions_are_bound_to_the_record(client, owner, harness, platform, resource):
    cell, r = harness.cell, resource
    record = make(client, owner, cell, r)
    client.patch(f"/v1/{r.plural}/{record['id']}", headers=owner, json={})
    client.delete(f"/v1/{r.plural}/{record['id']}", headers=owner)
    bound = [(e["permission"], e["bound_to"], e.get("resource_id")) for e in platform.executions.values()
             if e.get("resource_id") == str(record["id"]) and e.get("resource_type") == r.name]
    assert {p.rsplit(":", 1)[1] for p, _, _ in bound} >= {"write", "delete"}
    assert all(bound_to == str(record["id"]) == audited for _, bound_to, audited in bound)


def test_no_reference_across_workspaces(client, owner, harness, platform, resource):
    cell, r = harness.cell, resource
    refs = [f for f in r.refs if f.to != r.name]
    if not refs:
        pytest.skip("no refs to other resources")
    target = make(client, owner, cell, cell.resource(refs[0].to))
    other_actor = uuid.uuid4()
    platform.grants[other_actor] = set(cell.permissions())
    other = harness.token(other_actor, uuid.uuid4())
    own = {f.to: make(client, other, cell, cell.resource(f.to))["id"] for f in r.refs if f.required}
    payload = {**body(r, own), refs[0].name: target["id"]}
    assert client.post(f"/v1/{r.plural}", headers=other, json=payload).status_code == 422


def test_authorization(client, owner, harness, platform, resource):
    cell, r = harness.cell, resource
    record = make(client, owner, cell, r)
    refs = {f.to: make(client, owner, cell, cell.resource(f.to))["id"] for f in r.refs if f.required}
    stranger = harness.token(uuid.uuid4(), uuid.uuid4())
    assert client.get(f"/v1/{r.plural}", headers=stranger).status_code == 403
    assert client.get(f"/v1/{r.plural}/{record['id']}", headers=stranger).status_code == 404
    assert client.post(f"/v1/{r.plural}", headers=stranger, json=body(r, refs)).status_code == 403
    assert client.get(f"/v1/{r.plural}").status_code == 401
    assert client.get(f"/v1/{r.plural}", headers={"Authorization": "Bearer nope"}).status_code == 401
    wrong_audience = harness.token(uuid.uuid4(), uuid.uuid4(), audience=["other-api"])
    assert client.get(f"/v1/{r.plural}", headers=wrong_audience).status_code == 401
    platform.down = True
    assert client.get(f"/v1/{r.plural}", headers=owner).status_code == 503
    platform.down = False
    reader = uuid.uuid4()
    platform.grants[reader] = {f"{cell.name}.{r.name}:read"}
    read_only = harness.token(reader, uuid.uuid4())
    assert client.patch(f"/v1/{r.plural}/{record['id']}", headers=read_only, json={}).status_code == 403
    assert client.delete(f"/v1/{r.plural}/{record['id']}", headers=read_only).status_code == 403


def test_workspace_isolation(client, owner, harness, platform, resource):
    cell, r = harness.cell, resource
    record = make(client, owner, cell, r)
    other_actor = uuid.uuid4()
    platform.grants[other_actor] = set(cell.permissions())
    other = harness.token(other_actor, uuid.uuid4())
    assert record["id"] not in [i["id"] for i in client.get(f"/v1/{r.plural}", headers=other).json()["items"]]
    assert client.get(f"/v1/{r.plural}/{record['id']}", headers=other).status_code == 404
    assert client.patch(f"/v1/{r.plural}/{record['id']}", headers=other, json={}).status_code == 404
    assert client.delete(f"/v1/{r.plural}/{record['id']}", headers=other).status_code == 404


def test_idempotent_create(client, owner, harness, resource):
    cell, r = harness.cell, resource
    refs = {f.to: make(client, owner, cell, cell.resource(f.to))["id"] for f in r.refs if f.required}
    payload = body(r, refs)
    key = {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}
    first = client.post(f"/v1/{r.plural}", headers={**owner, **key}, json=payload)
    second = client.post(f"/v1/{r.plural}", headers={**owner, **key}, json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    rows = harness.superuser(f"SELECT count(*) FROM {cell.schema}.{r.table} WHERE id = %s", (first.json()["id"],))
    assert rows[0][0] == 1


def test_import_keeps_the_old_id(client, owner, harness, resource):
    cell, r = harness.cell, resource
    legacy = random.randint(1, LEGACY_LIMIT - 1)
    record = make(client, owner, cell, r, legacy_id=legacy)
    assert record["id"] == legacy and record["legacy_id"] == legacy
    assert client.get(f"/v1/{r.plural}/legacy/{legacy}", headers=owner).json()["id"] == legacy
    refs = {f.to: make(client, owner, cell, cell.resource(f.to))["id"] for f in r.refs if f.required}
    again = client.post(f"/v1/{r.plural}", headers=owner, json={**body(r, refs), "legacy_id": legacy})
    assert again.status_code == 409
    assert client.post(f"/v1/{r.plural}", headers=owner,
                       json={**body(r, refs), "legacy_id": LEGACY_LIMIT}).status_code == 422


def test_validation(client, owner, harness, resource):
    cell, r = harness.cell, resource
    record = make(client, owner, cell, r)
    assert client.post(f"/v1/{r.plural}", headers=owner, json={"no_such_field": 1}).status_code == 422
    assert client.get(f"/v1/{r.plural}", headers=owner, params={"no_such_filter": "x"}).status_code == 422
    assert client.get(f"/v1/{r.plural}", headers=owner, params={"limit": 0}).status_code == 422
    for f in r.fields:
        if f.required:
            assert client.patch(f"/v1/{r.plural}/{record['id']}", headers=owner,
                                json={f.name: None}).status_code == 422
        if f.type == "enum":
            assert client.patch(f"/v1/{r.plural}/{record['id']}", headers=owner,
                                json={f.name: "not-a-value"}).status_code == 422


def test_version_conflict(client, owner, harness, resource):
    cell, r = harness.cell, resource
    record = make(client, owner, cell, r)
    stale = client.patch(f"/v1/{r.plural}/{record['id']}", headers=owner, json={"expected_version": 99})
    assert stale.status_code == 409 and stale.json()["detail"]["version"] == record["version"]


def test_missing_reference(client, owner, harness, resource):
    cell, r = harness.cell, resource
    if not r.refs:
        pytest.skip("no refs")
    f = r.refs[0]
    refs = {g.to: make(client, owner, cell, cell.resource(g.to))["id"] for g in r.refs if g.required}
    payload = {**body(r, refs), f.name: LEGACY_LIMIT * 50 + random.randint(1, 1000)}
    assert client.post(f"/v1/{r.plural}", headers=owner, json=payload).status_code == 422


def test_publisher_delivers_the_outbox(client, owner, harness, platform, resource):
    cell, r = harness.cell, resource
    if r.graph is None:
        pytest.skip("no graph events")
    record = make(client, owner, cell, r)
    token = owner["Authorization"].removeprefix("Bearer ")
    with harness.cluster.connect(cell.role("publisher")) as conn:
        publish_pending(conn, cell.schema, platform, token, limit=500)
    subjects = {e["subject"] for e in platform.published}
    assert f"empire:{cell.name}:{r.name}:{record['id']}" in subjects
