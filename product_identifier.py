from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List


CATALOG_PATH = Path(__file__).with_name("products.json")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


@lru_cache(maxsize=1)
def load_catalog() -> Dict[str, Any]:
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Catalogo prodotti non leggibile: {exc}") from exc


def _matches_brand(text: str, aliases: List[str]) -> bool:
    return any(
        re.search(rf"\b{re.escape(alias.lower())}\b", text)
        for alias in aliases
    )


def identify_product(text: str) -> Dict[str, Any]:
    """Identifica prodotto e costruisce una chiave di confronto precisa.

    Restituisce sempre la stessa struttura. Una chiave economica viene creata
    soltanto quando il modello è identificato e gli attributi obbligatori
    (per esempio la memoria degli iPhone) sono presenti.
    """
    lowered = normalize_text(text)
    empty = {
        "brand": "",
        "family": "",
        "model": "",
        "variant": "",
        "storage": "",
        "year": "",
        "product_key": "",
        "recognition_confidence": 0,
    }

    if not lowered:
        return empty

    catalog = load_catalog()

    for family in catalog.get("families", []):
        aliases = [str(a) for a in family.get("brand_aliases", [])]
        if not _matches_brand(lowered, aliases):
            continue

        for model in family.get("models", []):
            model_name = str(model.get("name") or "")
            patterns = model.get("patterns", [])

            if not any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
                continue

            storage = ""
            attributes = family.get("attributes", {})
            storage_pattern = attributes.get("storage_pattern")
            if storage_pattern:
                match = re.search(storage_pattern, lowered, flags=re.IGNORECASE)
                if match:
                    storage = f"{match.group(1)}GB"

            year_match = re.search(r"\b(20(?:1[5-9]|2[0-9]))\b", lowered)
            year = year_match.group(1) if year_match else ""

            brand = str(family.get("brand") or "")
            family_name = str(family.get("family") or "")
            required_storage = bool(
                attributes.get("storage_required_for_pricing", False)
            )

            key_parts = [brand, family_name, model_name]
            if storage:
                key_parts.append(storage)

            precise = not required_storage or bool(storage)
            product_key = (
                ":".join(part.strip().lower() for part in key_parts if part)
                if precise else ""
            )

            confidence = 95 if precise else 72

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

    return empty
