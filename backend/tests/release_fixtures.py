import hashlib
import json
from pathlib import Path


VERIFIED_AT = "2026-09-15T20:34:00+00:00"
PROCESSED_AT = "2026-08-19T12:00:00Z"


def artifact_descriptor(path: Path, **values: object) -> dict[str, object]:
    return {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        **values,
    }


def write_runtime_pointer(
    data_dir: Path,
    archive: Path,
    *,
    version: str = "dataset-v1",
    verified_at: str = VERIFIED_AT,
) -> Path:
    release = data_dir / "releases" / version
    release.mkdir(parents=True)
    tileset = release / "collection_streets.mbtiles"
    tileset.write_bytes(archive.read_bytes())
    database = release / "app.sqlite3"
    database.write_bytes(b"runtime-binding")
    pointer = data_dir / "data_manifest.json"
    pointer.write_text(
        json.dumps(
            {
                "manifest_version": 4,
                "dataset_version": version,
                "release_path": f"releases/{version}",
                "processed_at": PROCESSED_AT,
                "verified_at": verified_at,
                "artifacts": {
                    "database": artifact_descriptor(
                        database,
                        database_schema_revision=1,
                    ),
                    "tileset": artifact_descriptor(
                        tileset,
                        tile_schema_revision=4,
                    ),
                },
                "database": {"database_schema_revision": 1},
                "tileset": {"tile_schema_revision": 4},
                "previous_releases": [],
            }
        ),
        encoding="utf-8",
    )
    return pointer
