"""PostgreSQL store for DCSS save files.

The physical table remains backward-compatible with the original single-slot
schema. New multi-profile saves are namespaced into the existing ``slot``
primary key as ``<instance_id>::<checkpoint>``. Legacy rows such as
``Asterion`` are exposed as instance ``Asterion`` / checkpoint ``latest``.
"""

import json
import logging
import os
from base64 import b64decode, b64encode
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

CRUD_LOAD = """
SELECT slot, files, meta, updated_at
FROM save_slots
WHERE slot = %s;
"""

CRUD_LIST = """
SELECT slot, meta, updated_at
FROM save_slots
ORDER BY updated_at DESC;
"""

CRUD_DELETE = "DELETE FROM save_slots WHERE slot = %s;"

NAMESPACE_SEP = "::"
MAX_PART_LEN = 60


def _clean_part(value: str, *, field: str, default: str | None = None) -> str:
    text = (value or "").strip()
    if not text and default is not None:
        text = default
    if not text:
        raise ValueError(f"{field} must not be empty")
    if NAMESPACE_SEP in text:
        raise ValueError(f"{field} must not contain {NAMESPACE_SEP!r}")
    if len(text) > MAX_PART_LEN:
        raise ValueError(f"{field} is too long (max {MAX_PART_LEN} characters)")
    return text


def _db_key(instance_id: str, checkpoint: str) -> str:
    instance = _clean_part(instance_id, field="instance_id")
    slot = _clean_part(checkpoint, field="slot", default="latest")
    key = f"{instance}{NAMESPACE_SEP}{slot}"
    if len(key) > 128:
        raise ValueError("combined instance_id/slot is too long for the database")
    return key


def _decode_key(db_slot: str, meta: dict | None = None) -> tuple[str, str, bool]:
    meta = meta or {}
    if NAMESPACE_SEP in db_slot:
        instance_id, checkpoint = db_slot.split(NAMESPACE_SEP, 1)
        return instance_id, checkpoint, False

    instance_id = str(meta.get("instance_id") or db_slot)
    checkpoint = str(meta.get("checkpoint") or "latest")
    return instance_id, checkpoint, True


class PgStore:
    def __init__(self):
        self._conn: Optional[psycopg2.extensions.connection] = None

    def connect(self, dsn: str | None = None):
        dsn = dsn or os.environ.get("DATABASE_URL")
        if not dsn:
            logger.warning("DATABASE_URL not set. Saves will not persist.")
            return

        self._conn = psycopg2.connect(
            dsn,
            sslmode=os.environ.get("PGSSLMODE", "require"),
        )
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

    def store(
        self,
        slot: str,
        files: dict[str, bytes],
        meta: dict | None = None,
        *,
        instance_id: str | None = None,
    ) -> str | None:
        """Store *files* and return the physical database key.

        With ``instance_id`` checkpoints are stored as ``instance_id::slot``.
        Omitting it keeps the original single-slot behaviour for compatibility.
        """
        if not self.available:
            logger.warning("DB unavailable, save not stored.")
            return None

        checkpoint = _clean_part(slot, field="slot", default="latest")
        db_slot = _db_key(instance_id, checkpoint) if instance_id else checkpoint

        merged_meta = dict(meta or {})
        if instance_id:
            merged_meta["instance_id"] = _clean_part(instance_id, field="instance_id")
            merged_meta["checkpoint"] = checkpoint

        encoded = {name: b64encode(data).decode() for name, data in files.items()}
        with self._conn.cursor() as cur:
            cur.execute(
                CRUD_SAVE,
                (db_slot, json.dumps(encoded), json.dumps(merged_meta)),
            )
            # After safely writing X::latest, remove the old legacy X row so
            # list_saves() does not show duplicate "latest" checkpoints.
            if instance_id and checkpoint == "latest":
                legacy_key = _clean_part(instance_id, field="instance_id")
                if legacy_key != db_slot:
                    cur.execute(CRUD_DELETE, (legacy_key,))

        logger.info("Saved slot=%s (%d files)", db_slot, len(files))
        return db_slot

    def _fetch_row(self, db_slot: str):
        if not self.available:
            return None
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(CRUD_LOAD, (db_slot,))
            return cur.fetchone()

    def load_record(
        self,
        slot: str = "latest",
        instance_id: str | None = None,
    ) -> dict | None:
        """Load one checkpoint including metadata and files.

        ``instance_id=X, slot=latest`` first checks ``X::latest`` and then
        falls back to legacy row ``X`` so pre-migration saves keep working.
        """
        checkpoint = _clean_part(slot, field="slot", default="latest")

        if instance_id:
            instance = _clean_part(instance_id, field="instance_id")
            candidates = [_db_key(instance, checkpoint)]
            if checkpoint == "latest":
                candidates.append(instance)
        else:
            candidates = [checkpoint]

        row = None
        for candidate in candidates:
            row = self._fetch_row(candidate)
            if row:
                break
        if not row:
            return None

        meta = row.get("meta") or {}
        resolved_instance, resolved_slot, legacy = _decode_key(row["slot"], meta)
        encoded: dict = row["files"] or {}
        files = {name: b64decode(data) for name, data in encoded.items()}

        return {
            "instance_id": resolved_instance,
            "slot": resolved_slot,
            "db_slot": row["slot"],
            "files": files,
            "meta": meta,
            "legacy": legacy,
            "updated_at": row["updated_at"],
        }

    def load(
        self,
        slot: str = "latest",
        instance_id: str | None = None,
    ) -> dict[str, bytes] | None:
        record = self.load_record(slot=slot, instance_id=instance_id)
        return record["files"] if record else None

    def list_saves(self, instance_id: str | None = None) -> list[dict]:
        """Return logical profile/checkpoint entries.

        Legacy rows are represented as ``instance_id=<old slot>, slot=latest``.
        """
        if not self.available:
            return []

        wanted = instance_id.strip().lower() if instance_id else None
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(CRUD_LIST)
            rows = cur.fetchall()

        result = []
        for row in rows:
            meta = row.get("meta") or {}
            resolved_instance, checkpoint, legacy = _decode_key(row["slot"], meta)
            if wanted and resolved_instance.lower() != wanted:
                continue

            result.append({
                "instance_id": resolved_instance,
                "slot": checkpoint,
                "db_slot": row["slot"],
                "legacy": legacy,
                "character": meta.get("character", resolved_instance),
                "depth": meta.get("depth", "?"),
                "turns": meta.get("turns", 0),
                "updated_at": row["updated_at"].strftime("%Y-%m-%d %H:%M UTC"),
            })
        return result

    def list_instances(self) -> list[dict]:
        """Summarise profiles, newest checkpoint first."""
        saves = self.list_saves()
        instances: dict[str, dict] = {}
        for save in saves:
            key = save["instance_id"].lower()
            if key not in instances:
                instances[key] = {
                    "instance_id": save["instance_id"],
                    "character": save["character"],
                    "latest_checkpoint": save["slot"],
                    "updated_at": save["updated_at"],
                    "checkpoint_count": 0,
                }
            instances[key]["checkpoint_count"] += 1
        return list(instances.values())

    def delete(self, slot: str, instance_id: str | None = None) -> bool:
        if not self.available:
            return False

        checkpoint = _clean_part(slot, field="slot", default="latest")
        if instance_id:
            instance = _clean_part(instance_id, field="instance_id")
            candidates = [_db_key(instance, checkpoint)]
            if checkpoint == "latest":
                candidates.append(instance)
        else:
            candidates = [checkpoint]

        with self._conn.cursor() as cur:
            for db_slot in candidates:
                cur.execute(CRUD_DELETE, (db_slot,))
                if cur.rowcount:
                    logger.info("Deleted slot=%s", db_slot)
                    return True
        return False
