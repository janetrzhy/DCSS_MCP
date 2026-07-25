"""PostgreSQL store for DCSS save files.

Schema:
  slots — stores save files as base64 per-filename within a slot.

  Each slot can hold multiple files (save.cs, morgue.txt, etc.).
  Metadata (species, background, depth, turns) is extracted from morgue if available.
"""

import os
import json
import logging
from base64 import b64encode, b64decode
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS save_slots (
    slot       VARCHAR(128) PRIMARY KEY,
    files      JSONB NOT NULL DEFAULT '{}',
    meta       JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CRUD_SAVE = """
INSERT INTO save_slots (slot, files, meta, updated_at)
VALUES (%s, %s::jsonb, %s::jsonb, NOW())
ON CONFLICT (slot) DO UPDATE SET
    files = EXCLUDED.files,
    meta  = EXCLUDED.meta,
    updated_at = NOW();
"""

CRUD_LOAD = "SELECT slot, files, meta FROM save_slots WHERE slot = %s;"

CRUD_LIST = "SELECT slot, meta, updated_at FROM save_slots ORDER BY updated_at DESC;"

CRUD_DELETE = "DELETE FROM save_slots WHERE slot = %s;"


class PgStore:
    def __init__(self):
        self._conn: Optional[psycopg2.extensions.connection] = None

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def connect(self, dsn: str | None = None):
        dsn = dsn or os.environ.get("DATABASE_URL")
        if not dsn:
            logger.warning("DATABASE_URL not set. Saves will not persist.")
            return
        self._conn = psycopg2.connect(dsn, sslmode="require")
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        logger.info("Connected to PostgreSQL.")

    @property
    def available(self) -> bool:
        return self._conn is not None and self._conn.closed == 0

    def close(self):
        if self._conn and self._conn.closed == 0:
            self._conn.close()
            logger.info("PostgreSQL connection closed.")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def store(self, slot: str, files: dict[str, bytes], meta: dict | None = None):
        """Store a set of save files under *slot*.

        *files* is {filename: bytes}.  Bytes are base64-encoded for JSON storage.
        """
        if not self.available:
            logger.warning("DB unavailable, save not stored.")
            return

        encoded = {name: b64encode(data).decode() for name, data in files.items()}
        meta_json = json.dumps(meta or {})
        files_json = json.dumps(encoded)

        with self._conn.cursor() as cur:
            cur.execute(CRUD_SAVE, (slot, files_json, meta_json))
        logger.info("Saved slot=%s (%d files)", slot, len(files))

    def load(self, slot: str) -> dict[str, bytes] | None:
        """Load a save slot.  Returns {filename: bytes} or None."""
        if not self.available:
            return None

        with self._conn.cursor() as cur:
            cur.execute(CRUD_LOAD, (slot,))
            row = cur.fetchone()

        if not row:
            return None

        _slot, files_json, meta_json = row
        encoded: dict = files_json
        return {name: b64decode(data) for name, data in encoded.items()}

    def list_saves(self) -> list[dict]:
        """Return list of save metadata dicts."""
        if not self.available:
            return []

        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(CRUD_LIST)
            rows = cur.fetchall()

        result = []
        for r in rows:
            meta = r.get("meta") or {}
            result.append({
                "slot": r["slot"],
                "character": meta.get("character", "?"),
                "depth": meta.get("depth", "?"),
                "turns": meta.get("turns", 0),
                "updated_at": r["updated_at"].strftime("%Y-%m-%d %H:%M UTC"),
            })
        return result

    def delete(self, slot: str):
        if not self.available:
            return
        with self._conn.cursor() as cur:
            cur.execute(CRUD_DELETE, (slot,))
        logger.info("Deleted slot=%s", slot)
