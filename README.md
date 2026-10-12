# solo-cell-kit

Make a Solo Empire cell from a `cell.yaml`: its own repository, HTTP API,
PostgreSQL schema (Neon), platform write path, graph outbox, deploy files and
contract tests. Every cell made with the kit follows the same rules as the
hand-built cells (solo-decisions, solo-habits, solo-notifications):

- one cell = one repository = one Railway project = one Neon project;
- reads ask empire-platform `/v1/authz/decide`; writes run precheck → local
  change → platform execution bound to the exact record → commit → finish
  with row hashes;
- forced row-level security per workspace; ids start at 1,000,000 so an import
  keeps the old ids (`legacy_id`) and references from other cells stay valid;
- graph events through an outbox and a cron publisher (resources with `graph`).

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q                       # contract + kit tests on examples/demo
mkdir ../solo-goals && cp my-cell.yaml ../solo-goals/cell.yaml
.venv/bin/cellkit-new ../solo-goals       # writes the repo; the kit is vendored into src/cellkit
.venv/bin/cellkit-new ../solo-goals --sync  # later: refresh the vendored kit
```

See `cellkit/spec.py` for the spec format, `cellkit/app.py` for the routes and
`cellkit/contract.py` for what every cell proves in its own test run.

## MCP server (kit 0.2.0)

Every cell gets its own MCP server (`cellkit/mcp.py`, vendored into `src/cellkit`):
`cellkit-new <cell> --sync` adds the console script `<cell>-mcp` (stdio) and a
repo-level `.mcp.json` (`.venv/bin/<cell>-mcp`, no secrets). Tools are made from
`cell.yaml`: `<cell>.<resource>.list|get|create|update|delete`, with argument
schemas from the resource's fields (list takes the declared `filters`, `limit`,
`offset`, `order`).

- Every tool calls the cell's HTTP API with the caller's own token; no tool reads
  the database; no tool takes actor, workspace, delegation or token arguments;
  strict models reject unknown arguments.
- URL: `EMPIRE_<CELL>_API` (env or `~/.config/empire-platform/client.env`), the same
  as solo-ops `cell_store.py` and solo-mcp `cell-store.ts`.
- Token: `EMPIRE_TOKEN`, else `EMPIRE_TOKEN_FILE` (mode 600), else the automation named
  by `EMPIRE_MCP_AUTOMATION` (default `owner-cli`): `EMPIRE_AUTOMATION_TOKEN_<NAME>` or
  `~/.config/empire-platform/tokens/<name>.jwt`.
- `EMPIRE_MCP_READONLY=1` registers only `list` and `get`.
- The `mcp` library (`mcp>=2.3,<3`, as empire-mcp) is in the cell's `dev` and `mcp` extras,
  not in the production image dependencies.

After `--sync` in a cell: `uv pip install --python .venv/bin/python -e ".[dev]"`, then
`.venv/bin/pytest -q`. Tests for the adapter are `tests/test_mcp.py` (fake httpx transport).
