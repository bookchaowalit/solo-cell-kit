"""A cell described as data: its resources and their fields (``cell.yaml``).

One spec drives everything the kit makes for a cell: the migration SQL, the
FastAPI routes and models, the permission names, the database roles and the
environment variable names. Names follow the platform's rules:

- cell and resource names are kebab-case (``roles-skills``, ``goal-task``);
- permissions are ``<cell>.<resource>:read|write|delete``;
- graph events are ``<cell>.<resource>.<verb>`` on ``empire:<cell>:<resource>:<id>``;
- SQL identifiers use the snake_case form (schema ``roles_skills``).

Example::

    cell: goals
    title: Goals
    resources:
      goal:
        table: goals
        plural: goals
        graph: [title, status]
        filters: [status, type]
        fields:
          title: {type: text, required: true, max_length: 500}
          status: {type: enum, values: [planning, active], default: planning}
          parent_id: {type: ref, to: goal, on_delete: set_null}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SNAKE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
TYPES = {"text", "int", "bigint", "real", "bool", "date", "timestamp", "json", "enum", "ref"}
RESERVED = {"id", "workspace_id", "legacy_id", "version", "created_at", "updated_at", "expected_version"}
LEGACY_LIMIT = 1_000_000  # imported rows keep their old id; new rows start above it


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    default: Any = None
    values: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    max_length: int | None = None
    to: str | None = None  # ref target resource (same cell)
    on_delete: str = "cascade"  # ref: cascade | set_null | restrict
    json_kind: str | None = None  # json: array | object
    unique: bool = False

    @property
    def nullable(self) -> bool:
        return not self.required and self.default is None


@dataclass(frozen=True)
class Resource:
    name: str
    table: str
    plural: str
    fields: tuple[Field, ...]
    filters: tuple[str, ...] = ()
    graph: tuple[str, ...] | None = None  # payload fields; None = no graph events
    unique: tuple[tuple[str, ...], ...] = ()
    order: str = "id"

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @property
    def select(self) -> str:
        return ", ".join(("id", "legacy_id", *self.columns, "version", "created_at", "updated_at"))

    @property
    def refs(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.type == "ref")


@dataclass(frozen=True)
class Cell:
    name: str
    title: str
    resources: tuple[Resource, ...]
    port: int = 3700
    extend: str | None = None  # "module:function" adding custom routes
    root: Path = field(default=Path("."), compare=False)

    @property
    def schema(self) -> str:
        return self.name.replace("-", "_")

    @property
    def env(self) -> str:
        """Prefix of this cell's environment variables (``ROLES_SKILLS`` for roles-skills)."""
        return self.schema.upper()

    @property
    def audience(self) -> str:
        return f"{self.name}-api"

    def role(self, kind: str) -> str:
        return f"{self.schema}_{kind}"

    @property
    def graph(self) -> bool:
        return any(r.graph is not None for r in self.resources)

    def resource(self, name: str) -> Resource:
        for r in self.resources:
            if r.name == name:
                return r
        raise KeyError(name)

    def permissions(self) -> tuple[str, ...]:
        out = [f"{self.name}.{r.name}:{right}" for r in self.resources for right in ("read", "write", "delete")]
        if self.graph:
            out.append(f"graph.{self.name}:write")
        return tuple(out)

    def ordered(self) -> tuple[Resource, ...]:
        """Resources with every ref target before the resource that points at it."""
        done: list[Resource] = []
        pending = list(self.resources)
        while pending:
            ready = [r for r in pending
                     if all(f.to == r.name or f.to in {d.name for d in done} for f in r.refs)]
            if not ready:
                raise SpecError("ref cycle between resources: " + ", ".join(r.name for r in pending))
            done.extend(ready)
            pending = [r for r in pending if r not in ready]
        return tuple(done)


def _field(name: str, raw: dict[str, Any], resource: str) -> Field:
    if not SNAKE.match(name) or name in RESERVED:
        raise SpecError(f"{resource}.{name}: invalid or reserved field name")
    kind = raw.get("type")
    if kind not in TYPES:
        raise SpecError(f"{resource}.{name}: unknown type {kind!r}")
    values = tuple(raw.get("values") or ())
    if kind == "enum" and not values:
        raise SpecError(f"{resource}.{name}: enum needs values")
    if kind == "ref" and not raw.get("to"):
        raise SpecError(f"{resource}.{name}: ref needs 'to'")
    on_delete = raw.get("on_delete", "cascade")
    if on_delete not in ("cascade", "set_null", "restrict"):
        raise SpecError(f"{resource}.{name}: on_delete must be cascade, set_null or restrict")
    if on_delete == "set_null" and raw.get("required"):
        raise SpecError(f"{resource}.{name}: a required ref cannot be set_null")
    if raw.get("default") is not None and kind == "enum" and raw["default"] not in values:
        raise SpecError(f"{resource}.{name}: default not in values")
    unknown = set(raw) - {"type", "required", "default", "values", "min", "max", "max_length", "to", "on_delete",
                          "json", "unique"}
    if unknown:
        raise SpecError(f"{resource}.{name}: unknown keys {sorted(unknown)}")
    return Field(name=name, type=kind, required=bool(raw.get("required")), default=raw.get("default"),
                 values=values, min=raw.get("min"), max=raw.get("max"), max_length=raw.get("max_length"),
                 to=raw.get("to"), on_delete=on_delete, json_kind=raw.get("json"), unique=bool(raw.get("unique")))


def parse(data: dict[str, Any], root: Path = Path(".")) -> Cell:
    name = data.get("cell", "")
    if not KEBAB.match(name):
        raise SpecError("cell must be kebab-case")
    resources = []
    plurals = set()
    for rname, raw in (data.get("resources") or {}).items():
        if not KEBAB.match(rname):
            raise SpecError(f"resource {rname!r} must be kebab-case")
        table = raw.get("table", rname.replace("-", "_") + "s")
        plural = raw.get("plural", rname + "s")
        if not SNAKE.match(table) or not KEBAB.match(plural) or plural in plurals:
            raise SpecError(f"{rname}: invalid or duplicate table/plural")
        plurals.add(plural)
        fields = tuple(_field(fname, fraw or {}, rname) for fname, fraw in (raw.get("fields") or {}).items())
        names = {f.name for f in fields}
        filters = tuple(raw.get("filters") or ())
        graph = raw.get("graph")
        unique = tuple(tuple(u) for u in raw.get("unique") or ())
        for listed in (*filters, *(graph or ()), *(c for u in unique for c in u)):
            if listed not in names:
                raise SpecError(f"{rname}: {listed!r} is not a field")
        resources.append(Resource(name=rname, table=table, plural=plural, fields=fields, filters=filters,
                                  graph=tuple(graph) if graph is not None else None, unique=unique,
                                  order=raw.get("order", "id")))
    if not resources:
        raise SpecError("a cell needs at least one resource")
    cell = Cell(name=name, title=data.get("title", name), resources=tuple(resources), port=int(data.get("port", 3700)),
                extend=data.get("extend"), root=root)
    known = {r.name for r in resources}
    for r in resources:
        for f in r.refs:
            if f.to not in known:
                raise SpecError(f"{r.name}.{f.name}: ref to unknown resource {f.to!r}")
        if r.order not in ("id", *r.columns):
            raise SpecError(f"{r.name}: order must be id or a field")
    cell.ordered()
    return cell


def load(path: Path) -> Cell:
    return parse(yaml.safe_load(path.read_text(encoding="utf-8")), path.parent)
