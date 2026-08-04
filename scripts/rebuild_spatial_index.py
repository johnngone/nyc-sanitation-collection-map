"""Build the SQLite RTree index for an existing database."""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.database import initialize


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/app.sqlite3"))
    args = parser.parse_args()
    initialize(args.database)
    with sqlite3.connect(args.database) as connection:
        connection.execute("DELETE FROM block_faces_rtree")
        connection.execute("DELETE FROM block_face_rtree_map")
        rows = connection.execute("SELECT block_face_id, min_x, max_x, min_y, max_y FROM block_faces").fetchall()
        for block_face_id, min_x, max_x, min_y, max_y in rows:
            connection.execute("INSERT INTO block_face_rtree_map (block_face_id) VALUES (?)", (block_face_id,))
            rtree_id = connection.execute("SELECT rtree_id FROM block_face_rtree_map WHERE block_face_id = ?", (block_face_id,)).fetchone()[0]
            connection.execute("INSERT INTO block_faces_rtree VALUES (?, ?, ?, ?, ?)", (rtree_id, min_x, max_x, min_y, max_y))
    print(f"Indexed block_faces={len(rows)} database={args.database}")


if __name__ == "__main__":
    main()
