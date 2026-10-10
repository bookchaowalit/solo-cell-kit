"""Database access for a cell: a throwaway local cluster for tests, or its hosted Neon database.

Roles: ``<cell>_migrator`` (owns the schema, runs migrations), ``<cell>_api``
(runtime, RLS-bound), ``<cell>_publisher`` (outbox sweep).

Hosted connection URLs come from ``<CELL>_DB_URL_<ROLE>`` (Railway variables)
or the owner-only file ``~/.config/solo-<cell>/db.env``; nothing secret is
printed or stored in the repository.

    <cell>-db migrate        apply pending migrations as the migrator
    <cell>-db provision      create the Neon project and roles (NEON_API_KEY), then migrate
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

from cellkit.spec import Cell

SUPERUSER = "postgres"
PORT = 5434
NEON_API = "https://console.neon.tech/api/v2"


@dataclass(frozen=True)
class Names:
    """Everything about a cell's database that follows from its name."""

    cell: Cell

    @property
    def database(self) -> str:
        return self.cell.schema

    @property
    def migrator(self) -> str:
        return self.cell.role("migrator")

    @property
    def runtime_roles(self) -> tuple[str, str]:
        return self.cell.role("api"), self.cell.role("publisher")

    @property
    def migrations(self) -> Path:
        return self.cell.root / "migrations"

    @property
    def roles_sql(self) -> Path:
        return self.cell.root / "db" / "roles.sql"

    @property
    def secrets_file(self) -> Path:
        override = os.environ.get(f"{self.cell.env}_DB_SECRETS")
        return Path(override) if override else Path.home() / ".config" / f"solo-{self.cell.name}" / "db.env"

    def short(self, role: str) -> str:
        return {self.migrator: "migrator"}.get(role, role.removeprefix(f"{self.cell.schema}_"))


def pg_bin_dir(env: str) -> Path:
    override = os.environ.get(f"{env}_PG_BIN")
    if override:
        return Path(override)
    try:
        import pgserver  # type: ignore[import-untyped]

        bundled = Path(pgserver.__file__).parent / "pginstall" / "bin"
        if (bundled / "pg_ctl").exists():
            return bundled
    except ImportError:
        pass
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    raise SystemExit(f"PostgreSQL binaries not found; install the dev extra or set {env}_PG_BIN")


@dataclass
class LocalCluster:
    """A throwaway PostgreSQL for tests (trust auth over a private socket)."""

    names: Names
    data_dir: Path
    socket_dir: Path

    def _run(self, *args: str) -> None:
        subprocess.run([str(pg_bin_dir(self.names.cell.env) / args[0]), *args[1:]], check=True, capture_output=True,
                       text=True)

    def init(self) -> None:
        self._run("initdb", "-D", str(self.data_dir), "-U", SUPERUSER, "--auth=trust", "--encoding=UTF8", "--locale=C")

    def start(self) -> None:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        options = f"-c listen_addresses='' -k {self.socket_dir} -p {PORT}"
        self._run("pg_ctl", "-D", str(self.data_dir), "-o", options, "-w", "-l", str(self.data_dir / "server.log"),
                  "start")

    def stop(self) -> None:
        if (self.data_dir / "postmaster.pid").exists():
            self._run("pg_ctl", "-D", str(self.data_dir), "-m", "fast", "-w", "stop")

    def connect(self, role: str, dbname: str | None = None, **kwargs) -> psycopg.Connection:
        return psycopg.connect(f"host={self.socket_dir} port={PORT} user={role} dbname={dbname or self.names.database}",
                               **kwargs)

    def bootstrap(self) -> None:
        n = self.names
        with self.connect(SUPERUSER, "postgres", autocommit=True) as conn:
            conn.execute(n.roles_sql.read_text(encoding="utf-8"))
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (n.database,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(n.database),
                                                                          sql.Identifier(n.migrator)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(n.database), sql.SQL(", ").join(map(sql.Identifier, n.runtime_roles))))


@dataclass
class HostedCluster:
    """Neon: one URL per role, from the environment or the owner-only secrets file."""

    names: Names
    urls: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, names: Names) -> "HostedCluster":
        prefix = f"{names.cell.env}_DB_URL_"
        urls: dict[str, str] = {}
        path = names.secrets_file
        if path.is_file():
            if path.stat().st_mode & 0o077:
                raise SystemExit(f"{path} must be readable by its owner only (chmod 600)")
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key.startswith(prefix):
                    urls[key.removeprefix(prefix).lower()] = value.strip()
        for key, value in os.environ.items():
            if key.startswith(prefix) and value:
                urls[key.removeprefix(prefix).lower()] = value
        return cls(names, urls)

    def connect(self, role: str, dbname: str | None = None, **kwargs) -> psycopg.Connection:
        key = self.names.short(role)
        if key not in self.urls:
            raise SystemExit(f"no connection URL for {role}: set {self.names.cell.env}_DB_URL_{key.upper()}")
        # Short connect timeout; Neon wakes a suspended compute within a few seconds.
        return psycopg.connect(self.urls[key], connect_timeout=15, **kwargs)


def default_cluster(cell: Cell) -> LocalCluster | HostedCluster:
    names = Names(cell)
    if os.environ.get(f"{cell.env}_DB_MODE") == "hosted":
        return HostedCluster.from_env(names)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    return LocalCluster(names, cell.root / ".runtime" / "pgdata", runtime / f"solo-{cell.name}")


def migrate(cluster: LocalCluster | HostedCluster) -> list[str]:
    """Apply pending migrations as the migrator; refuse edited ones."""
    names = cluster.names
    applied: list[str] = []
    with cluster.connect(names.migrator) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations ("
                     " name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        conn.commit()
        done = dict(conn.execute("SELECT name, sha256 FROM public.schema_migrations").fetchall())
        files = sorted(names.migrations.glob("*.sql"))
        if not files:
            raise SystemExit(f"no migrations found in {names.migrations}")
        for path in files:
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise SystemExit(f"applied migration was edited: {path.name}")
                continue
            with conn.transaction():
                conn.execute(text)
                conn.execute("INSERT INTO public.schema_migrations (name, sha256) VALUES (%s, %s)", (path.name, digest))
            applied.append(path.name)
    return applied


# --- Neon provisioning -------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # never forward the API key
        return None


def _neon(key: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(f"{NEON_API}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                              "Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    for attempt in range(15):
        try:
            with opener.open(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if error.code == 423 and attempt < 14:  # a running operation holds the project
                time.sleep(2)
                continue
            raise SystemExit(f"neon {method} {path.split('?')[0]}: {error.code}") from None
    raise SystemExit("neon project stayed locked")


def _url(host: str, database: str, role: str, password: str) -> str:
    return (f"postgresql://{urllib.parse.quote(role)}:{urllib.parse.quote(password, safe='')}@{host}/{database}"
            "?sslmode=require&channel_binding=require")


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def provision(cell: Cell, project_name: str, region: str) -> str:
    """Create or reuse the Neon project, database and roles; write URLs owner-only. Returns the project id."""
    names = Names(cell)
    key = os.environ.get("NEON_API_KEY")
    if not key:
        raise SystemExit("NEON_API_KEY is not set (inject it from the secret store)")
    found = [p for p in _neon(key, "GET", f"/projects?search={urllib.parse.quote(project_name)}").get("projects", [])
             if p["name"] == project_name]
    if found:
        project = found[0]["id"]
    else:
        created = _neon(key, "POST", "/projects", {"project": {"name": project_name, "region_id": region,
                                                               "pg_version": 16}})
        project = created["project"]["id"]
        time.sleep(5)
    branch = next(b for b in _neon(key, "GET", f"/projects/{project}/branches")["branches"]
                  if b.get("default") or b.get("primary"))["id"]
    host = next(e for e in _neon(key, "GET", f"/projects/{project}/endpoints")["endpoints"]
                if e["branch_id"] == branch and e["type"] == "read_write")["host"]
    base = f"/projects/{project}/branches/{branch}"
    if names.migrator not in {r["name"] for r in _neon(key, "GET", f"{base}/roles")["roles"]}:
        _neon(key, "POST", f"{base}/roles", {"role": {"name": names.migrator}})
        time.sleep(3)
    password = _neon(key, "GET", f"{base}/roles/{names.migrator}/reveal_password")["password"]
    if names.database not in {d["name"] for d in _neon(key, "GET", f"{base}/databases")["databases"]}:
        _neon(key, "POST", f"{base}/databases", {"database": {"name": names.database, "owner_name": names.migrator}})
        time.sleep(3)
    existing = HostedCluster.from_env(names).urls
    urls = {"migrator": _url(host, names.database, names.migrator, password)}
    with psycopg.connect(urls["migrator"], autocommit=True, connect_timeout=30) as conn:
        for role in names.runtime_roles:
            short = names.short(role)
            known = existing.get(short)
            role_password = urllib.parse.unquote(urllib.parse.urlsplit(known).password or "") if known else ""
            role_password = role_password or secrets.token_urlsafe(32)
            present = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            conn.execute(sql.SQL(
                "{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD {}").format(
                    sql.SQL("ALTER" if present else "CREATE"), sql.Identifier(role), sql.Literal(role_password)))
            urls[short] = _url(host, names.database, role, role_password)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(names.database)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(names.database), sql.SQL(", ").join(map(sql.Identifier, names.runtime_roles))))
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    lines = [f"# solo-{cell.name} hosted database; owner-only, never commit or print\n"]
    lines += [f"{cell.env}_DB_URL_{name.upper()}={value}\n" for name, value in sorted(urls.items())]
    _write_private(names.secrets_file, "".join(lines))
    migrate(HostedCluster(names, urls))
    return project
