"""Import a cell's rows from the old SQLite database through the cell's own API.

    cellkit-import <cell-dir> --sqlite PATH --api URL --token-file PATH [--dry-run]

Every row is created with ``legacy_id`` (so it keeps its old id; references from
other rows and other cells stay valid) and its old timestamps. Resources go in
dependency order, and rows of one resource go parents first, so a ref to a row
of the same resource is part of the create (only a cycle is set in a second
pass). A row that already exists (its legacy id answers) is counted, not
changed, so a rerun continues where a broken one stopped; any other conflict
stops the import. Values
are converted to the field types (SQLite 0/1 to bool, "YYYY-MM-DD HH:MM:SS" to
UTC timestamps) and rewritten by the resource's ``legacy_values``.

The token is read from a file and never printed. Prints counts per resource.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from cellkit.spec import Cell, Field, Resource, load


def convert(f: Field, value: Any) -> Any:
    if value is None:
        return None
    if f.type == "bool":
        return bool(int(value)) if isinstance(value, (int, str)) and str(value) in ("0", "1") else bool(value)
    if f.type == "timestamp":
        text = str(value).replace("T", " ")
        parsed = datetime.fromisoformat(text)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()
    if f.type == "date":
        return str(value)[:10]
    if f.type == "json" and isinstance(value, str):
        return json.loads(value)
    if f.type in ("int", "bigint", "ref"):
        return int(value)
    if f.type == "real":
        return float(value)
    return value


def stamp(value: Any) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("T", " "))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def rows_of(conn: sqlite3.Connection, r: Resource) -> list[dict[str, Any]]:
    table = r.legacy_table or r.table
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY "{r.legacy_id_from}"')]


def parents_first(r: Resource, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows ordered so a row comes after the rows of the same resource it points at (cycles keep their order)."""
    self_refs = [f.name for f in r.refs if f.to == r.name]
    if not self_refs:
        return rows
    by_id = {int(row[r.legacy_id_from]): row for row in rows}
    done: set[int] = set()
    out: list[dict[str, Any]] = []

    def visit(row_id: int, path: set[int]) -> None:
        if row_id in done or row_id in path or row_id not in by_id:
            return
        row = by_id[row_id]
        for name in self_refs:
            if row.get(name) is not None:
                visit(int(row[name]), path | {row_id})
        done.add(row_id)
        out.append(row)

    for row_id in by_id:
        visit(row_id, set())
    return out


def body_of(r: Resource, row: dict[str, Any], created: set[int] | None = None
            ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(create body, self-refs for the second pass: only those whose target is not created yet)."""
    rewrites = {name: dict(mapping) for name, mapping in r.legacy_values}
    out: dict[str, Any] = {"legacy_id": int(row[r.legacy_id_from])}
    later: dict[str, Any] = {}
    for f in r.fields:
        if f.name not in row:
            continue
        value = row[f.name]
        if f.name in rewrites and value in rewrites[f.name]:
            value = rewrites[f.name][value]
        value = convert(f, value)
        if value is None:
            continue
        if f.type == "ref" and f.to == r.name and value not in (created or set()):
            later[f.name] = value
        else:
            out[f.name] = value
    for name in ("created_at", "updated_at"):
        if row.get(name):
            out[name] = stamp(row[name])
    return out, later


def run(cell: Cell, sqlite_path: Path, api: str, token: str, *, dry_run: bool = False,
        client: httpx.Client | None = None) -> dict[str, dict[str, int]]:
    http = client or httpx.Client(base_url=api.rstrip("/"), timeout=60, follow_redirects=False,
                                  headers={"Authorization": f"Bearer {token}", "X-Empire-Channel": "cli"})
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    report: dict[str, dict[str, int]] = {}
    for r in cell.ordered():
        counts = {"rows": 0, "created": 0, "existing": 0, "linked": 0}
        second: list[tuple[int, dict[str, Any]]] = []
        created: set[int] = set()
        for row in parents_first(r, rows_of(conn, r)):
            counts["rows"] += 1
            payload, later = body_of(r, row, created)
            created.add(payload["legacy_id"])
            if later:
                second.append((payload["legacy_id"], later))
            if dry_run:
                continue
            response = http.post(f"/v1/{r.plural}", json=payload)
            if response.status_code == 201:
                counts["created"] += 1
            elif response.status_code == 409 and http.get(f"/v1/{r.plural}/legacy/{payload['legacy_id']}"
                                                           ).status_code == 200:
                counts["existing"] += 1
            else:
                raise SystemExit(f"{r.name} legacy_id={payload['legacy_id']}: HTTP {response.status_code}"
                                 f" {response.text[:300]}")
        for legacy_id, refs in second:
            if dry_run:
                continue
            response = http.patch(f"/v1/{r.plural}/{legacy_id}", json=refs)
            if response.status_code != 200:
                raise SystemExit(f"{r.name} {legacy_id} refs: HTTP {response.status_code} {response.text[:300]}")
            counts["linked"] += 1
        report[r.name] = counts
    return report


def verify(cell: Cell, sqlite_path: Path, api: str, token: str) -> dict[str, dict[str, int]]:
    """Old rows vs rows in the cell, per resource, by legacy id."""
    http = httpx.Client(base_url=api.rstrip("/"), timeout=60, follow_redirects=False,
                        headers={"Authorization": f"Bearer {token}", "X-Empire-Channel": "cli"})
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    out = {}
    for r in cell.resources:
        old = {int(row[r.legacy_id_from]) for row in rows_of(conn, r)}
        new: set[int] = set()
        after = 0
        while True:
            page = http.get(f"/v1/{r.plural}", params={"limit": 1000, "after_id": after}).json()["items"]
            new |= {item["legacy_id"] for item in page if item["legacy_id"] is not None}
            if len(page) < 1000:
                break
            after = page[-1]["id"]
        out[r.name] = {"old": len(old), "imported": len(old & new), "missing": len(old - new)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cellkit-import", description=__doc__.splitlines()[0])
    parser.add_argument("cell_dir", type=Path)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true", help="only compare old and imported rows")
    args = parser.parse_args(argv)
    from urllib.parse import urlsplit

    parts = urlsplit(args.api)
    if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "::1", "localhost"))):
        raise SystemExit("--api must be https (http only on loopback)")
    cell = load(args.cell_dir / "cell.yaml")
    token = args.token_file.read_text(encoding="utf-8").strip()
    if args.verify:
        report = verify(cell, args.sqlite, args.api, token)
        print(json.dumps(report, indent=1))
        return 1 if any(v["missing"] for v in report.values()) else 0
    print(json.dumps(run(cell, args.sqlite, args.api, token, dry_run=args.dry_run), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
