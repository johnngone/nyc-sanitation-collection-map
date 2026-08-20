import asyncio
import sqlite3

from backend.app.database import initialize
from backend.app import main as main_module


def test_initialize_additively_migrates_legacy_block_faces(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE block_faces (
                block_face_id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL,
                borough TEXT NOT NULL,
                street_name TEXT NOT NULL,
                side TEXT NOT NULL,
                geometry_wkt TEXT NOT NULL,
                min_x REAL NOT NULL,
                min_y REAL NOT NULL,
                max_x REAL NOT NULL,
                max_y REAL NOT NULL,
                sample_address TEXT,
                sample_latitude REAL,
                sample_longitude REAL
            );
            INSERT INTO block_faces (
                block_face_id, segment_id, borough, street_name, side,
                geometry_wkt, min_x, min_y, max_x, max_y
            ) VALUES (
                'legacy-face', 'legacy-segment', 'QUEENS', 'TEST STREET', 'LEFT',
                'LINESTRING (0 0, 1 1)', 0, 0, 1, 1
            );
            """
        )

    initialize(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(block_faces)")
        }
        assert "origin_block_face_id" in columns
        assert connection.execute(
            "SELECT origin_block_face_id FROM block_faces WHERE block_face_id = 'legacy-face'"
        ).fetchone() == ("legacy-face",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_block_face_origin'"
        ).fetchone() == (1,)


def test_app_startup_leaves_existing_legacy_database_untouched(tmp_path, monkeypatch) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('unchanged')")
    before = database.read_bytes()
    monkeypatch.setattr(main_module, "DATABASE_PATH", str(database))
    monkeypatch.setattr(main_module, "DATA_MANIFEST_PATH", str(tmp_path / "missing-manifest.json"))

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(run_lifespan())

    assert database.read_bytes() == before
