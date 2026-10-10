"""The importer: old ids, timestamps, parents set in a second pass, value rewrites, reruns, verify counts."""

import sqlite3

from cellkit import importer
from cellkit.spec import Resource


def legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE boards (id INTEGER PRIMARY KEY, title TEXT, status TEXT, archived INTEGER, parent_id INTEGER,
                             closed_at TEXT, priority TEXT, created_at TEXT, updated_at TEXT);
        INSERT INTO boards VALUES (7, 'child', 'active', 1, 9, '2026-01-02 03:04:05', 'low',
                                   '2025-12-01 10:00:00', '2025-12-02 10:00:00');
        INSERT INTO boards VALUES (9, 'parent', 'planning', 0, NULL, NULL, 2, '2025-11-01 10:00:00', NULL);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, board_id INTEGER, slug TEXT, done INTEGER);
        INSERT INTO cards VALUES (3, 7, 'c', 1);
    """)
    conn.commit()
    conn.close()


def test_import_keeps_ids_parents_stamps_and_reruns(tmp_path, harness, client, owner, monkeypatch):
    path = tmp_path / "old.db"
    legacy_db(path)
    board = harness.cell.resource("board")
    rewritten = Resource(**{**board.__dict__, "legacy_values": (("priority", (("low", 3),)),)})
    cell = type(harness.cell)(**{**harness.cell.__dict__, "resources": (rewritten, harness.cell.resource("card"))})
    client.headers.update(owner)
    report = importer.run(cell, path, "http://test", "unused", client=client)
    assert report["board"] == {"rows": 2, "created": 2, "existing": 0, "linked": 1}
    child = client.get("/v1/boards/7").json()
    assert (child["legacy_id"], child["parent_id"], child["archived"], child["priority"]) == (7, 9, True, 3)
    assert child["created_at"].startswith("2025-12-01T10:00:00") and child["closed_at"].startswith("2026-01-02T03")
    assert client.get("/v1/cards/3").json()["board_id"] == 7
    again = importer.run(cell, path, "http://test", "unused", client=client)
    assert again["board"]["existing"] == 2 and again["card"]["existing"] == 1


def test_timestamps_only_with_an_import(client, owner):
    response = client.post("/v1/boards", headers=owner, json={"title": "x", "created_at": "2020-01-01T00:00:00Z"})
    assert response.status_code == 422
