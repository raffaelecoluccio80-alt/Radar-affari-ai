from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from radar_database import RadarDatabase


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[_/|]+", " ", text)
    text = re.sub(r"[^a-z0-9+.\- ]+", " ", text)
    return " ".join(text.split())


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize(value))


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _unique(values: Iterable[Any], *, minimum_length: int = 2) -> List[str]:
    result: List[str] = []
    seen = set()

    for raw in values:
        value = _normalize(raw)
        if len(_compact(value)) < minimum_length:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)

    return result


def _safe_identity_part(value: Any) -> bool:
    """Blocca output Vision compositi o descrittivi che avvelenerebbero il catalogo."""
    text = _clean(value)
    normalized = _normalize(text)

    if not normalized:
        return False

    forbidden = (
        ";",
        "(",
        ")",
        "/",
        "corpo ",
        "obiettivo ",
        "apparentemente",
        "non determin",
        "unknown",
        "unidentified",
    )
    if any(token in text.lower() for token in forbidden):
        return False

    # Marca e modello devono restare etichette corte, non descrizioni.
    if len(text) > 80 or len(normalized.split()) > 10:
        return False

    return True


def _canonical_key(
    brand: str,
    family: str,
    model: str,
    variant: str = "",
    storage_or_size: str = "",
) -> str:
    parts = [
        _normalize(brand),
        _normalize(family) or "product",
        _normalize(model),
    ]

    variant_norm = _normalize(variant)
    size_norm = _normalize(storage_or_size)

    if variant_norm and variant_norm not in _normalize(model):
        parts.append(variant_norm)
    if size_norm:
        parts.append(size_norm)

    return ":".join(part for part in parts if part)


def _candidate_id(
    listing_id: str,
    product_key: str,
    source_hash: str = "",
) -> str:
    raw = f"{listing_id}|{product_key}|{source_hash}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def build_aliases(
    *,
    brand: str,
    model: str,
    variant: str = "",
    storage_or_size: str = "",
    title: str = "",
    extra_aliases: Optional[Sequence[str]] = None,
) -> List[str]:
    """Genera solo alias identitari, evitando colori e descrizioni generiche."""
    brand_n = _normalize(brand)
    model_n = _normalize(model)
    variant_n = _normalize(variant)
    size_n = _normalize(storage_or_size)

    aliases: List[str] = [
        model_n,
        f"{brand_n} {model_n}",
        _compact(model_n),
        _compact(f"{brand_n} {model_n}"),
    ]

    if variant_n:
        aliases.extend(
            [
                f"{model_n} {variant_n}",
                f"{brand_n} {model_n} {variant_n}",
                _compact(f"{model_n} {variant_n}"),
            ]
        )

    if size_n:
        aliases.extend(
            [
                f"{model_n} {size_n}",
                f"{brand_n} {model_n} {size_n}",
                _compact(f"{model_n} {size_n}"),
            ]
        )

    if variant_n and size_n:
        aliases.extend(
            [
                f"{model_n} {variant_n} {size_n}",
                f"{brand_n} {model_n} {variant_n} {size_n}",
            ]
        )

    # Dal titolo prendiamo soltanto frasi che contengono marca o modello.
    title_n = _normalize(title)
    if title_n and (
        (brand_n and brand_n in title_n)
        or (model_n and model_n in title_n)
        or (_compact(model_n) and _compact(model_n) in _compact(title_n))
    ):
        # Limite prudente: il titolo intero non diventa alias se troppo lungo.
        if len(title_n) <= 90:
            aliases.append(title_n)

    aliases.extend(extra_aliases or [])
    return _unique(aliases, minimum_length=3)


class CatalogBuilder:
    """Apprendimento persistente e revisionabile del catalogo.

    Non modifica products.json durante l'esecuzione: su Railway il filesystem
    applicativo non è una fonte persistente affidabile. I candidati e gli alias
    appresi vengono salvati in PostgreSQL/SQLite e potranno essere approvati
    prima di entrare nel riconoscimento automatico.
    """

    def __init__(
        self,
        database: RadarDatabase,
        *,
        minimum_candidate_confidence: int = 85,
        require_precise: bool = True,
    ) -> None:
        self.database = database
        self.minimum_candidate_confidence = max(
            0, min(int(minimum_candidate_confidence), 100)
        )
        self.require_precise = bool(require_precise)
        self.initialize()

    @property
    def _placeholder(self) -> str:
        return "%s" if self.database.backend == "postgresql" else "?"

    def initialize(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS catalog_candidates (
                id TEXT PRIMARY KEY,
                listing_id TEXT NOT NULL,
                product_key TEXT NOT NULL,
                brand TEXT NOT NULL,
                family TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                variant TEXT NOT NULL DEFAULT '',
                storage_or_size TEXT NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                confidence INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'vision_fusion',
                source_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                rejection_reason TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_candidate_unique
                ON catalog_candidates(listing_id, product_key, source_hash)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_catalog_candidate_status
                ON catalog_candidates(status, confidence)
            """,
            """
            CREATE TABLE IF NOT EXISTS learned_catalog (
                product_key TEXT PRIMARY KEY,
                brand TEXT NOT NULL,
                family TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                variant TEXT NOT NULL DEFAULT '',
                storage_or_size TEXT NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                evidence_count INTEGER NOT NULL DEFAULT 1,
                confidence INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_learned_catalog_brand_model
                ON learned_catalog(brand, model)
            """,
        ]

        with self.database.connect() as conn:
            for statement in statements:
                conn.execute(statement)

    def validate_fused_result(
        self,
        fused_result: Dict[str, Any],
    ) -> tuple[bool, str]:
        confidence = int(fused_result.get("confidence") or 0)

        if confidence < self.minimum_candidate_confidence:
            return False, (
                f"confidence {confidence} inferiore a "
                f"{self.minimum_candidate_confidence}"
            )

        if self.require_precise and not fused_result.get("precise"):
            return False, "riconoscimento non classificato come precise"

        if fused_result.get("conflicts"):
            return False, "sono presenti conflitti testo/foto"

        brand = fused_result.get("brand")
        model = fused_result.get("model")

        if not _safe_identity_part(brand):
            return False, "marca composita, descrittiva o non valida"

        if not _safe_identity_part(model):
            return False, "modello composito, descrittivo o non valido"

        product_key = _clean(fused_result.get("product_key"))
        if not product_key or product_key == "unidentified":
            return False, "product_key non valido"

        fraud_signals = fused_result.get("fraud_signals") or []
        if fraud_signals:
            return False, "segnali di possibile contraffazione o frode"

        consistency = _normalize(
            fused_result.get("text_image_consistency")
        )
        if consistency == "inconsistent":
            return False, "testo e immagini incoerenti"

        return True, ""

    def propose(
        self,
        *,
        listing_id: str,
        fused_result: Dict[str, Any],
        title: str = "",
        source_hash: str = "",
        extra_aliases: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Salva un candidato prudente, senza approvarlo automaticamente."""
        valid, reason = self.validate_fused_result(fused_result)
        if not valid:
            return {
                "accepted": False,
                "reason": reason,
                "candidate_id": "",
                "status": "rejected_before_save",
            }

        brand = _clean(fused_result.get("brand"))
        family = _clean(
            fused_result.get("family")
            or fused_result.get("category")
        )
        model = _clean(fused_result.get("model"))
        variant = _clean(fused_result.get("variant"))
        storage_or_size = _clean(
            fused_result.get("storage_or_size")
        )

        product_key = _clean(fused_result.get("product_key")).lower()
        if not product_key:
            product_key = _canonical_key(
                brand, family, model, variant, storage_or_size
            )

        aliases = build_aliases(
            brand=brand,
            model=model,
            variant=variant,
            storage_or_size=storage_or_size,
            title=title,
            extra_aliases=extra_aliases,
        )

        candidate_id = _candidate_id(
            str(listing_id), product_key, source_hash
        )
        now = utc_now_iso()
        p = self._placeholder

        values = (
            candidate_id,
            str(listing_id),
            product_key,
            brand,
            family,
            model,
            variant,
            storage_or_size,
            _json_dumps(aliases),
            int(fused_result.get("confidence") or 0),
            "vision_fusion",
            str(source_hash or ""),
            "pending",
            "",
            _json_dumps(fused_result),
            now,
            now,
        )

        with self.database.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO catalog_candidates (
                    id, listing_id, product_key, brand, family, model,
                    variant, storage_or_size, aliases_json, confidence,
                    source, source_hash, status, rejection_reason,
                    raw_json, created_at, updated_at
                ) VALUES ({", ".join([p] * len(values))})
                ON CONFLICT(id) DO UPDATE SET
                    aliases_json = EXCLUDED.aliases_json,
                    confidence = EXCLUDED.confidence,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = EXCLUDED.updated_at
                """,
                values,
            )

        return {
            "accepted": True,
            "reason": "",
            "candidate_id": candidate_id,
            "product_key": product_key,
            "aliases": aliases,
            "status": "pending",
        }

    def approve(self, candidate_id: str) -> Dict[str, Any]:
        """Approva un candidato e lo fonde nel catalogo appreso."""
        p = self._placeholder

        with self.database.connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM catalog_candidates
                WHERE id = {p}
                """,
                (str(candidate_id),),
            ).fetchone()

            if row is None:
                raise KeyError("Candidato catalogo non trovato")

            candidate = dict(row)
            try:
                candidate_aliases = json.loads(
                    candidate.get("aliases_json") or "[]"
                )
            except json.JSONDecodeError:
                candidate_aliases = []

            existing = conn.execute(
                f"""
                SELECT *
                FROM learned_catalog
                WHERE product_key = {p}
                """,
                (candidate["product_key"],),
            ).fetchone()

            now = utc_now_iso()

            if existing is None:
                aliases = _unique(candidate_aliases, minimum_length=3)
                values = (
                    candidate["product_key"],
                    candidate["brand"],
                    candidate["family"],
                    candidate["model"],
                    candidate["variant"],
                    candidate["storage_or_size"],
                    _json_dumps(aliases),
                    1,
                    int(candidate["confidence"] or 0),
                    now,
                    now,
                    now,
                )
                conn.execute(
                    f"""
                    INSERT INTO learned_catalog (
                        product_key, brand, family, model, variant,
                        storage_or_size, aliases_json, evidence_count,
                        confidence, first_seen_at, last_seen_at, updated_at
                    ) VALUES ({", ".join([p] * len(values))})
                    """,
                    values,
                )
                evidence_count = 1
            else:
                existing = dict(existing)
                try:
                    old_aliases = json.loads(
                        existing.get("aliases_json") or "[]"
                    )
                except json.JSONDecodeError:
                    old_aliases = []

                aliases = _unique(
                    list(old_aliases) + list(candidate_aliases),
                    minimum_length=3,
                )
                evidence_count = int(
                    existing.get("evidence_count") or 0
                ) + 1
                confidence = max(
                    int(existing.get("confidence") or 0),
                    int(candidate.get("confidence") or 0),
                )

                conn.execute(
                    f"""
                    UPDATE learned_catalog
                    SET aliases_json = {p},
                        evidence_count = {p},
                        confidence = {p},
                        last_seen_at = {p},
                        updated_at = {p}
                    WHERE product_key = {p}
                    """,
                    (
                        _json_dumps(aliases),
                        evidence_count,
                        confidence,
                        now,
                        now,
                        candidate["product_key"],
                    ),
                )

            conn.execute(
                f"""
                UPDATE catalog_candidates
                SET status = 'approved',
                    rejection_reason = '',
                    updated_at = {p}
                WHERE id = {p}
                """,
                (now, str(candidate_id)),
            )

        return {
            "candidate_id": str(candidate_id),
            "product_key": candidate["product_key"],
            "status": "approved",
            "evidence_count": evidence_count,
            "aliases": aliases,
        }

    def reject(self, candidate_id: str, reason: str) -> bool:
        p = self._placeholder
        now = utc_now_iso()

        with self.database.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE catalog_candidates
                SET status = 'rejected',
                    rejection_reason = {p},
                    updated_at = {p}
                WHERE id = {p}
                """,
                (
                    _clean(reason)[:1000],
                    now,
                    str(candidate_id),
                ),
            )
            return cursor.rowcount > 0

    def pending(self, limit: int = 20) -> List[Dict[str, Any]]:
        p = self._placeholder
        with self.database.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, listing_id, product_key, brand, family, model,
                       variant, storage_or_size, aliases_json, confidence,
                       created_at
                FROM catalog_candidates
                WHERE status = 'pending'
                ORDER BY confidence DESC, created_at ASC
                LIMIT {p}
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["aliases"] = json.loads(
                    item.pop("aliases_json") or "[]"
                )
            except json.JSONDecodeError:
                item["aliases"] = []
            results.append(item)
        return results

    def learned_products(self, limit: int = 1000) -> List[Dict[str, Any]]:
        p = self._placeholder
        with self.database.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT product_key, brand, family, model, variant,
                       storage_or_size, aliases_json, evidence_count,
                       confidence, first_seen_at, last_seen_at
                FROM learned_catalog
                ORDER BY evidence_count DESC, confidence DESC
                LIMIT {p}
                """,
                (max(1, min(int(limit), 10000)),),
            ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["aliases"] = json.loads(
                    item.pop("aliases_json") or "[]"
                )
            except json.JSONDecodeError:
                item["aliases"] = []
            results.append(item)
        return results

    def stats(self) -> Dict[str, int]:
        with self.database.connect() as conn:
            candidate_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
                        AS pending,
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END)
                        AS approved,
                    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END)
                        AS rejected
                FROM catalog_candidates
                """
            ).fetchone()

            learned_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS learned_products,
                    COALESCE(SUM(evidence_count), 0) AS total_evidence
                FROM learned_catalog
                """
            ).fetchone()

        return {
            "candidates_total": int(candidate_row["total"] or 0),
            "pending": int(candidate_row["pending"] or 0),
            "approved": int(candidate_row["approved"] or 0),
            "rejected": int(candidate_row["rejected"] or 0),
            "learned_products": int(
                learned_row["learned_products"] or 0
            ),
            "total_evidence": int(
                learned_row["total_evidence"] or 0
            ),
        }
