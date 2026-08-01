from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

log = logging.getLogger("radar-affari.database")

SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RadarDatabase:
    """Archivio SQLite persistente per Radar Affari.

    Obiettivi:
    - una riga stabile per ogni annuncio;
    - una osservazione separata per ogni prezzo rilevato;
    - stato attivo/scomparso;
    - storico delle valutazioni e delle notifiche;
    - migrazione idempotente dai vecchi JSON.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'missing', 'removed', 'sold', 'unknown')),
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
                    scan_token TEXT,
                    UNIQUE(listing_id, price, observed_at)
                );

                CREATE INDEX IF NOT EXISTS idx_price_obs_listing_time
                    ON price_observations(listing_id, observed_at);

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_key TEXT NOT NULL,
                    active_count INTEGER NOT NULL,
                    minimum_price REAL,
                    percentile_25 REAL,
                    median_price REAL,
                    quick_sale_price REAL,
                    calculated_at TEXT NOT NULL,
                    UNIQUE(product_key, calculated_at)
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_product_time
                    ON market_snapshots(product_key, calculated_at);

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

    def upsert_listing(
        self,
        item: Dict[str, Any],
        *,
        scan_token: Optional[str] = None,
        observed_at: Optional[str] = None,
    ) -> bool:
        """Inserisce o aggiorna un annuncio. Restituisce True se era nuovo."""
        now = observed_at or utc_now_iso()
        listing_id = str(item.get("id") or "").strip()
        url = str(item.get("url") or "").strip()
        if not listing_id or not url:
            raise ValueError("Annuncio senza id o URL")

        price = item.get("price")
        price_value = float(price) if isinstance(price, (int, float)) else None
        raw_json = json.dumps(item, ensure_ascii=False, default=str)

        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM listings WHERE id = ?",
                (listing_id,),
            ).fetchone() is not None

            conn.execute(
                """
                INSERT INTO listings (
                    id, source, source_listing_id, url, title, description,
                    category, brand, model, variant, storage, condition_text,
                    battery_health, seller_type, location, product_key,
                    recognition_confidence, current_price, shipping_cost,
                    first_seen_at, last_seen_at, removed_at, status,
                    observations_count, last_scan_token, raw_json,
                    created_at, updated_at
                ) VALUES (
                    :id, :source, :source_listing_id, :url, :title, :description,
                    :category, :brand, :model, :variant, :storage, :condition_text,
                    :battery_health, :seller_type, :location, :product_key,
                    :recognition_confidence, :current_price, :shipping_cost,
                    :first_seen_at, :last_seen_at, NULL, 'active',
                    1, :last_scan_token, :raw_json, :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    title = excluded.title,
                    description = excluded.description,
                    category = CASE WHEN excluded.category <> '' THEN excluded.category ELSE listings.category END,
                    brand = CASE WHEN excluded.brand <> '' THEN excluded.brand ELSE listings.brand END,
                    model = CASE WHEN excluded.model <> '' THEN excluded.model ELSE listings.model END,
                    variant = CASE WHEN excluded.variant <> '' THEN excluded.variant ELSE listings.variant END,
                    storage = CASE WHEN excluded.storage <> '' THEN excluded.storage ELSE listings.storage END,
                    condition_text = CASE WHEN excluded.condition_text <> '' THEN excluded.condition_text ELSE listings.condition_text END,
                    battery_health = COALESCE(excluded.battery_health, listings.battery_health),
                    seller_type = CASE WHEN excluded.seller_type <> '' THEN excluded.seller_type ELSE listings.seller_type END,
                    location = CASE WHEN excluded.location <> '' THEN excluded.location ELSE listings.location END,
                    product_key = CASE WHEN excluded.product_key <> '' THEN excluded.product_key ELSE listings.product_key END,
                    recognition_confidence = MAX(excluded.recognition_confidence, listings.recognition_confidence),
                    current_price = COALESCE(excluded.current_price, listings.current_price),
                    shipping_cost = excluded.shipping_cost,
                    last_seen_at = excluded.last_seen_at,
                    removed_at = NULL,
                    status = 'active',
                    observations_count = listings.observations_count + 1,
                    last_scan_token = excluded.last_scan_token,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                {
                    "id": listing_id,
                    "source": str(item.get("source") or "subito"),
                    "source_listing_id": item.get("source_listing_id"),
                    "url": url,
                    "title": str(item.get("title") or "")[:500],
                    "description": str(item.get("text") or item.get("description") or "")[:5000],
                    "category": str(item.get("category") or ""),
                    "brand": str(item.get("brand") or ""),
                    "model": str(item.get("model") or ""),
                    "variant": str(item.get("variant") or ""),
                    "storage": str(item.get("storage") or ""),
                    "condition_text": str(item.get("condition") or item.get("condition_text") or ""),
                    "battery_health": item.get("battery_health"),
                    "seller_type": str(item.get("seller_type") or ""),
                    "location": str(item.get("location") or ""),
                    "product_key": str(item.get("product_key") or "").lower(),
                    "recognition_confidence": int(item.get("recognition_confidence") or 0),
                    "current_price": price_value,
                    "shipping_cost": float(item.get("shipping_cost") or 0),
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_scan_token": scan_token,
                    "raw_json": raw_json,
                    "created_at": now,
                    "updated_at": now,
                },
            )

            if price_value is not None:
                # v1.6: una riga per ogni rilevazione, non soltanto quando
                # il prezzo cambia. Serve per misurare permanenza e frequenza.
                conn.execute(
                    """
                    INSERT INTO price_observations(listing_id, price, observed_at, scan_token)
                    VALUES (?, ?, ?, ?)
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
        """Segna come missing gli annunci non osservati nella scansione.

        Il periodo di grazia evita di dichiarare scomparso un annuncio solo
        perché una singola pagina/fonte ha avuto un errore temporaneo.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(grace_hours, 0))
        ).isoformat()

        query = """
            UPDATE listings
            SET status = 'missing',
                removed_at = COALESCE(removed_at, ?),
                updated_at = ?
            WHERE status = 'active'
              AND COALESCE(last_scan_token, '') <> ?
              AND last_seen_at < ?
        """
        params: List[Any] = [utc_now_iso(), utc_now_iso(), scan_token, cutoff]

        if source:
            query += " AND source = ?"
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
        params: List[Any] = [product_key.lower(), cutoff]
        exclude_sql = ""
        if exclude_listing_id:
            exclude_sql = " AND id <> ?"
            params.append(exclude_listing_id)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, url, current_price AS price, first_seen_at,
                       last_seen_at, observations_count, location
                FROM listings
                WHERE product_key = ?
                  AND status = 'active'
                  AND current_price IS NOT NULL
                  AND last_seen_at >= ?
                  {exclude_sql}
                ORDER BY current_price ASC, last_seen_at DESC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_evaluation(self, listing_id: str, result: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO deal_evaluations (
                    listing_id, asking_price, estimated_sale_price,
                    maximum_buy_price, gross_margin, net_margin, roi,
                    comparables_count, confidence, decision,
                    rejection_reason, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM notifications WHERE listing_id = ?",
                (listing_id,),
            ).fetchone() is not None

    def mark_notified(self, listing_id: str, decision: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO notifications(listing_id, notified_at, decision)
                VALUES (?, ?, ?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    notified_at = excluded.notified_at,
                    decision = excluded.decision
                """,
                (listing_id, utc_now_iso(), decision),
            )

    def add_subscriber(self, chat_id: int) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO subscribers(chat_id, active, created_at, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET active = 1, updated_at = excluded.updated_at
                """,
                (int(chat_id), now, now),
            )

    def remove_subscriber(self, chat_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE subscribers SET active = 0, updated_at = ? WHERE chat_id = ?",
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
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT CASE WHEN product_key = '' THEN 'non identificato' ELSE product_key END AS product_key,
                       COUNT(*) AS count
                FROM listings
                GROUP BY CASE WHEN product_key = '' THEN 'non identificato' ELSE product_key END
                ORDER BY count DESC, product_key ASC
                LIMIT ?
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
                    HAVING COUNT(*) > 1
                )
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
                        "source_url": row.get("source_url", ""),
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
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("Migrazione history fallita: %s", exc)

        if state_file and Path(state_file).exists():
            try:
                state = json.loads(Path(state_file).read_text(encoding="utf-8"))
                notified = state.get("notified", []) if isinstance(state, dict) else []
                with self.connect() as conn:
                    for listing_id in notified:
                        exists = conn.execute(
                            "SELECT 1 FROM listings WHERE id = ?",
                            (str(listing_id),),
                        ).fetchone()
                        if exists:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO notifications(listing_id, notified_at, decision)
                                VALUES (?, ?, 'LEGACY')
                                """,
                                (str(listing_id), utc_now_iso()),
                            )
                            counts["notifications"] += 1
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Migrazione state fallita: %s", exc)

        if subscribers_file and Path(subscribers_file).exists():
            try:
                values = json.loads(Path(subscribers_file).read_text(encoding="utf-8"))
                for chat_id in values if isinstance(values, list) else []:
                    self.add_subscriber(int(chat_id))
                    counts["subscribers"] += 1
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("Migrazione subscribers fallita: %s", exc)

        with self.connect() as conn:
            conn.execute(
                "INSERT INTO migrations(name, applied_at, details) VALUES (?, ?, ?)",
                (migration_name, utc_now_iso(), json.dumps(counts)),
            )
        return counts
