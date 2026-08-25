import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS block_faces (
    block_face_id TEXT PRIMARY KEY,
    origin_block_face_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    borough TEXT NOT NULL,
    street_name TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LEFT', 'RIGHT')),
    geometry_wkt TEXT NOT NULL,
    min_x REAL NOT NULL,
    min_y REAL NOT NULL,
    max_x REAL NOT NULL,
    max_y REAL NOT NULL,
    sample_address TEXT,
    sample_latitude REAL,
    sample_longitude REAL
);

CREATE TABLE IF NOT EXISTS block_face_lion_components (
    block_face_id TEXT NOT NULL REFERENCES block_faces(block_face_id) ON DELETE CASCADE,
    component_index INTEGER NOT NULL CHECK (component_index >= 0),
    segment_id TEXT NOT NULL,
    source_side TEXT NOT NULL CHECK (source_side IN ('LEFT', 'RIGHT')),
    source_rows_json TEXT NOT NULL,
    source_indices_json TEXT NOT NULL,
    street_names_json TEXT NOT NULL,
    source_records_json TEXT NOT NULL,
    dsny_object_ids_json TEXT NOT NULL,
    PRIMARY KEY (block_face_id, component_index)
);

CREATE TABLE IF NOT EXISTS block_face_dsny_sources (
    block_face_id TEXT NOT NULL REFERENCES block_faces(block_face_id) ON DELETE CASCADE,
    dsny_object_id TEXT NOT NULL,
    frequency_row INTEGER,
    schedule_code TEXT,
    section TEXT,
    district TEXT,
    PRIMARY KEY (block_face_id, dsny_object_id)
);

CREATE TABLE IF NOT EXISTS collection_schedules (
    block_face_id TEXT NOT NULL REFERENCES block_faces(block_face_id),
    collection_type TEXT NOT NULL,
    weekday TEXT NOT NULL CHECK (weekday IN ('MON','TUE','WED','THU','FRI','SAT')),
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    PRIMARY KEY (block_face_id, collection_type, weekday)
);

CREATE TABLE IF NOT EXISTS block_face_collection_states (
    block_face_id TEXT NOT NULL REFERENCES block_faces(block_face_id) ON DELETE CASCADE,
    collection_type TEXT NOT NULL CHECK (collection_type IN ('REFUSE','RECYCLING','ORGANICS','BULK')),
    effective_days_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('SOURCE_EXPLICIT','POLICY_DERIVED','UNKNOWN_SOURCE_BLANK','NO_SERVICE')),
    source_field TEXT NOT NULL,
    raw_value TEXT,
    rule_id TEXT,
    source_policy_conflict INTEGER NOT NULL DEFAULT 0 CHECK (source_policy_conflict IN (0,1)),
    provenance TEXT NOT NULL,
    PRIMARY KEY (block_face_id, collection_type)
);

CREATE TABLE IF NOT EXISTS unknown_block_faces (
    unknown_id TEXT PRIMARY KEY,
    technical_identity TEXT,
    segment_id TEXT NOT NULL,
    borough TEXT,
    street_name TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LEFT', 'RIGHT')),
    reason_code TEXT NOT NULL CHECK (reason_code IN ('INSUFFICIENT_ADDRESS_EVIDENCE','OUTSIDE_DSNY_COVERAGE','PARTIAL_GEOMETRY_GAP')),
    reason TEXT NOT NULL,
    identity_method TEXT NOT NULL,
    geometry_method TEXT NOT NULL,
    geometry_wkt TEXT NOT NULL,
    min_x REAL NOT NULL,
    min_y REAL NOT NULL,
    max_x REAL NOT NULL,
    max_y REAL NOT NULL,
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lookup_cache (
    lookup_key TEXT PRIMARY KEY,
    input_address TEXT NOT NULL,
    borough TEXT,
    bin TEXT,
    bbl TEXT,
    latitude REAL,
    longitude REAL,
    refuse_days_json TEXT,
    raw_response_json TEXT,
    http_status INTEGER,
    lookup_status TEXT NOT NULL,
    error_message TEXT,
    queried_at TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS block_face_rtree_map (
    rtree_id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_face_id TEXT NOT NULL UNIQUE REFERENCES block_faces(block_face_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS block_faces_rtree USING rtree(
    rtree_id,
    min_x, max_x,
    min_y, max_y
);

"""

SECONDARY_INDEXES = {
    "idx_schedule_day_type": "ON collection_schedules(collection_type, weekday)",
    "idx_schedule_type_day_face": "ON collection_schedules(collection_type, weekday, block_face_id)",
    "idx_collection_state_type": "ON block_face_collection_states(collection_type, state)",
    "idx_unknown_reason": "ON unknown_block_faces(reason_code)",
    "idx_block_face_bbox": "ON block_faces(min_x, max_x, min_y, max_y)",
    "idx_block_face_min_x": "ON block_faces(min_x)",
    "idx_block_face_max_x": "ON block_faces(max_x)",
    "idx_block_face_min_y": "ON block_faces(min_y)",
    "idx_block_face_max_y": "ON block_faces(max_y)",
    "idx_block_face_borough": "ON block_faces(borough)",
    "idx_block_face_origin": "ON block_faces(origin_block_face_id)",
    "idx_dsny_source_object": "ON block_face_dsny_sources(dsny_object_id)",
}


def create_secondary_indexes(connection: sqlite3.Connection) -> None:
    for name, definition in SECONDARY_INDEXES.items():
        connection.execute(f"CREATE INDEX IF NOT EXISTS {name} {definition}")


def drop_secondary_indexes(connection: sqlite3.Connection) -> None:
    for name in SECONDARY_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {name}")


def connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(database_path: str | Path, *, create_indexes: bool = True) -> None:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(database_path)) as connection:
        connection.executescript(SCHEMA)
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(block_faces)")
        }
        if "origin_block_face_id" not in columns:
            connection.execute("ALTER TABLE block_faces ADD COLUMN origin_block_face_id TEXT")
            connection.execute(
                "UPDATE block_faces SET origin_block_face_id = block_face_id "
                "WHERE origin_block_face_id IS NULL"
            )
        if create_indexes:
            create_secondary_indexes(connection)
        connection.commit()


def iter_rows(connection: sqlite3.Connection, query: str, parameters: tuple[object, ...] = ()) -> Iterator[sqlite3.Row]:
    yield from connection.execute(query, parameters)
