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
    assert report["board"] == {"rows": 2, "created": 2, "existing": 0, "linked": 0}  # the parent (9) went first
    child = client.get("/v1/boards/7").json()
    assert (child["legacy_id"], child["parent_id"], child["archived"], child["priority"]) == (7, 9, True, 3)
    assert child["created_at"].startswith("2025-12-01T10:00:00") and child["closed_at"].startswith("2026-01-02T03")
    assert child["updated_at"].startswith("2025-12-02T10:00:00")  # no second-pass patch overwrote it
    assert client.get("/v1/cards/3").json()["board_id"] == 7
    again = importer.run(cell, path, "http://test", "unused", client=client)
    assert again["board"]["existing"] == 2 and again["card"]["existing"] == 1


def test_timestamps_only_with_an_import(client, owner):
    response = client.post("/v1/boards", headers=owner, json={"title": "x", "created_at": "2020-01-01T00:00:00Z"})
    assert response.status_code == 422


def test_a_conflict_on_another_row_stops_the_import(tmp_path, harness, client, owner):
    import pytest
    path = tmp_path / "dup.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE boards (id INTEGER PRIMARY KEY, title TEXT);
        INSERT INTO boards VALUES (501, 'b');
        CREATE TABLE cards (id INTEGER PRIMARY KEY, board_id INTEGER, slug TEXT, done INTEGER);
        INSERT INTO cards VALUES (601, 501, 'same', 0);
        INSERT INTO cards VALUES (602, 501, 'same', 0);
    """)
    conn.commit()
    conn.close()
    client.headers.update(owner)
    with pytest.raises(SystemExit):
        importer.run(harness.cell, path, "http://test", "unused", client=client)


def test_the_api_url_must_be_https_or_loopback(tmp_path):
    import pytest
    (tmp_path / "t").write_text("x")
    with pytest.raises(SystemExit, match="https"):
        importer.main([str(tmp_path), "--sqlite", "x", "--api", "http://localhost.attacker.example",
                       "--token-file", str(tmp_path / "t")])
