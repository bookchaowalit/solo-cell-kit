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


# --- review fixes (0.2.1) -----------------------------------------------------------------------

def test_sync_keeps_env_and_other_servers_in_mcp_json():
    from cellkit.new import generate

    dest = Path(tempfile.mkdtemp(prefix="ck-mcpjson-"))
    shutil.copy(ROOT / "examples" / "demo" / "cell.yaml", dest / "cell.yaml")
    (dest / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "demo-things": {"command": "old", "args": ["x"], "env": {"EMPIRE_MCP_READONLY": "1"}},
        "other": {"command": "other-server"}}}))
    generate(dest, sync=True)
    servers = json.loads((dest / ".mcp.json").read_text())["mcpServers"]
    assert servers["demo-things"] == {"command": ".venv/bin/demo-things-mcp", "args": [],
                                      "env": {"EMPIRE_MCP_READONLY": "1"}}
    assert servers["other"] == {"command": "other-server"}


def test_errors_never_carry_upstream_text():
    secret = "S3CR3T-body"
    for status, word in [(403, "forbidden"), (404, "not_found"), (409, "conflict"), (422, "invalid"),
                         (429, "rate_limited"), (503, "upstream_error"), (418, "error")]:
        for detail in ({"reason": secret}, {"error": {"nested": secret}}, secret):
            with pytest.raises(ToolError) as caught:
                call(server(Api(status, {"detail": detail, "message": secret})), "demo-things.board.get", {"id": 1})
            assert str(caught.value).endswith(f"{status}: {word}") and secret not in str(caught.value)


@pytest.mark.parametrize("bad", ["a b", "tok\nen", "tok\x00", "tök", "  "])
def test_bad_tokens_fail_with_a_fixed_error_and_never_echo(bad):
    with pytest.raises(mcp.ConfigError) as caught:
        mcp.bearer({"EMPIRE_TOKEN": bad})
    assert bad.strip() not in str(caught.value) or not bad.strip()
    api = Api()
    client = httpx.Client(base_url="https://demo.test", transport=httpx.MockTransport(api))
    with pytest.raises(ToolError) as caught:
        call(build_server(CELL, client, lambda: bad), "demo-things.board.get", {"id": 1})
    assert bad.strip() not in str(caught.value) or not bad.strip()
    assert api.requests == [] and caught.value.__cause__ is None or "Illegal header" not in repr(caught.value)


def test_identity_named_fields_are_not_exposed():
    from cellkit.spec import parse

    cell = parse({"cell": "x", "resources": {"thing": {"filters": ["actor", "status"], "fields": {
        "title": {"type": "text", "required": True}, "status": {"type": "text"},
        "actor": {"type": "text"}, "workspace": {"type": "text"}, "token": {"type": "text"},
        "owner_id": {"type": "int"}, "tenant": {"type": "text"}, "authorization": {"type": "text"}}}}})
    with pytest.warns(UserWarning, match="identity-like"):
        srv = build_server(cell, httpx.Client(base_url="https://x.test"), lambda: "t")
    for tool in tools(srv).values():
        assert not mcp.IDENTITY_NAMES & set(tool.input_schema["properties"]), tool.name
    assert {"title", "status"} <= set(tools(srv)["x.thing.create"].input_schema["properties"])


def test_create_idempotency_key():
    api = Api(body={"id": 1})
    srv = server(api)
    call(srv, "demo-things.board.create", {"title": "t", "idempotency_key": "retry-key-0001"})
    call(srv, "demo-things.board.create", {"title": "t"})
    given, generated = api.requests
    assert given.headers["idempotency-key"] == "retry-key-0001" and json.loads(given.content) == {"title": "t"}
    assert generated.headers["idempotency-key"].startswith("mcp-")
    for bad in ("short", "has space in it", "x" * 129):
        with pytest.raises(ToolFailed, match="invalid_arguments: idempotency_key"):
            call(srv, "demo-things.board.create", {"title": "t", "idempotency_key": bad})


@pytest.mark.parametrize("url", ["https://u:p@a.test", "https://a.test?x=1", "https://a.test#f",
                                 "https://a.test:99999", "https://a.test:abc", "ftp://a.test"])
def test_api_url_rejects_userinfo_query_fragment_bad_port(url, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(mcp.ConfigError) as caught:
        mcp.api_url("goals", {"EMPIRE_GOALS_API": url})
    assert "u:p" not in str(caught.value)
    assert mcp.api_url("goals", {"EMPIRE_GOALS_API": "https://a.test:8443/base"})


@pytest.mark.parametrize("name", ["../x", "/abs/path", "A", "a/b", "a.b", "-a", "a" * 64, "a b"])
def test_automation_name_is_restricted(name):
    with pytest.raises(mcp.ConfigError, match="EMPIRE_MCP_AUTOMATION"):
        mcp.bearer({"EMPIRE_MCP_AUTOMATION": name})


def test_json_looking_text_passes():
    api = Api(body={"id": 1})
    srv = server(api)
    call(srv, "demo-things.card.create", {"board_id": 1, "slug": "s", "note": "{}"})
    call(srv, "demo-things.card.update", {"id": 1, "note": "[]"})
    assert json.loads(api.requests[0].content)["note"] == "{}"
    assert json.loads(api.requests[1].content) == {"note": "[]"}


def test_resource_owning_an_idempotency_key_field_builds_and_sends_it():
    from cellkit.spec import parse

    cell = parse({"cell": "x", "resources": {"item": {"fields": {
        "title": {"type": "text", "required": True}, "idempotency_key": {"type": "text"}}}}})
    api = Api(body={"id": 1})
    srv = build_server(cell, httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(api)), lambda: "t")
    call(srv, "x.item.create", {"title": "t", "idempotency_key": "own-key-0001"})
    (request,) = api.requests
    assert json.loads(request.content) == {"title": "t", "idempotency_key": "own-key-0001"}
    assert request.headers["idempotency-key"] == "own-key-0001"


def test_sync_keeps_cell_customised_files():
    from cellkit.new import generate

    dest = Path(tempfile.mkdtemp(prefix="ck-keep-"))
    shutil.copy(ROOT / "examples" / "demo" / "cell.yaml", dest / "cell.yaml")
    custom = {".railway/railway.ts": "// custom service\n", ".gitignore": "custom\n",
              ".dockerignore": "custom\n", "Dockerfile": "FROM custom\n", "package.json": "{\"custom\": true}\n"}
    for name, text in custom.items():
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(text)
    generate(dest, sync=True)
    for name, text in custom.items():
        assert (dest / name).read_text() == text, name
    assert (dest / "src" / "cellkit" / "mcp.py").exists() and (dest / ".mcp.json").exists()
