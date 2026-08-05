from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger("radar-affari.database")

SCHEMA_VERSION = 3


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RadarDatabase:
    """Archivio persistente per Radar Affari.

    Usa PostgreSQL quando DATABASE_URL è disponibile.
    Usa SQLite soltanto come fallback locale.
    """

    def __init__(self, target: Path | str) -> None:
        raw_target = str(target)
        env_database_url = os.getenv("DATABASE_URL", "").strip()

        if env_database_url:
            self.database_url = env_database_url
            self.backend = "postgresql"
            self.path: Optional[Path] = None
        elif raw_target.startswith(("postgres://", "postgresql://")):
            self.database_url = raw_target
            self.backend = "postgresql"
            self.path = None
        else:
            self.database_url = ""
            self.backend = "sqlite"
            self.path = Path(raw_target)
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.initialize()

    @property
    def backend_label(self) -> str:
        return "PostgreSQL" if self.backend == "postgresql" else "SQLite"

    @property
    def location_label(self) -> str:
        if self.backend == "postgresql":
            return "PostgreSQL"
        return str(self.path)

    def _pg_connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL configurato ma psycopg non è installato. "
                "Aggiungi psycopg[binary] al requirements.txt."
            ) from exc

        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]

        return psycopg.connect(url, row_factory=dict_row)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.backend == "postgresql":
            conn = self._pg_connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return

        assert self.path is not None
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        if self.backend == "postgresql":
            self._initialize_postgresql()
        else:
            self._initialize_sqlite()

    def _initialize_sqlite(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT 'subito',
                    source_listing_id TEXT,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    brand TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    variant TEXT NOT NULL DEFAULT '',
                    storage TEXT NOT NULL DEFAULT '',
                    condition_text TEXT NOT NULL DEFAULT '',
                    battery_health REAL,
                    seller_type TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    product_key TEXT NOT NULL DEFAULT '',
                    recognition_confidence INTEGER NOT NULL DEFAULT 0,
                    current_price REAL,
                    shipping_cost REAL NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    removed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    observations_count INTEGER NOT NULL DEFAULT 1,
                    last_scan_token TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_source_external
                    ON listings(source, source_listing_id)
                    WHERE source_listing_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_listings_product_status
                    ON listings(product_key, status);

                CREATE INDEX IF NOT EXISTS idx_listings_last_seen
                    ON listings(last_seen_at);

                CREATE INDEX IF NOT EXISTS idx_listings_price
                    ON listings(current_price);

                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                    price REAL NOT NULL,
                    observed_at TEXT NOT NULL,
                    scan_token TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_price_obs_listing_time
                    ON price_observations(listing_id, observed_at);

                CREATE TABLE IF NOT EXISTS deal_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                    asking_price REAL,
                    estimated_sale_price REAL,
                    maximum_buy_price REAL,
                    gross_margin REAL,
                    net_margin REAL,
                    roi REAL,
                    comparables_count INTEGER NOT NULL DEFAULT 0,
                    confidence INTEGER NOT NULL DEFAULT 0,
                    decision TEXT NOT NULL,
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    evaluated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_evaluations_listing_time
                    ON deal_evaluations(listing_id, evaluated_at);

                CREATE TABLE IF NOT EXISTS notifications (
                    listing_id TEXT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
                    notified_at TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def _initialize_postgresql(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'subito',
                source_listing_id TEXT,
                url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                brand TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                variant TEXT NOT NULL DEFAULT '',
                storage TEXT NOT NULL DEFAULT '',
                condition_text TEXT NOT NULL DEFAULT '',
                battery_health DOUBLE PRECISION,
                seller_type TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                product_key TEXT NOT NULL DEFAULT '',
                recognition_confidence INTEGER NOT NULL DEFAULT 0,
                current_price DOUBLE PRECISION,
                shipping_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                removed_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                observations_count INTEGER NOT NULL DEFAULT 1,
                last_scan_token TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_source_external
                ON listings(source, source_listing_id)
                WHERE source_listing_id IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_listings_product_status
                ON listings(product_key, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_listings_last_seen
                ON listings(last_seen_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_listings_price
                ON listings(current_price)
            """,
            """
            CREATE TABLE IF NOT EXISTS price_observations (
                id BIGSERIAL PRIMARY KEY,
                listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                price DOUBLE PRECISION NOT NULL,
                observed_at TEXT NOT NULL,
                scan_token TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_price_obs_listing_time
                ON price_observations(listing_id, observed_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS deal_evaluations (
                id BIGSERIAL PRIMARY KEY,
                listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                asking_price DOUBLE PRECISION,
                estimated_sale_price DOUBLE PRECISION,
                maximum_buy_price DOUBLE PRECISION,
                gross_margin DOUBLE PRECISION,
                net_margin DOUBLE PRECISION,
                roi DOUBLE PRECISION,
                comparables_count INTEGER NOT NULL DEFAULT 0,
                confidence INTEGER NOT NULL DEFAULT 0,
                decision TEXT NOT NULL,
                rejection_reason TEXT NOT NULL DEFAULT '',
                evaluated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_evaluations_listing_time
                ON deal_evaluations(listing_id, evaluated_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS notifications (
                listing_id TEXT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
                notified_at TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id BIGINT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT ''
            )
            """,
        ]

        with self.connect() as conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_meta(key, value)
                VALUES('schema_version', %s)
                ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def upsert_listing(
        self,
        item: Dict[str, Any],
        *,
        scan_token: Optional[str] = None,
        observed_at: Optional[str] = None,
    ) -> bool:
        now = observed_at or utc_now_iso()
        listing_id = str(item.get("id") or "").strip()
        url = str(item.get("url") or "").strip()
        if not listing_id or not url:
            raise ValueError("Annuncio senza id o URL")

        price = item.get("price")
        price_value = float(price) if isinstance(price, (int, float)) else None
        raw_json = json.dumps(item, ensure_ascii=False, default=str)

        values = (
            listing_id,
            str(item.get("source") or "subito"),
            item.get("source_listing_id"),
            url,
            str(item.get("title") or "")[:500],
            str(item.get("text") or item.get("description") or "")[:5000],
            str(item.get("category") or ""),
            str(item.get("brand") or ""),
            str(item.get("model") or ""),
            str(item.get("variant") or ""),
            str(item.get("storage") or ""),
            str(item.get("condition") or item.get("condition_text") or ""),
            item.get("battery_health"),
            str(item.get("seller_type") or ""),
            str(item.get("location") or ""),
            str(item.get("product_key") or "").lower(),
            int(item.get("recognition_confidence") or 0),
            price_value,
            float(item.get("shipping_cost") or 0),
            now,
            now,
            scan_token,
            raw_json,
            now,
            now,
        )

        with self.connect() as conn:
            placeholder = "%s" if self.backend == "postgresql" else "?"
            exists = conn.execute(
                f"SELECT 1 FROM listings WHERE id = {placeholder}",
                (listing_id,),
            ).fetchone() is not None

            placeholders = ", ".join([placeholder] * len(values))
            conn.execute(
                f"""
                INSERT INTO listings (
                    id, source, source_listing_id, url, title, description,
                    category, brand, model, variant, storage, condition_text,
                    battery_health, seller_type, location, product_key,
                    recognition_confidence, current_price, shipping_cost,
                    first_seen_at, last_seen_at, last_scan_token, raw_json,
                    created_at, updated_at
                ) VALUES ({placeholders})
                ON CONFLICT(id) DO UPDATE SET
                    url = EXCLUDED.url,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    category = CASE WHEN EXCLUDED.category <> '' THEN EXCLUDED.category ELSE listings.category END,
                    brand = CASE WHEN EXCLUDED.brand <> '' THEN EXCLUDED.brand ELSE listings.brand END,
                    model = CASE WHEN EXCLUDED.model <> '' THEN EXCLUDED.model ELSE listings.model END,
                    variant = CASE WHEN EXCLUDED.variant <> '' THEN EXCLUDED.variant ELSE listings.variant END,
                    storage = CASE WHEN EXCLUDED.storage <> '' THEN EXCLUDED.storage ELSE listings.storage END,
                    condition_text = CASE WHEN EXCLUDED.condition_text <> '' THEN EXCLUDED.condition_text ELSE listings.condition_text END,
                    battery_health = COALESCE(EXCLUDED.battery_health, listings.battery_health),
                    seller_type = CASE WHEN EXCLUDED.seller_type <> '' THEN EXCLUDED.seller_type ELSE listings.seller_type END,
                    location = CASE WHEN EXCLUDED.location <> '' THEN EXCLUDED.location ELSE listings.location END,
                    product_key = CASE WHEN EXCLUDED.product_key <> '' THEN EXCLUDED.product_key ELSE listings.product_key END,
                    recognition_confidence = CASE
                        WHEN EXCLUDED.recognition_confidence > listings.recognition_confidence
                        THEN EXCLUDED.recognition_confidence
                        ELSE listings.recognition_confidence
                    END,
                    current_price = COALESCE(EXCLUDED.current_price, listings.current_price),
                    shipping_cost = EXCLUDED.shipping_cost,
                    last_seen_at = EXCLUDED.last_seen_at,
                    removed_at = NULL,
                    status = 'active',
                    observations_count = listings.observations_count + 1,
                    last_scan_token = EXCLUDED.last_scan_token,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = EXCLUDED.updated_at
                """,
                values,
            )

            if price_value is not None:
                conn.execute(
                    f"""
                    INSERT INTO price_observations(
                        listing_id, price, observed_at, scan_token
                    ) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                    """,
                    (listing_id, price_value, now, scan_token),
                )

        return not exists

    def mark_missing_after_scan(
        self,
        scan_token: str,
        *,
        source: Optional[str] = None,
        grace_hours: int = 24,
    ) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(grace_hours, 0))
        ).isoformat()
        now = utc_now_iso()
        placeholder = "%s" if self.backend == "postgresql" else "?"

        query = f"""
            UPDATE listings
            SET status = 'missing',
                removed_at = COALESCE(removed_at, {placeholder}),
                updated_at = {placeholder}
            WHERE status = 'active'
              AND COALESCE(last_scan_token, '') <> {placeholder}
              AND last_seen_at < {placeholder}
        """
        params: List[Any] = [now, now, scan_token, cutoff]

        if source:
            query += f" AND source = {placeholder}"
            params.append(source)

        with self.connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount

    def active_comparables(
        self,
        product_key: str,
        *,
        exclude_listing_id: Optional[str] = None,
        max_age_days: int = 30,
    ) -> List[Dict[str, Any]]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max_age_days)
        ).isoformat()
        placeholder = "%s" if self.backend == "postgresql" else "?"
        params: List[Any] = [product_key.lower(), cutoff]
        exclude_sql = ""

        if exclude_listing_id:
            exclude_sql = f" AND id <> {placeholder}"
            params.append(exclude_listing_id)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, description, url,
                       current_price AS price, first_seen_at,
                       last_seen_at, observations_count, location
                FROM listings
                WHERE product_key = {placeholder}
                  AND status = 'active'
                  AND current_price IS NOT NULL
                  AND last_seen_at >= {placeholder}
                  {exclude_sql}
                ORDER BY current_price ASC, last_seen_at DESC
                """,
                params,
            ).fetchall()

        return [dict(row) for row in rows]


    def unknown_listings(
        self,
        limit: int = 20,
        *,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Restituisce annunci pertinenti ancora non identificati.

        La pertinenza viene ricavata dal raw_json salvato dal collector.
        Gli annunci senza URL o prezzo vengono esclusi.
        """
        placeholder = "%s" if self.backend == "postgresql" else "?"
        status_sql = "AND status = 'active'" if active_only else ""

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, url, title, description, category, brand, model,
                       variant, storage, product_key,
                       recognition_confidence,
                       current_price AS price, raw_json, last_seen_at
                FROM listings
                WHERE current_price IS NOT NULL
                  AND url <> ''
                  AND (
                        product_key = ''
                        OR product_key = 'unidentified'
                        OR model = ''
                      )
                  {status_sql}
                ORDER BY last_seen_at DESC
                LIMIT {placeholder}
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                raw = json.loads(item.get("raw_json") or "{}")
            except json.JSONDecodeError:
                raw = {}

            matched = raw.get("matched") or []
            relevant = bool(raw.get("relevant")) or bool(matched)
            excluded = bool(raw.get("excluded"))

            if not relevant or excluded:
                continue

            item["matched"] = matched
            item["relevant"] = relevant
            item["excluded"] = excluded
            results.append(item)

        return results

    def update_listing_recognition(
        self,
        listing_id: str,
        fused_result: Dict[str, Any],
    ) -> bool:
        """Aggiorna l'identità del prodotto solo con una fusione utilizzabile."""
        if not fused_result.get("usable"):
            return False

        product_key = str(
            fused_result.get("product_key") or ""
        ).strip().lower()
        brand = str(fused_result.get("brand") or "").strip()
        model = str(fused_result.get("model") or "").strip()

        if not product_key or product_key == "unidentified" or not brand or not model:
            return False

        placeholder = "%s" if self.backend == "postgresql" else "?"
        now = utc_now_iso()

        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE listings
                SET category = CASE
                        WHEN {placeholder} <> '' THEN {placeholder}
                        ELSE category
                    END,
                    brand = {placeholder},
                    model = {placeholder},
                    variant = {placeholder},
                    storage = {placeholder},
                    product_key = {placeholder},
                    recognition_confidence = {placeholder},
                    updated_at = {placeholder}
                WHERE id = {placeholder}
                """,
                (
                    str(fused_result.get("category") or ""),
                    str(fused_result.get("category") or ""),
                    brand,
                    model,
                    str(fused_result.get("variant") or ""),
                    str(fused_result.get("storage_or_size") or ""),
                    product_key,
                    int(fused_result.get("confidence") or 0),
                    now,
                    str(listing_id),
                ),
            )
            return cursor.rowcount > 0


    def record_evaluation(self, listing_id: str, result: Dict[str, Any]) -> None:
        placeholder = "%s" if self.backend == "postgresql" else "?"
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO deal_evaluations (
                    listing_id, asking_price, estimated_sale_price,
                    maximum_buy_price, gross_margin, net_margin, roi,
                    comparables_count, confidence, decision,
                    rejection_reason, evaluated_at
                ) VALUES (
                    {placeholder}, {placeholder}, {placeholder}, {placeholder},
                    {placeholder}, {placeholder}, {placeholder}, {placeholder},
                    {placeholder}, {placeholder}, {placeholder}, {placeholder}
                )
                """,
                (
                    listing_id,
                    result.get("asking_price"),
                    result.get("estimated_sale_price"),
                    result.get("maximum_buy_price"),
                    result.get("gross_margin"),
                    result.get("net_margin"),
                    result.get("roi"),
                    int(result.get("comparables_count") or 0),
                    int(result.get("confidence") or 0),
                    str(result.get("decision") or "DATI_INSUFFICIENTI"),
                    str(result.get("rejection_reason") or ""),
                    str(result.get("evaluated_at") or utc_now_iso()),
                ),
            )

    def was_notified(self, listing_id: str) -> bool:
        placeholder = "%s" if self.backend == "postgresql" else "?"
        with self.connect() as conn:
            return conn.execute(
                f"SELECT 1 FROM notifications WHERE listing_id = {placeholder}",
                (listing_id,),
            ).fetchone() is not None

    def mark_notified(self, listing_id: str, decision: str = "") -> None:
        placeholder = "%s" if self.backend == "postgresql" else "?"
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO notifications(listing_id, notified_at, decision)
                VALUES ({placeholder}, {placeholder}, {placeholder})
                ON CONFLICT(listing_id) DO UPDATE SET
                    notified_at = EXCLUDED.notified_at,
                    decision = EXCLUDED.decision
                """,
                (listing_id, utc_now_iso(), decision),
            )

    def add_subscriber(self, chat_id: int) -> None:
        placeholder = "%s" if self.backend == "postgresql" else "?"
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO subscribers(chat_id, active, created_at, updated_at)
                VALUES ({placeholder}, 1, {placeholder}, {placeholder})
                ON CONFLICT(chat_id) DO UPDATE SET
                    active = 1,
                    updated_at = EXCLUDED.updated_at
                """,
                (int(chat_id), now, now),
            )

    def remove_subscriber(self, chat_id: int) -> None:
        placeholder = "%s" if self.backend == "postgresql" else "?"
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE subscribers
                SET active = 0, updated_at = {placeholder}
                WHERE chat_id = {placeholder}
                """,
                (utc_now_iso(), int(chat_id)),
            )

    def subscribers(self) -> List[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM subscribers WHERE active = 1 ORDER BY chat_id"
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def clear_notifications(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM notifications")
            return cursor.rowcount

    def product_counts(self, limit: int = 8) -> List[Dict[str, Any]]:
        placeholder = "%s" if self.backend == "postgresql" else "?"
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT CASE
                    WHEN product_key = '' THEN 'non identificato'
                    ELSE product_key
                END AS product_key,
                COUNT(*) AS count
                FROM listings
                GROUP BY CASE
                    WHEN product_key = '' THEN 'non identificato'
                    ELSE product_key
                END
                ORDER BY count DESC, product_key ASC
                LIMIT {placeholder}
                """,
                (max(int(limit), 1),),
            ).fetchall()
        return [dict(row) for row in rows]

    def price_change_listings_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM (
                    SELECT listing_id
                    FROM price_observations
                    GROUP BY listing_id
                    HAVING COUNT(DISTINCT price) > 1
                ) changed
                """
            ).fetchone()
        return int(row["n"] or 0)

    def stats(self) -> Dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing,
                    SUM(CASE WHEN product_key <> '' THEN 1 ELSE 0 END) AS recognized
                FROM listings
                """
            ).fetchone()
            observations = conn.execute(
                "SELECT COUNT(*) AS n FROM price_observations"
            ).fetchone()["n"]
            evaluations = conn.execute(
                "SELECT COUNT(*) AS n FROM deal_evaluations"
            ).fetchone()["n"]

        return {
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "missing": int(row["missing"] or 0),
            "recognized": int(row["recognized"] or 0),
            "price_observations": int(observations or 0),
            "evaluations": int(evaluations or 0),
        }

    def migrate_json_files(
        self,
        *,
        history_file: Optional[Path | str] = None,
        state_file: Optional[Path | str] = None,
        subscribers_file: Optional[Path | str] = None,
    ) -> Dict[str, int]:
        # Le vecchie migrazioni JSON vengono usate solo in fallback SQLite.
        # In PostgreSQL il collector ricostruisce automaticamente il mercato.
        if self.backend == "postgresql":
            return {"history": 0, "notifications": 0, "subscribers": 0}

        migration_name = "legacy_json_v1"
        with self.connect() as conn:
            already = conn.execute(
                "SELECT 1 FROM migrations WHERE name = ?",
                (migration_name,),
            ).fetchone()

        if already:
            return {"history": 0, "notifications": 0, "subscribers": 0}

        counts = {"history": 0, "notifications": 0, "subscribers": 0}

        if history_file and Path(history_file).exists():
            try:
                history = json.loads(Path(history_file).read_text(encoding="utf-8"))
                for row in history if isinstance(history, list) else []:
                    if not isinstance(row, dict):
                        continue
                    item = {
                        "id": row.get("id"),
                        "url": row.get("url"),
                        "title": row.get("title", ""),
                        "price": row.get("price"),
                        "source": "subito",
                        "brand": row.get("brand", ""),
                        "model": row.get("model", ""),
                        "product_key": row.get("product_key", ""),
                    }
                    if item["id"] and item["url"]:
                        self.upsert_listing(
                            item,
                            observed_at=row.get("last_seen") or row.get("first_seen"),
                        )
                        counts["history"] += 1
            except Exception as exc:
                log.warning("Migrazione history fallita: %s", exc)

        if subscribers_file and Path(subscribers_file).exists():
            try:
                values = json.loads(Path(subscribers_file).read_text(encoding="utf-8"))
                for chat_id in values if isinstance(values, list) else []:
                    self.add_subscriber(int(chat_id))
                    counts["subscribers"] += 1
            except Exception as exc:
                log.warning("Migrazione subscribers fallita: %s", exc)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO migrations(name, applied_at, details)
                VALUES (?, ?, ?)
                """,
                (migration_name, utc_now_iso(), json.dumps(counts)),
            )

        return counts
