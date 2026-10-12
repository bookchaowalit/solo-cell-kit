"""The MCP adapter: tools per resource, strict arguments, the caller's bearer, no identity arguments.

A fake httpx transport stands in for the cell API; nothing here reaches a real API or database.
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cellkit import mcp
from cellkit.mcp import ToolFailed, build_server
from cellkit.spec import load

ROOT = Path(__file__).resolve().parents[1]
CELL = load(ROOT / "examples" / "demo" / "cell.yaml")
IDENTITY = {"actor", "workspace", "workspace_id", "delegation", "on_behalf_of", "token", "channel", "sub",
            "authorization", "bearer"}


class Api:
    def __init__(self, status: int = 200, body=None):
        self.requests: list[httpx.Request] = []
        self.status, self.body = status, body if body is not None else {"items": []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE" and self.status < 400:
            return httpx.Response(204)
        return httpx.Response(self.status, json=self.body)


def server(api: Api, **kwargs):
    client = httpx.Client(base_url="https://demo.test", transport=httpx.MockTransport(api))
    return build_server(CELL, client, lambda: "tok-123", **kwargs)


def call(srv, name, arguments):
    result = asyncio.run(srv.call_tool(name, arguments))
    return json.loads(result.content[0].text)


def tools(srv):
    return {t.name: t for t in asyncio.run(srv.list_tools())}


def test_tools_per_resource():
    listed = tools(server(Api()))
    expected = {f"demo-things.{r.name}.{op}" for r in CELL.resources
                for op in ("list", "get", "create", "update", "delete")}
    assert set(listed) == expected


def test_read_only_registers_list_and_get_only():
    assert {n.rsplit(".", 1)[1] for n in tools(server(Api(), read_only=True))} == {"list", "get"}


def test_schemas_come_from_the_resource_and_close_additional_properties():
    listed = tools(server(Api()))
    create = listed["demo-things.board.create"].input_schema
    assert create["required"] == ["title"] and create["additionalProperties"] is False
    assert any(o.get("enum") == ["a", "b"] for o in create["properties"]["kind"]["anyOf"])
    assert set(listed["demo-things.board.list"].input_schema["properties"]) == {
        "status", "kind", "limit", "offset", "order"}
    assert "expected_version" in listed["demo-things.board.update"].input_schema["properties"]


def test_no_tool_takes_identity_arguments():
    for tool in tools(server(Api())).values():
        assert not IDENTITY & set(tool.input_schema["properties"]), tool.name


@pytest.mark.parametrize("name, arguments", [
    ("demo-things.board.list", {"actor": "x"}),
    ("demo-things.board.list", {"token": "x"}),
    ("demo-things.board.get", {"id": 1, "workspace_id": "w"}),
    ("demo-things.board.create", {"title": "t", "delegation": "d"}),
    ("demo-things.board.update", {"id": 1, "on_behalf_of": "u"}),
    ("demo-things.board.delete", {"id": 1, "workspace": "w"}),
    ("demo-things.board.create", {}),
    ("demo-things.board.list", {"status": "nonsense"}),
    ("demo-things.board.list", {"limit": 5000}),
])
def test_unknown_or_invalid_arguments_are_rejected_before_any_call(name, arguments):
    api = Api()
    with pytest.raises(ToolFailed, match="invalid_arguments"):
        call(server(api), name, arguments)
    assert api.requests == []


def test_unknown_tool_is_rejected():
    with pytest.raises(ToolFailed, match="unknown tool"):
        call(server(Api()), "demo-things.board.explode", {})


def test_bearer_and_channel_headers_are_sent():
    api = Api()
    call(server(api), "demo-things.board.list", {"status": "active", "limit": 5})
    request = api.requests[0]
    assert request.method == "GET" and request.url.path == "/v1/boards"
    assert request.headers["authorization"] == "Bearer tok-123"
    assert request.headers["x-empire-channel"] == "mcp" and request.headers["x-correlation-id"].startswith("mcp-")
    assert dict(request.url.params) == {"status": "active", "limit": "5", "offset": "0", "order": "asc"}


def test_get_create_update_delete_map_to_the_api():
    api = Api(body={"id": 1000001, "title": "t"})
    srv = server(api)
    call(srv, "demo-things.board.get", {"id": 1000001})
    call(srv, "demo-things.card.create", {"board_id": 1000001, "slug": "c"})
    call(srv, "demo-things.board.update", {"id": 1000001, "title": "new", "parent_id": None, "expected_version": 2})
    assert call(srv, "demo-things.board.delete", {"id": 1000001}) == {"deleted": True}
    got, created, updated, deleted = api.requests
    assert (got.method, got.url.path) == ("GET", "/v1/boards/1000001")
    assert (created.method, created.url.path) == ("POST", "/v1/cards")
    assert json.loads(created.content) == {"board_id": 1000001, "slug": "c"}
    assert created.headers["idempotency-key"].startswith("mcp-")
    assert (updated.method, updated.url.path) == ("PATCH", "/v1/boards/1000001")
    assert json.loads(updated.content) == {"title": "new", "parent_id": None, "expected_version": 2}
    assert (deleted.method, deleted.url.path) == ("DELETE", "/v1/boards/1000001")
    assert all(r.headers["authorization"] == "Bearer tok-123" for r in api.requests)


def test_api_errors_carry_only_the_code():
    with pytest.raises(ToolError, match="403: forbidden"):
        call(server(Api(403, {"detail": {"error": "forbidden", "secret": "no"}})), "demo-things.board.get", {"id": 1})


def test_token_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mcp.bearer({"EMPIRE_TOKEN": "a"}) == "a"
    file = tmp_path / "t.jwt"
    file.write_text("b\n")
    file.chmod(0o600)
    assert mcp.bearer({"EMPIRE_TOKEN_FILE": str(file)}) == "b"
    file.chmod(0o644)
    with pytest.raises(mcp.ConfigError, match="owner only"):
        mcp.bearer({"EMPIRE_TOKEN_FILE": str(file)})
    assert mcp.bearer({"EMPIRE_AUTOMATION_TOKEN_OWNER_CLI": "c"}) == "c"
    assert mcp.bearer({"EMPIRE_MCP_AUTOMATION": "x-y", "EMPIRE_AUTOMATION_TOKEN_X_Y": "d"}) == "d"
    with pytest.raises(mcp.ConfigError, match="no token"):
        mcp.bearer({})


def test_api_url_convention(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mcp.api_key("roles-skills") == "EMPIRE_ROLES_SKILLS_API"
    assert mcp.api_url("goals", {"EMPIRE_GOALS_API": "https://g.test"}) == "https://g.test"
    assert mcp.api_url("goals", {"EMPIRE_GOALS_API": "http://127.0.0.1:3620"})
    with pytest.raises(mcp.ConfigError, match="https"):
        mcp.api_url("goals", {"EMPIRE_GOALS_API": "http://example.com"})
    with pytest.raises(mcp.ConfigError, match="not configured"):
        mcp.api_url("goals", {})
    client_env = tmp_path / "client.env"
    client_env.write_text("EMPIRE_GOALS_API=https://file.test\n")
    client_env.chmod(0o600)
    assert mcp.api_url("goals", {"EMPIRE_CLIENT_CONFIG": str(client_env)}) == "https://file.test"


def test_sync_writes_the_console_script_and_mcp_json():
    from cellkit.new import generate

    dest = Path(tempfile.mkdtemp(prefix="ck-mcp-"))
    shutil.copy(ROOT / "examples" / "demo" / "cell.yaml", dest / "cell.yaml")
    generate(dest)
    assert json.loads((dest / ".mcp.json").read_text()) == {
        "mcpServers": {"demo-things": {"command": ".venv/bin/demo-things-mcp", "args": []}}}
    assert 'demo-things-mcp = "cellkit.mcp:main"' in (dest / "pyproject.toml").read_text()
    assert (dest / "src" / "cellkit" / "mcp.py").is_file()
