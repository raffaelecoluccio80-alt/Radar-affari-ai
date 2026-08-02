from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List


CATALOG_PATH = Path(__file__).with_name("products.json")


def normalize_text(value: str) -> str:
    """Normalizza il testo mantenendo separatori utili al riconoscimento."""
    value = (value or "").lower()
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[_/|]+", " ", value)
    value = re.sub(r"(?<=\d)-(?=\d)", "-", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def compact_text(value: str) -> str:
    """Versione compatta usata per alias come iphone15pro e 15pro."""
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


@lru_cache(maxsize=1)
def load_catalog() -> Dict[str, Any]:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Catalogo prodotti non leggibile: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Catalogo prodotti non valido: radice JSON non oggetto.")

    families = data.get("families")
    if not isinstance(families, list):
        raise RuntimeError("Catalogo prodotti non valido: campo 'families' assente.")

    return data


def clear_catalog_cache() -> None:
    """Forza la rilettura di products.json senza riavviare il processo."""
    load_catalog.cache_clear()


def _safe_search(pattern: str, text: str) -> bool:
    try:
        return bool(re.search(pattern, text, flags=re.IGNORECASE))
    except re.error:
        return False


def _matches_brand(text: str, aliases: Iterable[str]) -> bool:
    compact = compact_text(text)

    for raw_alias in aliases:
        alias = normalize_text(str(raw_alias))
        if not alias:
            continue

        if re.search(rf"\b{re.escape(alias)}\b", text):
            return True

        alias_compact = compact_text(alias)
        if len(alias_compact) >= 3 and alias_compact in compact:
            return True

    return False


def _matches_model(text: str, model: Dict[str, Any]) -> bool:
    patterns = model.get("patterns", [])
    for pattern in patterns:
        if _safe_search(str(pattern), text):
            return True

    compact = compact_text(text)
    aliases = model.get("aliases", [])

    for raw_alias in aliases:
        alias = normalize_text(str(raw_alias))
        if not alias:
            continue

        # Alias con confini di parola: "iphone 15 pro", "15 pro".
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text):
            return True

        # Alias compatti: "iphone15pro", "15pro".
        alias_compact = compact_text(alias)
        if len(alias_compact) >= 4 and alias_compact in compact:
            return True

    return False


def _extract_storage(text: str, attributes: Dict[str, Any]) -> str:
    aliases = attributes.get("storage_aliases", {})
    compact = compact_text(text)

    if isinstance(aliases, dict):
        # Prima controlliamo gli alias specifici, ad esempio 1 TB.
        for raw_alias, raw_value in aliases.items():
            alias_compact = compact_text(str(raw_alias))
            if alias_compact and alias_compact in compact:
                try:
                    return f"{int(raw_value)}GB"
                except (TypeError, ValueError):
                    continue

    storage_pattern = attributes.get("storage_pattern")
    if storage_pattern:
        try:
            match = re.search(str(storage_pattern), text, flags=re.IGNORECASE)
        except re.error:
            match = None

        if match:
            try:
                value = int(match.group(1))
                return f"{value}GB"
            except (IndexError, TypeError, ValueError):
                pass

    # Fallback prudente per scritture comuni non intercettate dal catalogo.
    fallback = re.search(
        r"(?<!\d)(64|128|256|512|1024)\s*(?:gb|giga)\b",
        text,
        flags=re.IGNORECASE,
    )
    if fallback:
        return f"{int(fallback.group(1))}GB"

    if re.search(r"(?<!\d)1\s*tb\b", text, flags=re.IGNORECASE):
        return "1024GB"

    return ""


def _empty_result() -> Dict[str, Any]:
    return {
        "brand": "",
        "family": "",
        "model": "",
        "variant": "",
        "storage": "",
        "year": "",
        "product_key": "",
        "recognition_confidence": 0,
    }


def identify_product(text: str) -> Dict[str, Any]:
    """Identifica marca, famiglia, modello e memoria.

    La chiave economica viene creata solo quando sono presenti gli attributi
    obbligatori per il confronto prezzi, come la memoria per gli iPhone.
    """
    lowered = normalize_text(text)
    if not lowered:
        return _empty_result()

    catalog = load_catalog()

    for family in catalog.get("families", []):
        if not isinstance(family, dict):
            continue

        brand_aliases = [
            str(alias)
            for alias in family.get("brand_aliases", [])
            if str(alias).strip()
        ]

        if brand_aliases and not _matches_brand(lowered, brand_aliases):
            continue

        models = family.get("models", [])
        if not isinstance(models, list):
            continue

        # Manteniamo l'ordine del catalogo: i modelli più specifici devono
        # precedere quelli generici (es. Pro Max prima di Pro e modello base).
        for model in models:
            if not isinstance(model, dict):
                continue

            if not _matches_model(lowered, model):
                continue

            model_name = str(model.get("name") or "").strip()
            attributes = family.get("attributes", {})
            if not isinstance(attributes, dict):
                attributes = {}

            storage = _extract_storage(lowered, attributes)

            year_match = re.search(r"\b(20(?:1[5-9]|2[0-9]))\b", lowered)
            year = year_match.group(1) if year_match else ""

            brand = str(family.get("brand") or "").strip()
            family_name = str(family.get("family") or "").strip()

            required_storage = bool(
                attributes.get("storage_required_for_pricing", False)
            )
            precise = not required_storage or bool(storage)

            key_parts = [brand, family_name, model_name]
            if storage:
                key_parts.append(storage)

            product_key = (
                ":".join(
                    part.strip().lower()
                    for part in key_parts
                    if part and part.strip()
                )
                if precise
                else ""
            )

            pattern_match = any(
                _safe_search(str(pattern), lowered)
                for pattern in model.get("patterns", [])
            )

            if precise and pattern_match:
                confidence = 97
            elif precise:
                confidence = 92
            elif pattern_match:
                confidence = 74
            else:
                confidence = 70

            return {
                "brand": brand,
                "family": family_name,
                "model": model_name,
                "variant": "",
                "storage": storage,
                "year": year,
                "product_key": product_key,
                "recognition_confidence": confidence,
            }

    return _empty_result()
