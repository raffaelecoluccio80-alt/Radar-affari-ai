from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9+]+", " ", text)
    return " ".join(text.split())


def _is_empty(value: Any) -> bool:
    return _normalize(value) in {
        "",
        "unknown",
        "non determinato",
        "non determinata",
        "non identificato",
        "unidentified",
        "n d",
        "nd",
    }


def _same(a: Any, b: Any) -> bool:
    if _is_empty(a) or _is_empty(b):
        return False
    return _normalize(a) == _normalize(b)


def _contains_equivalent(a: Any, b: Any) -> bool:
    """Confronto prudente per valori come 'Turbo Levo' e
    'Turbo Levo Comp Carbon'."""
    if _is_empty(a) or _is_empty(b):
        return False

    left = _normalize(a)
    right = _normalize(b)

    if left == right:
        return True

    # Evita equivalenze per stringhe troppo corte, ad esempio "Pro".
    if min(len(left), len(right)) < 4:
        return False

    return left in right or right in left


def _normalize_storage(value: Any) -> str:
    text = _normalize(value)
    if not text:
        return ""

    match = re.search(r"\b(64|128|256|512|1024)\s*(gb|giga|tb)?\b", text)
    if not match:
        return _clean(value)

    number = int(match.group(1))
    if number == 1024:
        return "1TB"
    return f"{number}GB"


def _normalize_size(value: Any) -> str:
    text = _normalize(value)
    if not text:
        return ""

    # Taglie bici comuni.
    match = re.search(r"\b(xxs|xs|s|m|l|xl|xxl)\b", text)
    if match:
        return match.group(1).upper()

    # Misure ruota.
    wheel = re.search(r"\b(26|27(?:[.,]5)?|29)\b", text)
    if wheel:
        return wheel.group(1).replace(",", '"').replace(".5", '.5"') + (
            '"' if "." not in wheel.group(1) else ""
        )

    return _clean(value)


def _normalize_storage_or_size(value: Any, category: Any) -> str:
    category_norm = _normalize(category)
    if any(token in category_norm for token in ("phone", "smartphone", "telefonia", "iphone")):
        return _normalize_storage(value)
    return _normalize_size(value)


def _confidence(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), 100))
    except (TypeError, ValueError):
        return 0


def _build_product_key(
    brand: str,
    category: str,
    model: str,
    variant: str = "",
    storage_or_size: str = "",
) -> str:
    brand_key = _normalize(brand)
    category_key = _normalize(category) or "product"
    model_key = _normalize(model)
    variant_key = _normalize(variant)
    size_key = _normalize(storage_or_size)

    if not brand_key or not model_key:
        return "unidentified"

    parts = [brand_key, category_key, model_key]
    if variant_key and variant_key not in model_key:
        parts.append(variant_key)
    if size_key:
        parts.append(size_key)

    return ":".join(parts)


def _select_value(
    field: str,
    text_value: Any,
    vision_value: Any,
    text_confidence: int,
    vision_confidence: int,
    *,
    allow_containment: bool = False,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Restituisce valore, fonte e conflitto eventuale."""

    text_clean = _clean(text_value)
    vision_clean = _clean(vision_value)

    if _is_empty(text_clean) and _is_empty(vision_clean):
        return "", "none", None

    if _is_empty(text_clean):
        return vision_clean, "vision", None

    if _is_empty(vision_clean):
        return text_clean, "text", None

    equivalent = (
        _contains_equivalent(text_clean, vision_clean)
        if allow_containment
        else _same(text_clean, vision_clean)
    )

    if equivalent:
        # Conserva il valore più specifico.
        chosen = max((text_clean, vision_clean), key=len)
        return chosen, "text+vision", None

    conflict = {
        "field": field,
        "text_value": text_clean,
        "vision_value": vision_clean,
        "text_confidence": text_confidence,
        "vision_confidence": vision_confidence,
    }

    # In caso di conflitto il vincitore deve avere un vantaggio netto.
    if vision_confidence >= text_confidence + 15:
        return vision_clean, "vision_over_text", conflict

    if text_confidence >= vision_confidence + 15:
        return text_clean, "text_over_vision", conflict

    # Conflitto irrisolto: non inventiamo.
    return "", "conflict", conflict


def fuse_recognition(
    text_result: Dict[str, Any],
    vision_result: Dict[str, Any],
    *,
    minimum_usable_confidence: int = 70,
    minimum_precise_confidence: int = 85,
) -> Dict[str, Any]:
    """Fonde riconoscimento testuale e visivo in modo prudente.

    Regole:
    - testo e Vision concordi: confidence aumenta;
    - una sola fonte disponibile: viene usata con penalizzazione;
    - conflitto forte non risolto: identità non precisa;
    - brand e modello sono obbligatori per creare un product_key preciso.
    """

    text_conf = _confidence(
        text_result.get("recognition_confidence")
        or text_result.get("confidence")
    )
    vision_conf = _confidence(vision_result.get("recognition_confidence"))

    conflicts: List[Dict[str, Any]] = []
    sources: Dict[str, str] = {}

    category, sources["category"], conflict = _select_value(
        "category",
        text_result.get("category") or text_result.get("family"),
        vision_result.get("category"),
        text_conf,
        vision_conf,
        allow_containment=True,
    )
    if conflict:
        conflicts.append(conflict)

    brand, sources["brand"], conflict = _select_value(
        "brand",
        text_result.get("brand"),
        vision_result.get("brand"),
        text_conf,
        vision_conf,
    )
    if conflict:
        conflicts.append(conflict)

    model, sources["model"], conflict = _select_value(
        "model",
        text_result.get("model"),
        vision_result.get("model"),
        text_conf,
        vision_conf,
        allow_containment=True,
    )
    if conflict:
        conflicts.append(conflict)

    variant, sources["variant"], conflict = _select_value(
        "variant",
        text_result.get("variant"),
        vision_result.get("variant"),
        text_conf,
        vision_conf,
        allow_containment=True,
    )
    if conflict:
        conflicts.append(conflict)

    text_storage = (
        text_result.get("storage")
        or text_result.get("storage_or_size")
        or text_result.get("size")
    )
    vision_storage = vision_result.get("storage_or_size")

    normalized_text_storage = _normalize_storage_or_size(text_storage, category)
    normalized_vision_storage = _normalize_storage_or_size(vision_storage, category)

    storage_or_size, sources["storage_or_size"], conflict = _select_value(
        "storage_or_size",
        normalized_text_storage,
        normalized_vision_storage,
        text_conf,
        vision_conf,
    )
    if conflict:
        conflicts.append(conflict)

    estimated_year, sources["estimated_year"], conflict = _select_value(
        "estimated_year",
        text_result.get("year") or text_result.get("estimated_year"),
        vision_result.get("estimated_year"),
        text_conf,
        vision_conf,
    )
    if conflict:
        conflicts.append(conflict)

    # Calcolo confidence complessiva.
    both_available = text_conf > 0 and vision_conf > 0
    agreement_fields = sum(
        1 for source in sources.values() if source == "text+vision"
    )
    unresolved_conflicts = sum(
        1 for source in sources.values() if source == "conflict"
    )

    if both_available:
        base_confidence = round((text_conf * 0.45) + (vision_conf * 0.55))
    else:
        base_confidence = max(text_conf, vision_conf) - 8

    base_confidence += min(agreement_fields * 3, 12)
    base_confidence -= unresolved_conflicts * 20
    base_confidence -= max(0, len(conflicts) - unresolved_conflicts) * 5

    if _is_empty(brand):
        base_confidence -= 25
    if _is_empty(model):
        base_confidence -= 35

    confidence = max(0, min(int(base_confidence), 100))

    product_key = _build_product_key(
        brand=brand,
        category=category,
        model=model,
        variant=variant,
        storage_or_size=storage_or_size,
    )

    precise = (
        product_key != "unidentified"
        and confidence >= minimum_precise_confidence
        and unresolved_conflicts == 0
    )
    usable = (
        product_key != "unidentified"
        and confidence >= minimum_usable_confidence
    )

    if precise:
        status = "precise"
    elif usable:
        status = "usable_with_verification"
    elif conflicts:
        status = "conflict"
    else:
        status = "insufficient"

    visible_defects = vision_result.get("visible_defects") or []
    fraud_signals = vision_result.get("counterfeit_or_fraud_signals") or []

    return {
        "category": category,
        "brand": brand,
        "model": model,
        "variant": variant,
        "storage_or_size": storage_or_size,
        "estimated_year": estimated_year,
        "product_key": product_key,
        "confidence": confidence,
        "status": status,
        "precise": precise,
        "usable": usable,
        "source": (
            "text+vision"
            if text_conf > 0 and vision_conf > 0
            else "vision"
            if vision_conf > 0
            else "text"
        ),
        "field_sources": sources,
        "conflicts": conflicts,
        "text_confidence": text_conf,
        "vision_confidence": vision_conf,
        "visible_condition": _clean(vision_result.get("visible_condition")),
        "visible_defects": list(visible_defects),
        "visible_accessories": list(
            vision_result.get("visible_accessories") or []
        ),
        "fraud_signals": list(fraud_signals),
        "text_image_consistency": _clean(
            vision_result.get("text_image_consistency")
        ),
        "notes": _clean(vision_result.get("notes")),
    }


def should_use_vision(
    item: Dict[str, Any],
    *,
    maximum_text_confidence_without_vision: int = 84,
) -> bool:
    """Decide se vale la pena chiamare la Vision.

    La Vision viene proposta solo per annunci:
    - pertinenti;
    - con URL e prezzo;
    - non esclusi;
    - non già riconosciuti con alta confidence.
    """

    if item.get("excluded"):
        return False
    if not item.get("relevant") and not item.get("matched"):
        return False
    if not _clean(item.get("url")):
        return False
    if not isinstance(item.get("price"), (int, float)):
        return False

    key = _clean(item.get("product_key"))
    confidence = _confidence(item.get("recognition_confidence"))

    unidentified = not key or key == "unidentified"
    low_confidence = confidence <= maximum_text_confidence_without_vision

    return unidentified or low_confidence
