"""Resolve committed release artifacts from the atomic dataset manifest.

The manifest is the sole commit pointer.  A directory under ``data/releases``
is not live merely because it exists; it must be named by the current manifest
or its retained-release list.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath


MANIFEST_VERSION = 3
SUPPORTED_MANIFEST_VERSIONS = {2, 3}
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReleaseManifestError(RuntimeError):
    """The committed manifest exists but cannot be trusted or resolved."""


@dataclass(frozen=True)
class TilesetRelease:
    dataset_version: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class CurrentRelease:
    dataset_version: str
    database_path: Path
    database_sha256: str
    tileset_path: Path
    tileset_sha256: str
    previous_tilesets: tuple[TilesetRelease, ...]
    manifest: dict[str, object]


def read_current_release(manifest_path: str | Path) -> CurrentRelease | None:
    """Return a committed v2/v3 release, or ``None`` for legacy/no manifest.

    A malformed v2 manifest fails closed.  Falling back in that case could
    silently combine a legacy database with tiles from a different release.
    """

    pointer = Path(manifest_path)
    try:
        raw = pointer.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ReleaseManifestError(f"could not read dataset manifest: {pointer}") from error
    try:
        manifest = json.loads(raw)
    except ValueError as error:
        raise ReleaseManifestError(f"dataset manifest is invalid JSON: {pointer}") from error
    if not isinstance(manifest, dict):
        raise ReleaseManifestError("dataset manifest must be a JSON object")
    manifest_version = manifest.get("manifest_version")
    if type(manifest_version) is int and manifest_version == 1:
        return None
    if type(manifest_version) is not int or manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ReleaseManifestError("dataset manifest version is unsupported or missing")

    has_release_path = "release_path" in manifest
    has_artifacts = "artifacts" in manifest
    if not has_release_path or not has_artifacts:
        raise ReleaseManifestError("dataset manifest has incomplete release fields")

    dataset_version = _version(manifest.get("dataset_version"), "dataset_version")
    release_root = _release_root(pointer.parent, manifest.get("release_path"), dataset_version)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ReleaseManifestError("dataset manifest is missing artifacts")
    database_path, database_hash = _artifact(
        release_root, artifacts, "database", "app.sqlite3"
    )
    tileset_path, tileset_hash = _artifact(
        release_root, artifacts, "tileset", "collection_streets.mbtiles"
    )

    previous_tilesets: list[TilesetRelease] = []
    previous_versions: set[str] = set()
    raw_previous = manifest.get("previous_releases", [])
    if not isinstance(raw_previous, list):
        raise ReleaseManifestError("previous_releases must be an array")
    for index, previous in enumerate(raw_previous):
        if not isinstance(previous, dict):
            raise ReleaseManifestError(f"previous_releases[{index}] must be an object")
        version = _version(
            previous.get("dataset_version"),
            f"previous_releases[{index}].dataset_version",
        )
        if version == dataset_version or version in previous_versions:
            raise ReleaseManifestError(f"duplicate retained dataset version: {version}")
        previous_versions.add(version)
        previous_root = _release_root(
            pointer.parent,
            previous.get("release_path"),
            version,
        )
        previous_artifacts = previous.get("artifacts")
        if not isinstance(previous_artifacts, dict):
            raise ReleaseManifestError(
                f"previous_releases[{index}] is missing artifacts"
            )
        previous_path, previous_hash = _artifact(
            previous_root,
            previous_artifacts,
            "tileset",
            "collection_streets.mbtiles",
        )
        previous_tilesets.append(TilesetRelease(version, previous_path, previous_hash))

    return CurrentRelease(
        dataset_version=dataset_version,
        database_path=database_path,
        database_sha256=database_hash,
        tileset_path=tileset_path,
        tileset_sha256=tileset_hash,
        previous_tilesets=tuple(previous_tilesets),
        manifest=manifest,
    )


def tileset_for_version(release: CurrentRelease, version: str) -> TilesetRelease | None:
    """Resolve only versions explicitly committed by the current pointer."""

    if not VERSION_PATTERN.fullmatch(version):
        return None
    if version == release.dataset_version:
        return TilesetRelease(version, release.tileset_path, release.tileset_sha256)
    return next(
        (candidate for candidate in release.previous_tilesets if candidate.dataset_version == version),
        None,
    )


def verify_artifact_checksum(path: str | Path, expected_sha256: str) -> bool:
    """Verify an artifact, caching the digest by resolved path/stat identity."""

    artifact = Path(path)
    try:
        stat = artifact.stat()
    except OSError:
        return False
    if not artifact.is_file() or stat.st_size <= 0:
        return False
    actual = _cached_sha256(str(artifact.resolve()), stat.st_mtime_ns, stat.st_size)
    return hmac.compare_digest(actual, expected_sha256)


_release_checksum_lock = threading.Lock()
_release_checksum_states: dict[tuple[object, ...], str] = {}


def release_checksum_status(
    release: CurrentRelease,
    *,
    synchronous_max_bytes: int,
) -> str:
    """Return ``verified``, ``verifying``, or ``invalid`` with single-flight I/O.

    Small test/development artifacts are verified inline.  Production-sized
    SQLite/MBTiles files are hashed once on a daemon worker so health requests
    never spend minutes blocked on bind-mounted storage.
    """

    if synchronous_max_bytes < 0:
        raise ValueError("synchronous checksum byte threshold must be non-negative")
    artifacts = (
        (release.database_path, release.database_sha256),
        (release.tileset_path, release.tileset_sha256),
    )
    identities: list[tuple[str, int, int, str]] = []
    for path, expected in artifacts:
        try:
            stat = path.stat()
        except OSError:
            return "invalid"
        if not path.is_file() or stat.st_size <= 0:
            return "invalid"
        identities.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size, expected))
    key: tuple[object, ...] = (release.dataset_version, *identities)
    total_bytes = sum(identity[2] for identity in identities)
    with _release_checksum_lock:
        existing = _release_checksum_states.get(key)
        if existing is not None:
            return existing
        # Installing the in-progress marker while holding the lock elects one
        # caller. Concurrent health/map-config requests cannot duplicate I/O.
        _release_checksum_states[key] = "verifying"
        if len(_release_checksum_states) > 16:
            for old_key in list(_release_checksum_states):
                if old_key != key and _release_checksum_states[old_key] != "verifying":
                    del _release_checksum_states[old_key]
                    if len(_release_checksum_states) <= 16:
                        break

    if total_bytes <= synchronous_max_bytes:
        _finish_release_checksum(key, artifacts)
        with _release_checksum_lock:
            return _release_checksum_states[key]
    worker = threading.Thread(
        target=_finish_release_checksum,
        args=(key, artifacts),
        name=f"release-checksum-{release.dataset_version}",
        daemon=True,
    )
    worker.start()
    return "verifying"


def artifact_checksum_status(
    path: str | Path,
    expected_sha256: str,
    *,
    synchronous_max_bytes: int,
) -> str:
    """Single-flight checksum status for a retained release artifact."""

    if synchronous_max_bytes < 0:
        raise ValueError("synchronous checksum byte threshold must be non-negative")
    artifact = Path(path)
    try:
        stat = artifact.stat()
    except OSError:
        return "invalid"
    if not artifact.is_file() or stat.st_size <= 0:
        return "invalid"
    identity = (
        str(artifact.resolve()),
        stat.st_mtime_ns,
        stat.st_size,
        expected_sha256,
    )
    key: tuple[object, ...] = ("artifact", identity)
    artifacts = ((artifact, expected_sha256),)
    with _release_checksum_lock:
        existing = _release_checksum_states.get(key)
        if existing is not None:
            return existing
        _release_checksum_states[key] = "verifying"
    if stat.st_size <= synchronous_max_bytes:
        _finish_release_checksum(key, artifacts)
        with _release_checksum_lock:
            return _release_checksum_states[key]
    threading.Thread(
        target=_finish_release_checksum,
        args=(key, artifacts),
        name="retained-tileset-checksum",
        daemon=True,
    ).start()
    return "verifying"


def _finish_release_checksum(
    key: tuple[object, ...],
    artifacts: tuple[tuple[Path, str], ...],
) -> None:
    try:
        valid = all(verify_artifact_checksum(path, expected) for path, expected in artifacts)
    except (OSError, ValueError):
        valid = False
    with _release_checksum_lock:
        _release_checksum_states[key] = "verified" if valid else "invalid"


@lru_cache(maxsize=16)
def _cached_sha256(path_string: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size
    digest = hashlib.sha256()
    with Path(path_string).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(value: object, label: str) -> str:
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise ReleaseManifestError(f"{label} is not a valid release version")
    return value


def _release_root(data_root: Path, value: object, version: str) -> Path:
    expected = PurePosixPath("releases") / version
    relative = _relative_path(value, "release_path")
    if relative != expected:
        raise ReleaseManifestError(
            f"release_path must be {expected.as_posix()!r} for dataset version {version!r}"
        )
    return _contained(data_root, relative, "release_path")


def _artifact(
    release_root: Path,
    artifacts: dict[str, object],
    name: str,
    expected_filename: str,
) -> tuple[Path, str]:
    descriptor = artifacts.get(name)
    if not isinstance(descriptor, dict):
        raise ReleaseManifestError(f"manifest artifact {name!r} must be an object")
    relative = _relative_path(descriptor.get("path"), f"artifacts.{name}.path")
    if relative != PurePosixPath(expected_filename):
        raise ReleaseManifestError(
            f"manifest artifact {name!r} must use filename {expected_filename!r}"
        )
    checksum = descriptor.get("sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ReleaseManifestError(f"manifest artifact {name!r} has an invalid sha256")
    return _contained(release_root, relative, f"artifacts.{name}.path"), checksum


def _relative_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseManifestError(f"{label} must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseManifestError(f"{label} must be a contained relative path")
    return path


def _contained(root: Path, relative: PurePosixPath, label: str) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*relative.parts).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ReleaseManifestError(f"{label} escapes the data directory")
    return candidate
