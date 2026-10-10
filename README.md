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
