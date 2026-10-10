"""Entry points shared by every cell: ``<cell>-api``, ``<cell>-publish`` and ``<cell>-db``.

The cell is found from ``cell.yaml`` in the application root: ``<CELL>_APP_ROOT``,
else ``CELL_APP_ROOT``, else the working directory (``/app`` in the image).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cellkit.spec import Cell, load


def cell() -> Cell:
    root = Path(os.environ.get("CELL_APP_ROOT") or Path.cwd())
    return load(root / "cell.yaml")


def api() -> None:
    import uvicorn

    from cellkit.app import create_app
    from cellkit.auth import TokenVerifier
    from cellkit.db import default_cluster
    from cellkit.platform import HttpPlatform

    spec = cell()
    env = spec.env
    hosted = os.environ.get(f"{env}_DB_MODE") == "hosted"
    pem = os.environ.get(f"{env}_JWT_PUBLIC_KEY_PEM", "").strip()
    issuer = os.environ.get(f"{env}_JWT_ISSUER", "")
    platform_url = os.environ.get("EMPIRE_PLATFORM_URL", "")
    if not pem or not issuer or not platform_url:
        raise SystemExit(f"set {env}_JWT_PUBLIC_KEY_PEM, {env}_JWT_ISSUER and EMPIRE_PLATFORM_URL")
    if hosted and issuer == "empire-platform-dev":
        raise SystemExit("hosted mode refuses the dev issuer")
    cluster = default_cluster(spec)
    app = create_app(spec, lambda: cluster.connect(spec.role("api")), TokenVerifier(pem, issuer, spec.audience),
                     HttpPlatform(platform_url),
                     publisher_token=lambda: os.environ.get(f"{env}_PUBLISHER_TOKEN") or None)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0" if hosted else "127.0.0.1"),
                port=int(os.environ.get("PORT", str(spec.port))), proxy_headers=False)


def publish(argv: list[str] | None = None) -> int:
    from cellkit.db import default_cluster
    from cellkit.platform import HttpPlatform
    from cellkit.publisher import publish_pending

    spec = cell()
    parser = argparse.ArgumentParser(prog=f"{spec.name}-publish", description="sweep the outbox to the platform graph")
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args(argv)
    token = os.environ.get(f"{spec.env}_PUBLISHER_TOKEN")
    url = os.environ.get("EMPIRE_PLATFORM_URL")
    if not token or not url:
        print(f"set {spec.env}_PUBLISHER_TOKEN and EMPIRE_PLATFORM_URL", file=sys.stderr)
        return 2
    with default_cluster(spec).connect(spec.role("publisher")) as conn:
        counts = publish_pending(conn, spec.schema, HttpPlatform(url), token, limit=args.batch)
    print(json.dumps({"event": f"{spec.name}.publish", **counts}))
    return 1 if counts["failed"] else 0


def db(argv: list[str] | None = None) -> int:
    from cellkit.db import Names, default_cluster, migrate, provision

    spec = cell()
    parser = argparse.ArgumentParser(prog=f"{spec.name}-db", description="migrate or provision the cell database")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    prov = sub.add_parser("provision")
    prov.add_argument("--project", default=f"solo-{spec.name}")
    prov.add_argument("--region", default="aws-ap-southeast-1")
    args = parser.parse_args(argv)
    if args.command == "provision":
        project = provision(spec, args.project, args.region)
        print(f"neon project {project} ready; URLs in {Names(spec).secrets_file} (owner-only)")
        return 0
    applied = migrate(default_cluster(spec))
    print(f"{len(applied)} migrations applied")
    return 0


def run_publish() -> None:
    sys.exit(publish())


def run_db() -> None:
    sys.exit(db())
