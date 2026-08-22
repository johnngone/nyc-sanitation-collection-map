"""Read-only access to the generated collection-street MBTiles archive."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


SOURCE_LAYER = "collection_streets"
UNKNOWN_SOURCE_LAYER = "collection_unknowns"
VECTOR_TILE_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class TilesetMetadata:
    version: str
    tile_schema_revision: int
    source_layer: str
    unknown_source_layer: str | None
    unknown_minzoom: int | None
    minzoom: int
    maxzoom: int
    bounds: tuple[float, float, float, float]
    data_updated: str | None


def _read_only_connection(path: Path) -> sqlite3.Connection:
    # A new immutable connection is intentionally opened for each tile request.
    # Refreshes publish the archive with an atomic rename, so a request either
    # sees the complete old file or the complete new file and never a partial DB.
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


@lru_cache(maxsize=8)
def _cached_metadata(path_string: str, mtime_ns: int, size: int) -> TilesetMetadata:
    del mtime_ns, size  # They form the cache key and invalidate an atomic refresh.
    path = Path(path_string)
    with closing(_read_only_connection(path)) as connection:
        values = dict(connection.execute("SELECT name, value FROM metadata"))

    if values.get("format") != "pbf":
        raise ValueError("tileset format must be pbf")
    version = values.get("version", "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("tileset metadata has an invalid version")
    try:
        minzoom = int(values["minzoom"])
        maxzoom = int(values["maxzoom"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("tileset metadata has invalid zoom bounds") from error
    if not 0 <= minzoom <= maxzoom <= 30:
        raise ValueError("tileset zoom bounds are outside the supported range")
    raw_bounds = values.get("bounds", "").split(",")
    try:
        bounds = tuple(float(value) for value in raw_bounds)
    except (TypeError, ValueError) as error:
        raise ValueError("tileset metadata has invalid geographic bounds") from error
    if len(bounds) != 4:
        raise ValueError("tileset metadata has invalid geographic bounds")
    west, south, east, north = bounds
    if (
        not all(math.isfinite(value) for value in bounds)
        or not -180 <= west < east <= 180
        or not -90 <= south < north <= 90
    ):
        raise ValueError("tileset metadata has invalid geographic bounds")
    try:
        tile_schema_revision = int(values.get("tile_schema_revision", "1"))
    except (TypeError, ValueError) as error:
        raise ValueError("tileset metadata has an invalid schema revision") from error
    if tile_schema_revision <= 0:
        raise ValueError("tileset metadata has an invalid schema revision")

    source_layer = values.get("source_layer", SOURCE_LAYER)
    vector_metadata = values.get("json")
    layer_ids: set[object] = set()
    if vector_metadata:
        try:
            layers = json.loads(vector_metadata).get("vector_layers", [])
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("tileset vector layer metadata is invalid") from error
        layer_ids = {layer.get("id") for layer in layers if isinstance(layer, dict)}
        if source_layer not in layer_ids:
            raise ValueError("tileset metadata does not describe its source layer")
    if source_layer != SOURCE_LAYER:
        raise ValueError(f"tileset source layer must be {SOURCE_LAYER}")
    unknown_source_layer = values.get("unknown_source_layer") or None
    unknown_minzoom: int | None = None
    if tile_schema_revision >= 3:
        if unknown_source_layer != UNKNOWN_SOURCE_LAYER or unknown_source_layer not in layer_ids:
            raise ValueError("v3 tileset metadata does not describe its unknown source layer")
        try:
            unknown_minzoom = int(values["unknown_minzoom"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("v3 tileset metadata has invalid unknown minimum zoom") from error
        if not minzoom <= unknown_minzoom <= maxzoom:
            raise ValueError("v3 unknown minimum zoom is outside tileset bounds")

    return TilesetMetadata(
        version=version,
        tile_schema_revision=tile_schema_revision,
        source_layer=source_layer,
        unknown_source_layer=unknown_source_layer,
        unknown_minzoom=unknown_minzoom,
        minzoom=minzoom,
        maxzoom=maxzoom,
        bounds=(west, south, east, north),
        data_updated=values.get("data_updated") or None,
    )


def read_metadata(path_value: str | Path) -> TilesetMetadata:
    path = Path(path_value)
    stat = path.stat()
    if not path.is_file() or stat.st_size == 0:
        raise FileNotFoundError(path)
    return _cached_metadata(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def read_tile(path_value: str | Path, z: int, x: int, xyz_y: int) -> bytes | None:
    path = Path(path_value)
    tms_y = (1 << z) - 1 - xyz_y
    with closing(_read_only_connection(path)) as connection:
        row = connection.execute(
            """SELECT tile_data FROM tiles
               WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?""",
            (z, x, tms_y),
        ).fetchone()
    return bytes(row[0]) if row is not None else None
