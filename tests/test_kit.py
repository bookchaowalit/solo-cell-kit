"""Kit behaviour beyond the per-resource contract: refs, paging, json, uniqueness, the extend hook, specs."""

import uuid

import pytest
from fastapi.testclient import TestClient

from cellkit.app import create_app
from cellkit.auth import TokenVerifier
from cellkit.spec import SpecError, parse
from cellkit.testing import ISSUER


def board(client, owner, **extra):
    response = client.post("/v1/boards", headers=owner, json={"title": f"b-{uuid.uuid4().hex[:6]}", **extra})
    assert response.status_code == 201, response.text
    return response.json()


def test_set_null_and_cascade_refs(client, owner):
    parent = board(client, owner)
    child = board(client, owner, parent_id=parent["id"])
    card = client.post("/v1/cards", headers=owner, json={"board_id": parent["id"], "slug": "c1"}).json()
    assert client.delete(f"/v1/boards/{parent['id']}", headers=owner).status_code == 204
    assert client.get(f"/v1/boards/{child['id']}", headers=owner).json()["parent_id"] is None
    assert client.get(f"/v1/cards/{card['id']}", headers=owner).status_code == 404


def test_paging_order_null_filter_and_json(client, owner):
    made = [board(client, owner, kind="a", tags=[i], settings={"i": i}) for i in range(3)]
    ids = [b["id"] for b in made]
    page = client.get("/v1/boards", headers=owner, params={"id": ids, "limit": 2}).json()["items"]
    assert len(page) == 2
    desc = client.get("/v1/boards", headers=owner, params={"id": ids, "order": "desc"}).json()["items"]
    assert sorted(b["title"] for b in desc) == [b["title"] for b in reversed(desc)]  # ordered by title
    after = client.get("/v1/boards", headers=owner, params={"id": ids, "after_id": min(ids)}).json()["items"]
    assert min(ids) not in [b["id"] for b in after]
    nulls = client.get("/v1/boards", headers=owner, params={"id": ids, "null": "due_date"}).json()["items"]
    assert len(nulls) == 3 and nulls[0]["tags"] in ([0], [1], [2]) and "i" in nulls[0]["settings"]
    assert client.get("/v1/boards", headers=owner, params={"null": "nope"}).status_code == 422
    assert client.post("/v1/boards", headers=owner, json={"title": "x", "tags": {"no": 1}}).status_code == 422


def test_unique_combination_conflicts(client, owner):
    b = board(client, owner)
    assert client.post("/v1/cards", headers=owner, json={"board_id": b["id"], "slug": "same"}).status_code == 201
    assert client.post("/v1/cards", headers=owner, json={"board_id": b["id"], "slug": "same"}).status_code == 409


def test_extend_hook_adds_routes(harness, platform, owner, monkeypatch):
    import sys
    import types

    module = types.ModuleType("demo_extend")

    def register(ctx):
        @ctx.app.get("/v1/boards/summary")
        def summary(caller: ctx.caller):
            ctx.allow(caller, f"{ctx.cell.name}.board:read", "board")
            with ctx.bound(caller) as conn:
                return {"count": conn.execute(f"SELECT count(*) AS n FROM {ctx.cell.schema}.boards").fetchone()["n"]}

    module.register = register
    monkeypatch.setitem(sys.modules, "demo_extend", module)
    cell = parse({"cell": harness.cell.name, "title": "x",
                  "resources": {"board": {"table": "boards", "plural": "boards",
                                          "fields": {"title": {"type": "text", "required": True}}}}},
                 harness.cell.root)
    cell = type(cell)(**{**cell.__dict__, "extend": "demo_extend:register"})
    app = create_app(cell, lambda: harness.cluster.connect(cell.role("api")),
                     TokenVerifier(harness.public_key, ISSUER, cell.audience), platform)
    response = TestClient(app).get("/v1/boards/summary", headers=owner)
    assert response.status_code == 200 and response.json()["count"] >= 0


@pytest.mark.parametrize("bad", [
    {"cell": "Bad_Name", "resources": {"a": {"fields": {"x": {"type": "text"}}}}},
    {"cell": "ok", "resources": {"a": {"fields": {"id": {"type": "text"}}}}},
    {"cell": "ok", "resources": {"a": {"fields": {"x": {"type": "ref", "to": "missing"}}}}},
    {"cell": "ok", "resources": {"a": {"fields": {"x": {"type": "enum"}}}}},
    {"cell": "ok", "resources": {"a": {"filters": ["y"], "fields": {"x": {"type": "text"}}}}},
    {"cell": "ok", "resources": {"a": {"fields": {"x": {"type": "ref", "to": "b", "required": True}}},
                                 "b": {"fields": {"y": {"type": "ref", "to": "a", "required": True}}}}},
])
def test_bad_specs_are_refused(bad):
    with pytest.raises(SpecError):
        parse(bad)
