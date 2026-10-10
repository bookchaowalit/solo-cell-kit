"""Make (or refresh) a cell repository from its ``cell.yaml``.

    cellkit-new <dest-dir>            write everything for the cell described by <dest-dir>/cell.yaml
    cellkit-new <dest-dir> --sync     refresh only the vendored kit (src/cellkit) and generated scaffolding

The kit is vendored (copied into ``src/cellkit``) so every cell builds and
deploys on its own, without reaching this repository. The first migration is
written once; it is never regenerated after it exists (applied migrations are
checksummed), so later schema changes are new hand-written migrations.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from cellkit import sqlgen
from cellkit.spec import Cell, load

KIT = Path(__file__).resolve().parent

PYPROJECT = """[project]
name = "solo-{name}"
version = "0.1.0"
description = "{title} cell: its own API and database (made with solo-cell-kit)"
requires-python = ">=3.12"
dependencies = [
    "psycopg[binary]>=3.2,<4",
    "fastapi>=0.115,<1",
    "pyjwt[crypto]>=2.10,<3",
    "uvicorn>=0.30,<1",
    "httpx>=0.27,<1",
    "pyyaml>=6,<7",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pgserver>=0.1.4"]

[project.scripts]
{name}-api = "cellkit.server:api"
{name}-publish = "cellkit.server:run_publish"
{name}-db = "cellkit.server:run_db"

[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
"""

DOCKERFILE = """FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 CELL_APP_ROOT=/app
WORKDIR /app
COPY pyproject.toml README.md cell.yaml ./
COPY src ./src
COPY migrations ./migrations
COPY db ./db
RUN pip install --no-cache-dir . \\
    && useradd --system --uid 10001 --no-create-home cell
USER cell
# Secrets (database URLs, public key, publisher token) arrive as environment variables at runtime only.
CMD ["{name}-api"]
"""

RAILWAY = """import {{ defineRailway, project, service }} from "railway/iac";

// solo-{name}: the {title} cell. Variables stay unmanaged (secrets live in Railway only).
// {name}-api sleeps when idle (Railway Serverless) and wakes on the next request.{publisher_note}
// Every service runs in Singapore next to Neon (aws-ap-southeast-1) and empire-platform; the IaC does not
// carry the region, so after the first deploy: railway scale --service <svc> asia-southeast1-eqsg3a=1 us-east4-eqdc4a=0
export default defineRailway(() => {{
  const api = service("{name}-api", {{
    healthcheck: "/health",
    healthcheckTimeout: 60,
    deploy: {{ sleepApplication: true }},
  }});{publisher}

  return project("solo-{name}", {{
    variables: {{ managed: false }},
    resources: [{resources}],
  }});
}});
"""

PUBLISHER = """
  const publisher = service("{name}-publisher", {{
    startCommand: "{name}-publish",
    deploy: {{ cronSchedule: "*/15 * * * *", restartPolicyType: "NEVER" }},
  }});"""

PACKAGE = """{{
  "name": "solo-{name}-infra",
  "private": true,
  "description": "Railway Infrastructure as Code for the solo-{name} services (.railway/railway.ts)",
  "devDependencies": {{
    "railway": "^3.13.1"
  }}
}}
"""

GITIGNORE = ".venv/\n.runtime/\n__pycache__/\n*.egg-info/\n.env\n.env.*\n.pytest_cache/\nnode_modules/\n"
DOCKERIGNORE = ".venv\n.runtime\n.git\ntests\nnode_modules\n**/__pycache__\n*.egg-info\n.env\n.env.*\n"

AGENTS = """# Agent Instructions

- Human-facing replies are in Thai by default; keep code, commands and identifiers as-is.
- Never print, commit or copy secrets: database URLs, tokens and keys live in Railway
  variables and `~/.config/solo-{name}/` (owner-only).
- Tests use a throwaway local PostgreSQL and a fake platform; they must never reach
  the hosted database or the hosted platform.
- Run `.venv/bin/pytest -q` before committing. Migrations are append-only: never edit
  an applied file, add a new numbered one.
- `src/cellkit` is vendored from solo-cell-kit: change it there, then run
  `cellkit-new . --sync` here. Cell-specific code lives in `src/solo_{schema}/`.
- Deploys are manual (`railway up`) and change production; verify `/health` and one
  authorized read afterwards.
- Keep the solo-empire registry entry (`{name}` in
  `repository-catalog/registries/online-systems.yaml`) in sync.
"""

CONFTEST = '''"""Fixtures from the kit; the contract tests run against this cell's own cell.yaml."""

from pathlib import Path

import pytest

from cellkit.testing import client, harness, owner, platform  # noqa: F401


@pytest.fixture(scope="session")
def cell_root() -> Path:
    return Path(__file__).resolve().parents[1]
'''

CONTRACT = '''"""The kit's contract (CRUD, authorization, isolation, idempotency, imports, outbox) for every resource."""

from cellkit.contract import *  # noqa: F401,F403
'''


def readme(cell: Cell) -> str:
    rows = "\n".join(f"| `/v1/{r.plural}` | {r.name} | {', '.join(f'`{f}`' for f in r.filters) or '—'} |"
                     for r in cell.resources)
    env = cell.env
    publisher = (f"\n| Publisher | service `{cell.name}-publisher` (Railway cron, `*/15 * * * *`) |"
                 if cell.graph else "")
    return f"""# solo-{cell.name}

The {cell.title} cell: its own API (Railway, serverless) and its own Neon
database, made from `cell.yaml` with [solo-cell-kit](../solo-cell-kit). It
reuses empire-platform for identity, authorization, executions and audit over
HTTP with the caller's token.

| | |
|---|---|
| API | Railway project `solo-{cell.name}`, service `{cell.name}-api` (Serverless) |{publisher}
| Data | Neon project `solo-{cell.name}`, database `{cell.schema}` |
| Registry | solo-empire `repository-catalog/registries/online-systems.yaml` (`{cell.name}`) |

## API

Every resource has list, stats, get, get-by-legacy-id, create, patch and delete
(see `cellkit/app.py`). Permissions are `{cell.name}.<resource>:read|write|delete`.

| Path | Resource | Filters |
|---|---|---|
{rows}

## Develop and operate

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q
NEON_API_KEY=... .venv/bin/{cell.name}-db provision
npm install && railway config apply && railway up --ci --service {cell.name}-api
railway scale --service {cell.name}-api asia-southeast1-eqsg3a=1 us-east4-eqdc4a=0   # next to Neon and the platform
```

Railway variables: `{env}_DB_MODE=hosted`, `{env}_DB_URL_API`,
`{env}_JWT_PUBLIC_KEY_PEM`, `{env}_JWT_ISSUER`, `EMPIRE_PLATFORM_URL`{
', and for the publisher `' + env + '_DB_URL_PUBLISHER` and `' + env + '_PUBLISHER_TOKEN`' if cell.graph else ''}.
"""


def write(path: Path, text: str, *, keep: bool = False) -> None:
    if keep and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def generate(dest: Path, *, sync: bool = False) -> Cell:
    cell = load(dest / "cell.yaml")
    vendored = dest / "src" / "cellkit"
    if vendored.exists():
        shutil.rmtree(vendored)
    shutil.copytree(KIT, vendored, ignore=shutil.ignore_patterns("__pycache__", "new.py"))
    fill = {"name": cell.name, "title": cell.title, "schema": cell.schema}
    write(dest / "pyproject.toml", PYPROJECT.format(**fill))
    write(dest / "Dockerfile", DOCKERFILE.format(**fill))
    write(dest / ".railway" / "railway.ts", RAILWAY.format(
        **fill, publisher=PUBLISHER.format(**fill) if cell.graph else "",
        publisher_note=(f"\n// {cell.name}-publisher is a cron sweep of the outbox that exits after each run."
                        if cell.graph else ""),
        resources="api, publisher" if cell.graph else "api"))
    write(dest / "package.json", PACKAGE.format(**fill))
    write(dest / ".gitignore", GITIGNORE)
    write(dest / ".dockerignore", DOCKERIGNORE)
    write(dest / "AGENTS.md", AGENTS.format(**fill))
    write(dest / "CLAUDE.md", "@AGENTS.md\n")
    write(dest / "db" / "roles.sql", sqlgen.roles(cell))
    write(dest / "tests" / "conftest.py", CONFTEST, keep=True)
    write(dest / "tests" / "test_contract.py", CONTRACT, keep=True)
    write(dest / f"src/solo_{cell.schema}" / "__init__.py", "", keep=True)
    if not sync:
        write(dest / "README.md", readme(cell), keep=True)
        if not any((dest / "migrations").glob("*.sql")):
            write(dest / "migrations" / f"0001_{cell.schema}.sql", sqlgen.migration(cell))
    return cell


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cellkit-new", description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--sync", action="store_true", help="refresh the vendored kit and scaffolding only")
    args = parser.parse_args(argv)
    cell = generate(args.dest.resolve(), sync=args.sync)
    print(f"solo-{cell.name}: {len(cell.resources)} resources, permissions: {', '.join(cell.permissions())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
