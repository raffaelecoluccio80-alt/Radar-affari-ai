from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from radar_database import RadarDatabase


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def build_vision_hash(
    *,
    listing_url: str,
    title: str = "",
    description: str = "",
    image_urls: Optional[list[str]] = None,
) -> str:
    """Crea un hash stabile del contenuto usato per l'analisi Vision.

    Se titolo, descrizione o immagini cambiano, l'hash cambia e la cache
    può essere aggiornata senza riutilizzare un risultato ormai vecchio.
    """
    payload = {
        "listing_url": str(listing_url or "").strip(),
        "title": str(title or "").strip(),
        "description": str(description or "").strip(),
        "image_urls": sorted(
            str(url).strip()
            for url in (image_urls or [])
            if str(url).strip()
        ),
    }
    raw = _json_dumps(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class VisionCache:
    """Cache persistente delle analisi Vision.

    Funziona sia con PostgreSQL sia con SQLite tramite RadarDatabase.
    Non modifica le tabelle esistenti del Radar.
    """

    def __init__(self, database: RadarDatabase) -> None:
        self.database = database
        self.initialize()

    @property
    def _placeholder(self) -> str:
        return "%s" if self.database.backend == "postgresql" else "?"

    def initialize(self) -> None:
        if self.database.backend == "postgresql":
            statements = [
                """
                CREATE TABLE IF NOT EXISTS vision_cache (
                    listing_id TEXT PRIMARY KEY
                        REFERENCES listings(id) ON DELETE CASCADE,
                    vision_hash TEXT NOT NULL,
                    vision_json TEXT NOT NULL,
                    fused_json TEXT NOT NULL DEFAULT '{}',
                    recognition_confidence INTEGER NOT NULL DEFAULT 0,
                    condition_confidence INTEGER NOT NULL DEFAULT 0,
                    model_used TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'success',
                    error_message TEXT NOT NULL DEFAULT '',
                    analyzed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_vision_cache_hash
                    ON vision_cache(vision_hash)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_vision_cache_updated
                    ON vision_cache(updated_at)
                """,
            ]
        else:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS vision_cache (
                    listing_id TEXT PRIMARY KEY
                        REFERENCES listings(id) ON DELETE CASCADE,
                    vision_hash TEXT NOT NULL,
                    vision_json TEXT NOT NULL,
                    fused_json TEXT NOT NULL DEFAULT '{}',
                    recognition_confidence INTEGER NOT NULL DEFAULT 0,
                    condition_confidence INTEGER NOT NULL DEFAULT 0,
                    model_used TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'success',
                    error_message TEXT NOT NULL DEFAULT '',
                    analyzed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_vision_cache_hash
                    ON vision_cache(vision_hash)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_vision_cache_updated
                    ON vision_cache(updated_at)
                """,
            ]

        with self.database.connect() as conn:
            for statement in statements:
                conn.execute(statement)

    def get(
        self,
        listing_id: str,
        *,
        expected_hash: Optional[str] = None,
        max_age_days: Optional[int] = None,
        include_errors: bool = False,
    ) -> Optional[Dict[str, Any]]:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None

        placeholder = self._placeholder

        with self.database.connect() as conn:
            row = conn.execute(
                f"""
                SELECT listing_id, vision_hash, vision_json, fused_json,
                       recognition_confidence, condition_confidence,
                       model_used, status, error_message,
                       analyzed_at, updated_at
                FROM vision_cache
                WHERE listing_id = {placeholder}
                """,
                (listing_id,),
            ).fetchone()

        if row is None:
            return None

        row = dict(row)

        if expected_hash and row["vision_hash"] != expected_hash:
            return None

        if not include_errors and row.get("status") != "success":
            return None

        if max_age_days is not None:
            try:
                updated_at = datetime.fromisoformat(
                    str(row["updated_at"]).replace("Z", "+00:00")
                )
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(
                    days=max(int(max_age_days), 0)
                )
                if updated_at < cutoff:
                    return None
            except (TypeError, ValueError):
                return None

        try:
            vision_result = json.loads(row.get("vision_json") or "{}")
        except json.JSONDecodeError:
            vision_result = {}

        try:
            fused_result = json.loads(row.get("fused_json") or "{}")
        except json.JSONDecodeError:
            fused_result = {}

        return {
            "listing_id": row["listing_id"],
            "vision_hash": row["vision_hash"],
            "vision_result": vision_result,
            "fused_result": fused_result,
            "recognition_confidence": int(
                row.get("recognition_confidence") or 0
            ),
            "condition_confidence": int(
                row.get("condition_confidence") or 0
            ),
            "model_used": row.get("model_used") or "",
            "status": row.get("status") or "",
            "error_message": row.get("error_message") or "",
            "analyzed_at": row.get("analyzed_at") or "",
            "updated_at": row.get("updated_at") or "",
        }

    def save_success(
        self,
        *,
        listing_id: str,
        vision_hash: str,
        vision_result: Dict[str, Any],
        fused_result: Optional[Dict[str, Any]] = None,
        model_used: str = "",
        analyzed_at: Optional[str] = None,
    ) -> None:
        listing_id = str(listing_id or "").strip()
        vision_hash = str(vision_hash or "").strip()

        if not listing_id or not vision_hash:
            raise ValueError("listing_id e vision_hash sono obbligatori")

        now = analyzed_at or utc_now_iso()
        placeholder = self._placeholder

        values = (
            listing_id,
            vision_hash,
            _json_dumps(vision_result),
            _json_dumps(fused_result or {}),
            int(vision_result.get("recognition_confidence") or 0),
            int(vision_result.get("condition_confidence") or 0),
            str(model_used or ""),
            "success",
            "",
            now,
            now,
        )

        with self.database.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO vision_cache (
                    listing_id, vision_hash, vision_json, fused_json,
                    recognition_confidence, condition_confidence,
                    model_used, status, error_message,
                    analyzed_at, updated_at
                ) VALUES (
                    {", ".join([placeholder] * len(values))}
                )
                ON CONFLICT(listing_id) DO UPDATE SET
                    vision_hash = EXCLUDED.vision_hash,
                    vision_json = EXCLUDED.vision_json,
                    fused_json = EXCLUDED.fused_json,
                    recognition_confidence = EXCLUDED.recognition_confidence,
                    condition_confidence = EXCLUDED.condition_confidence,
                    model_used = EXCLUDED.model_used,
                    status = 'success',
                    error_message = '',
                    analyzed_at = EXCLUDED.analyzed_at,
                    updated_at = EXCLUDED.updated_at
                """,
                values,
            )

    def save_error(
        self,
        *,
        listing_id: str,
        vision_hash: str,
        error_message: str,
        model_used: str = "",
        analyzed_at: Optional[str] = None,
    ) -> None:
        listing_id = str(listing_id or "").strip()
        vision_hash = str(vision_hash or "").strip()

        if not listing_id or not vision_hash:
            raise ValueError("listing_id e vision_hash sono obbligatori")

        now = analyzed_at or utc_now_iso()
        placeholder = self._placeholder

        values = (
            listing_id,
            vision_hash,
            "{}",
            "{}",
            0,
            0,
            str(model_used or ""),
            "error",
            str(error_message or "")[:2000],
            now,
            now,
        )

        with self.database.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO vision_cache (
                    listing_id, vision_hash, vision_json, fused_json,
                    recognition_confidence, condition_confidence,
                    model_used, status, error_message,
                    analyzed_at, updated_at
                ) VALUES (
                    {", ".join([placeholder] * len(values))}
                )
                ON CONFLICT(listing_id) DO UPDATE SET
                    vision_hash = EXCLUDED.vision_hash,
                    vision_json = EXCLUDED.vision_json,
                    fused_json = EXCLUDED.fused_json,
                    recognition_confidence = 0,
                    condition_confidence = 0,
                    model_used = EXCLUDED.model_used,
                    status = 'error',
                    error_message = EXCLUDED.error_message,
                    analyzed_at = EXCLUDED.analyzed_at,
                    updated_at = EXCLUDED.updated_at
                """,
                values,
            )

    def needs_refresh(
        self,
        *,
        listing_id: str,
        expected_hash: str,
        max_age_days: int = 30,
    ) -> bool:
        return self.get(
            listing_id,
            expected_hash=expected_hash,
            max_age_days=max_age_days,
        ) is None

    def delete(self, listing_id: str) -> bool:
        placeholder = self._placeholder
        with self.database.connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM vision_cache WHERE listing_id = {placeholder}",
                (str(listing_id),),
            )
            return cursor.rowcount > 0

    def stats(self) -> Dict[str, int]:
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)
                        AS success,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)
                        AS errors
                FROM vision_cache
                """
            ).fetchone()

        return {
            "total": int(row["total"] or 0),
            "success": int(row["success"] or 0),
            "errors": int(row["errors"] or 0),
        }
