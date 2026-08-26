import sqlite3

from backend.app.database import DATABASE_SCHEMA_REVISION, initialize


def test_initialize_creates_exact_database_schema_revision_one(tmp_path) -> None:
    database = tmp_path / "fresh.sqlite3"
    initialize(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(block_faces)")
        }
        unknown_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(unknown_block_faces)")
        }

    assert columns == {
        "block_face_id", "origin_block_face_id", "segment_id", "borough",
        "street_name", "side", "geometry_wkt",
    }
    assert not {"min_x", "min_y", "max_x", "max_y", "sample_address", "sample_latitude", "sample_longitude"} & columns
    assert not {"min_x", "min_y", "max_x", "max_y"} & unknown_columns
    assert not {"lookup_cache", "block_face_rtree_map", "block_faces_rtree"} & tables
    assert DATABASE_SCHEMA_REVISION == 1


def test_application_import_does_not_create_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    import backend.app.main  # noqa: F401

    assert not (tmp_path / "data").exists()
