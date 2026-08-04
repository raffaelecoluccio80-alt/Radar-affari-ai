from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


CATALOG_PATH = Path(__file__).with_name("products.json")


def normalize_text(value: str) -> str:
    """Normalizza testo, accenti e separatori senza perdere dati utili."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.lower().replace("\u00a0", " ")
    value = re.sub(r"[_/|]+", " ", value)
    value = re.sub(r"(?<=\d)[,](?=\d)", ".", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def compact_text(value: str) -> str:
    """Versione compatta: 'iPhone 15 Pro' -> 'iphone15pro'."""
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
    except (re.error, TypeError):
        return False


def _phrase_match(alias: str, text: str) -> bool:
    """Match di una frase con confini alfanumerici."""
    alias = normalize_text(alias)
    if not alias:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
            text,
            flags=re.IGNORECASE,
        )
    )


def _tokenize(value: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", normalize_text(value))


def _fuzzy_phrase_score(alias: str, text: str) -> int:
    """Score 0-100 per piccoli errori di battitura.

    È volutamente prudente: lavora solo su finestre con lo stesso numero
    di token e viene usato soltanto quando il marchio è già riconosciuto.
    """
    alias_tokens = _tokenize(alias)
    text_tokens = _tokenize(text)
    if not alias_tokens or len(alias_tokens) > len(text_tokens):
        return 0

    alias_compact = "".join(alias_tokens)
    if len(alias_compact) < 5:
        return 0

    best = 0.0
    width = len(alias_tokens)

    for index in range(0, len(text_tokens) - width + 1):
        window = text_tokens[index:index + width]
        window_compact = "".join(window)

        ratio = SequenceMatcher(
            None, alias_compact, window_compact, autojunk=False
        ).ratio()

        # I numeri di modello devono coincidere: evita 14/15, 5/6 ecc.
        alias_numbers = re.findall(r"\d+", alias_compact)
        window_numbers = re.findall(r"\d+", window_compact)
        if alias_numbers and alias_numbers != window_numbers:
            continue

        best = max(best, ratio)

    return int(round(best * 100))


def _matches_brand(text: str, aliases: Iterable[str]) -> bool:
    compact = compact_text(text)

    for raw_alias in aliases:
        alias = normalize_text(str(raw_alias))
        if not alias:
            continue

        if _phrase_match(alias, text):
            return True

        alias_compact = compact_text(alias)
        if len(alias_compact) >= 3 and alias_compact in compact:
            return True

    return False


def _model_match_score(
    text: str,
    model: Dict[str, Any],
    *,
    allow_fuzzy: bool = False,
) -> Tuple[int, str]:
    """Restituisce punteggio e tipo di match del modello."""
    compact = compact_text(text)
    best_score = 0
    best_source = ""

    for pattern in model.get("patterns", []):
        if _safe_search(str(pattern), text):
            # I pattern del catalogo sono la prova più affidabile.
            score = 100 + min(len(str(pattern)), 40)
            if score > best_score:
                best_score = score
                best_source = "pattern"

    for raw_alias in model.get("aliases", []):
        alias = normalize_text(str(raw_alias))
        if not alias:
            continue

        if _phrase_match(alias, text):
            score = 90 + min(len(alias), 35)
            if score > best_score:
                best_score = score
                best_source = "alias"

        alias_compact = compact_text(alias)
        if len(alias_compact) >= 4 and alias_compact in compact:
            score = 82 + min(len(alias_compact), 35)
            if score > best_score:
                best_score = score
                best_source = "compact_alias"

        if allow_fuzzy and len(alias_compact) >= 5:
            fuzzy = _fuzzy_phrase_score(alias, text)
            if fuzzy >= 90:
                score = 68 + min(fuzzy - 90, 10) + min(len(alias_compact), 20)
                if score > best_score:
                    best_score = score
                    best_source = "fuzzy_alias"

    # Fallback sul nome canonico, utile anche se il catalogo non ha aliases.
    model_name = normalize_text(str(model.get("name") or ""))
    if model_name:
        if _phrase_match(model_name, text):
            score = 80 + min(len(model_name), 35)
            if score > best_score:
                best_score = score
                best_source = "model_name"

        model_compact = compact_text(model_name)
        if len(model_compact) >= 4 and model_compact in compact:
            score = 72 + min(len(model_compact), 35)
            if score > best_score:
                best_score = score
                best_source = "compact_model_name"

        if allow_fuzzy and len(model_compact) >= 5:
            fuzzy = _fuzzy_phrase_score(model_name, text)
            if fuzzy >= 92:
                score = 62 + min(fuzzy - 92, 8) + min(len(model_compact), 18)
                if score > best_score:
                    best_score = score
                    best_source = "fuzzy_model_name"

    return best_score, best_source


def _extract_storage(text: str, attributes: Dict[str, Any]) -> str:
    """Estrae memoria evitando di confondere RAM, batteria o altri numeri."""
    normalized = normalize_text(text)
    compact = compact_text(normalized)

    aliases = attributes.get("storage_aliases", {})
    if isinstance(aliases, dict):
        # Alias più lunghi prima: 1024gb precede 1tb.
        ordered_aliases = sorted(
            aliases.items(),
            key=lambda item: len(compact_text(str(item[0]))),
            reverse=True,
        )
        for raw_alias, raw_value in ordered_aliases:
            alias_compact = compact_text(str(raw_alias))
            if alias_compact and alias_compact in compact:
                try:
                    return f"{int(raw_value)}GB"
                except (TypeError, ValueError):
                    continue

    storage_pattern = attributes.get("storage_pattern")
    if storage_pattern:
        try:
            match = re.search(str(storage_pattern), normalized, flags=re.IGNORECASE)
        except re.error:
            match = None

        if match:
            try:
                value = int(match.group(1))
                return f"{value}GB"
            except (IndexError, TypeError, ValueError):
                pass

    # Formati comuni: 256 GB, 256gb, 256 giga, 1 TB.
    fallback = re.search(
        r"(?<!\d)(64|128|256|512|1024)\s*(?:gb|giga)\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if fallback:
        return f"{int(fallback.group(1))}GB"

    if re.search(r"(?<!\d)1\s*tb\b", normalized, flags=re.IGNORECASE):
        return "1024GB"

    # Formati compatti frequenti nei titoli: iphone15pro256gb.
    compact_match = re.search(r"(64|128|256|512|1024)(?:gb|giga)", compact)
    if compact_match:
        return f"{int(compact_match.group(1))}GB"

    return ""


def _extract_year(text: str) -> str:
    match = re.search(r"\b(20(?:1[5-9]|2[0-9]))\b", text)
    return match.group(1) if match else ""


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


def _family_can_match_without_brand(
    family: Dict[str, Any],
    model_name: str,
    text: str,
    storage: str,
    model_score: int,
) -> bool:
    """Consente titoli abbreviati solo quando il segnale è abbastanza forte."""
    family_name = normalize_text(str(family.get("family") or ""))
    brand = normalize_text(str(family.get("brand") or ""))

    # iPhone abbreviati: "15 Pro 256GB". Richiediamo variante distintiva
    # oppure memoria esplicita, per non scambiare numeri generici per modelli.
    if brand == "apple" and family_name == "iphone":
        distinctive = bool(
            re.search(
                r"\b(?:pro|max|plus|mini|se|xr|xs)\b",
                normalize_text(model_name),
            )
        )
        return model_score >= 92 and (distinctive or bool(storage))

    # Altri prodotti possono essere riconosciuti senza brand solo con match
    # molto forte e nome sufficientemente distintivo (es. EP-2 Pro).
    return model_score >= 110 and len(compact_text(model_name)) >= 5


def identify_product(text: str) -> Dict[str, Any]:
    """Identifica marca, famiglia, modello e memoria.

    Compatibile con l'interfaccia precedente. Analizza tutto il testo ricevuto
    (titolo e descrizione, se app.py li concatena) e sceglie il candidato con
    il punteggio più alto, invece di fermarsi al primo match generico.
    """
    lowered = normalize_text(text)
    if not lowered:
        return _empty_result()

    catalog = load_catalog()
    candidates: List[Dict[str, Any]] = []

    for family_index, family in enumerate(catalog.get("families", [])):
        if not isinstance(family, dict):
            continue

        brand_aliases = [
            str(alias)
            for alias in family.get("brand_aliases", [])
            if str(alias).strip()
        ]
        brand_match = _matches_brand(lowered, brand_aliases)

        attributes = family.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}

        storage = _extract_storage(lowered, attributes)
        models = family.get("models", [])
        if not isinstance(models, list):
            continue

        for model_index, model in enumerate(models):
            if not isinstance(model, dict):
                continue

            model_score, match_source = _model_match_score(
                lowered,
                model,
                allow_fuzzy=brand_match,
            )
            if model_score <= 0:
                continue

            model_name = str(model.get("name") or "").strip()
            if not brand_match and not _family_can_match_without_brand(
                family=family,
                model_name=model_name,
                text=lowered,
                storage=storage,
                model_score=model_score,
            ):
                continue

            # Premiamo presenza del brand e modelli specifici/lunghi.
            total_score = model_score
            if brand_match:
                total_score += 45
            if storage:
                total_score += 12
            total_score += min(len(compact_text(model_name)), 25)

            candidates.append(
                {
                    "family": family,
                    "model": model,
                    "attributes": attributes,
                    "storage": storage,
                    "brand_match": brand_match,
                    "match_source": match_source,
                    "score": total_score,
                    "family_index": family_index,
                    "model_index": model_index,
                }
            )

    if not candidates:
        return _empty_result()

    # Punteggio maggiore; a parità, modello più specifico e poi ordine catalogo.
    candidates.sort(
        key=lambda item: (
            item["score"],
            len(compact_text(str(item["model"].get("name") or ""))),
            -item["family_index"],
            -item["model_index"],
        ),
        reverse=True,
    )
    winner = candidates[0]

    family = winner["family"]
    model = winner["model"]
    attributes = winner["attributes"]
    storage = winner["storage"]

    brand = str(family.get("brand") or "").strip()
    family_name = str(family.get("family") or "").strip()
    model_name = str(model.get("name") or "").strip()
    year = _extract_year(lowered)

    required_storage = bool(attributes.get("storage_required_for_pricing", False))
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

    if precise and winner["brand_match"] and winner["match_source"] == "pattern":
        confidence = 98
    elif precise and winner["brand_match"] and winner["match_source"] in {
        "fuzzy_alias", "fuzzy_model_name"
    }:
        confidence = 88
    elif precise and winner["brand_match"]:
        confidence = 95
    elif precise:
        confidence = 90
    elif winner["brand_match"] and winner["match_source"] == "pattern":
        confidence = 78
    else:
        confidence = 72

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
