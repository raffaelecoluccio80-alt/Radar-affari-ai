import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from radar_database import RadarDatabase
from product_identifier import identify_product as catalog_identify_product
from visual_analyzer import analyze_listing
from knowledge_engine import build_knowledge_report


# ============================================================
# CONFIGURAZIONE
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("radar-affari")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_MINUTES = max(int(os.getenv("CHECK_MINUTES", "15")), 5)

MIN_MARGIN_EURO = float(os.getenv("MIN_MARGIN_EURO", "80"))
MIN_ROI_PERCENT = float(os.getenv("MIN_ROI_PERCENT", "20"))
MAX_ALERTS_PER_SCAN = max(int(os.getenv("MAX_ALERTS_PER_SCAN", "30")), 1)

# Parametri del motore di valutazione v1.5.
# Il Radar usa solo confronti recenti, elimina prezzi anomali e assegna
# un livello di attendibilità alla stima.
MARKET_LOOKBACK_DAYS = max(int(os.getenv("MARKET_LOOKBACK_DAYS", "90")), 7)
MIN_COMPARABLES = max(int(os.getenv("MIN_COMPARABLES", "3")), 3)
GOOD_COMPARABLES = max(int(os.getenv("GOOD_COMPARABLES", "12")), MIN_COMPARABLES)
HIGH_COMPARABLES = max(int(os.getenv("HIGH_COMPARABLES", "25")), GOOD_COMPARABLES)
MIN_CONFIDENCE_SCORE = min(
    max(int(os.getenv("MIN_CONFIDENCE_SCORE", "45")), 0),
    100,
)

MIN_MARKET_DISCOUNT_PERCENT = float(
    os.getenv("MIN_MARKET_DISCOUNT_PERCENT", "12")
)

# Price Engine Pro v1.8
# Limita l'impatto dei comparabili molto lontani dal prezzo centrale
# e richiede un vantaggio reale rispetto alla fascia bassa del mercato.
MAX_COMPARABLE_DEVIATION_PERCENT = float(
    os.getenv("MAX_COMPARABLE_DEVIATION_PERCENT", "45")
)
MIN_DISCOUNT_VS_LOW_PERCENT = float(
    os.getenv("MIN_DISCOUNT_VS_LOW_PERCENT", "5")
)

KEYWORDS = [
    value.strip().lower()
    for value in os.getenv(
        "KEYWORDS",
        (
            "hilti,festool,makita,bosch professional,leica,topcon,trimble,"
            "bici elettrica,ebike,engwe,ado,haibike,cube,specialized,trek,"
            "faema,la marzocco,rational,berkel,abbattitore,impastatrice,"
            "dyson,folletto,iphone,ipad,macbook,playstation,ps5,xbox,"
            "nintendo switch"
        ),
    ).split(",")
    if value.strip()
]

# Fonti mirate: evitiamo la pagina generica "vendita" e leggiamo
# direttamente le categorie dove è più probabile trovare prodotti utili.
DEFAULT_SOURCE_URLS = [
    "https://www.subito.it/annunci-piemonte/vendita/biciclette/",
    "https://www.subito.it/annunci-piemonte/vendita/telefonia/",
    "https://www.subito.it/annunci-piemonte/vendita/informatica/",
    "https://www.subito.it/annunci-piemonte/vendita/elettrodomestici/",
    "https://www.subito.it/annunci-piemonte/vendita/fotografia/",
]

configured_sources = [
    value.strip()
    for value in os.getenv("SOURCE_URLS", "").split(",")
    if value.strip()
]

# Se su Railway esiste ancora soltanto la vecchia pagina generica,
# usiamo automaticamente le categorie mirate.
GENERIC_SOURCE_URLS = {
    "https://www.subito.it/annunci-piemonte/vendita",
    "https://www.subito.it/annunci-piemonte/vendita/",
}

normalized_configured_sources = {
    value.rstrip("/")
    for value in configured_sources
}

if (
    not configured_sources
    or normalized_configured_sources
    == {value.rstrip("/") for value in GENERIC_SOURCE_URLS}
):
    SOURCE_URLS = DEFAULT_SOURCE_URLS
else:
    # Mantiene eventuali fonti personalizzate e aggiunge quelle principali,
    # evitando duplicati.
    SOURCE_URLS = list(dict.fromkeys(
        configured_sources + DEFAULT_SOURCE_URLS
    ))

configured_data_dir = os.getenv("DATA_DIR", "").strip()
if configured_data_dir:
    DATA_DIR = Path(configured_data_dir)
elif Path("/data").exists():
    DATA_DIR = Path("/data/radar_affari_ai")
else:
    DATA_DIR = Path(tempfile.gettempdir()) / "radar_affari_ai"

DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "radar_state.json"
SUBSCRIBERS_FILE = DATA_DIR / "radar_subscribers.json"
HISTORY_FILE = DATA_DIR / "radar_history.json"
STATS_FILE = DATA_DIR / "radar_stats.json"
DB_FILE = DATA_DIR / "radar_affari.sqlite3"
DATABASE_TARGET = os.getenv("DATABASE_URL", "").strip() or str(DB_FILE)
DB = RadarDatabase(DATABASE_TARGET)
DB.migrate_json_files(
    history_file=HISTORY_FILE,
    state_file=STATE_FILE,
    subscribers_file=SUBSCRIBERS_FILE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}

SCAN_LOCK = asyncio.Lock()

LAST_DECISION_DEBUG: List[Dict[str, Any]] = []
MAX_DECISION_DEBUG_ITEMS = 50



def source_label(url: str) -> str:
    path = urlparse(url).path.lower()
    for category in (
        "biciclette",
        "telefonia",
        "informatica",
        "elettrodomestici",
        "fotografia",
    ):
        if f"/{category}/" in path:
            return category.capitalize()
    return urlparse(url).netloc or "Fonte"


# ============================================================
# ARCHIVIO LOCALE
# ============================================================

def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Impossibile leggere %s: %s", path, exc)
        return default


def save_json(path: Path, data: Any) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.error("Impossibile salvare %s: %s", path, exc)


def subscribers() -> List[int]:
    return DB.subscribers()


def add_subscriber(chat_id: int) -> None:
    DB.add_subscriber(chat_id)


def remove_subscriber(chat_id: int) -> None:
    DB.remove_subscriber(chat_id)


# ============================================================
# FUNZIONI DI PULIZIA E RICONOSCIMENTO
# ============================================================

def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def create_item_id(url: str) -> str:
    clean_url = url.split("?")[0].rstrip("/")
    return hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:20]


def parse_price(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return number if 5 <= number <= 500000 else None

    text = normalize_text(str(value)).replace("\u00a0", " ")
    if not text:
        return None

    def convert_number(raw: str) -> Optional[float]:
        cleaned = raw.strip().replace(" ", "")

        if "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")

        try:
            number = float(cleaned)
        except ValueError:
            return None

        return number if 5 <= number <= 500000 else None

    number_pattern = (
        r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?"
        r"|\d+(?:,\d{1,2})?)"
    )

    euro_matches = re.findall(
        rf"(?<!\d){number_pattern}\s*(?:€|euro\b)",
        text,
        flags=re.IGNORECASE,
    )

    euro_values = [
        number
        for raw in euro_matches
        if (number := convert_number(raw)) is not None
    ]

    if euro_values:
        return euro_values[-1]

    context_matches = re.findall(
        rf"(?:prezzo|richiesta|chiedo|vendo\s+a|vendita\s+a|"
        rf"a\s+soli|offerta)\s*[:\-]?\s*{number_pattern}",
        text,
        flags=re.IGNORECASE,
    )

    context_values = [
        number
        for raw in context_matches
        if (number := convert_number(raw)) is not None
    ]

    return context_values[-1] if context_values else None


def title_from_url(url: str) -> str:
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d{5,}\.html?$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"\.html?$", "", slug, flags=re.IGNORECASE)
    slug = slug.replace("-", " ").replace("_", " ")
    return normalize_text(slug)


def matching_keywords(text: str) -> List[str]:
    lowered = normalize_text(text).lower()
    return [keyword for keyword in KEYWORDS if keyword in lowered]


def identify_product(text: str) -> Dict[str, Any]:
    """Usa il catalogo prodotti v2 con controlli anti-falso-positivo."""
    result = catalog_identify_product(text)
    normalized = normalize_text(text).lower()

    detected_model = str(result.get("model") or "").strip().lower()
    product_key = str(result.get("product_key") or "").strip().lower()

    # Sicurezza: modelli iPhone nuovi/non presenti nel catalogo non devono
    # essere ricondotti per somiglianza a modelli precedenti.
    mentions_iphone_17 = bool(
        re.search(r"\biphone\s*17\b|\biphone\s*17\s*(air|pro|max|pro\s*max)\b", normalized)
    )
    mentions_iphone_air = bool(re.search(r"\biphone\s*(17\s*)?air\b", normalized))

    if mentions_iphone_17 and "17" not in detected_model and ":17" not in product_key:
        return {
            "brand": "",
            "model": "",
            "variant": "",
            "storage": "",
            "recognition_confidence": 0,
            "product_key": "unidentified",
        }

    if mentions_iphone_air and "air" not in detected_model and ":air" not in product_key:
        return {
            "brand": "",
            "model": "",
            "variant": "",
            "storage": "",
            "recognition_confidence": 0,
            "product_key": "unidentified",
        }

    return result

def is_probable_listing_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path.lower().rstrip("/")

    if not host or not path:
        return False

    if "subito.it" in host:
        excluded_sections = (
            "/cerco-lavoro/",
            "/offerte-lavoro/",
            "/lavoro-servizi/",
            "/lavoro_servizi/",
            "/uffici-locali-commerciali/",
            "/appartamenti/",
            "/case-vacanze/",
            "/case_vacanze/",
            "/terreni-rustici/",
            "/terreni_rustici/",
            "/ville-singole-e-a-schiera/",
            "/garage-e-box/",
            "/auto/",
            "/moto-e-scooter/",
            "/veicoli-commerciali/",
        )
        if any(section in f"{path}/" for section in excluded_sections):
            return False
        return bool(re.search(r"-\d{5,}\.html?$", path))

    if "ebay." in host:
        return "/itm/" in path

    if "vinted." in host:
        return "/items/" in path

    return False


def risk_analysis(text: str) -> Dict[str, Any]:
    lowered = normalize_text(text).lower()

    high_risk_terms = [
        "non funzionante", "non funziona", "da riparare", "da sistemare",
        "per ricambi", "rotto", "guasto", "non testato",
        "non so se funziona", "senza garanzia", "bloccato",
        "account bloccato", "imei bloccato",
    ]

    medium_risk_terms = [
        "senza caricatore", "senza batteria", "manca", "difetto",
        "segni di usura", "solo spedizione", "visto e piaciuto",
    ]

    found_high = [term for term in high_risk_terms if term in lowered]
    found_medium = [term for term in medium_risk_terms if term in lowered]

    score = min(100, len(found_high) * 35 + len(found_medium) * 15)
    level = "ALTO" if found_high else "MEDIO" if found_medium else "BASSO"

    return {
        "score": score,
        "level": level,
        "reasons": (found_high + found_medium)[:4],
    }


# ============================================================
# ESTRAZIONE ANNUNCI
# ============================================================

def walk_json(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_price_from_object(obj: Dict[str, Any]) -> Optional[float]:
    possible_values = [
        obj.get("price"),
        obj.get("priceValue"),
        obj.get("amount"),
        obj.get("value"),
    ]

    offers = obj.get("offers")
    if isinstance(offers, dict):
        possible_values.extend([
            offers.get("price"),
            offers.get("lowPrice"),
            offers.get("highPrice"),
        ])

    for value in possible_values:
        if isinstance(value, dict):
            for nested_key in ("value", "amount", "price"):
                parsed = parse_price(value.get(nested_key))
                if parsed is not None:
                    return parsed
        else:
            parsed = parse_price(value)
            if parsed is not None:
                return parsed

    return None


def add_item(
    items: Dict[str, Dict[str, Any]],
    source_url: str,
    candidate_url: str,
    candidate_title: str = "",
    candidate_text: str = "",
    candidate_price: Optional[float] = None,
) -> None:
    absolute_url = urljoin(source_url, candidate_url).split("#")[0]

    if not is_probable_listing_url(absolute_url):
        return

    title = normalize_text(candidate_title)
    full_text = normalize_text(candidate_text)

    if len(title) < 4:
        title = title_from_url(absolute_url)

    combined_text = normalize_text(f"{title} {full_text}")
    matched = matching_keywords(combined_text)

    if len(title) < 4:
        return

    product = identify_product(combined_text)
    item_id = create_item_id(absolute_url)

    text_price = parse_price(full_text)
    price = candidate_price if candidate_price is not None else text_price

    new_item = {
        "id": item_id,
        "title": title[:180],
        "url": absolute_url,
        "text": full_text[:1000],
        "price": price,
        "matched": matched,
        "brand": product["brand"],
        "model": product["model"],
        "variant": product.get("variant", ""),
        "storage": product.get("storage", ""),
        "recognition_confidence": product.get("recognition_confidence", 0),
        "product_key": product["product_key"],
        "excluded": bool(product.get("excluded", False)),
        "exclusion_reason": str(product.get("exclusion_reason") or ""),
        "source": "subito",
        "category": source_label(source_url).lower(),
        "source_url": source_url,
        "relevant": bool(matched) and not bool(product.get("excluded", False)),
    }

    current = items.get(item_id)
    if current is None:
        items[item_id] = new_item
        return

    if len(new_item["text"]) > len(current.get("text", "")):
        current["text"] = new_item["text"]
    if len(new_item["title"]) > len(current.get("title", "")):
        current["title"] = new_item["title"]
    if current.get("price") is None and price is not None:
        current["price"] = price

    current_keywords = set(current.get("matched", []))
    current_keywords.update(matched)
    current["matched"] = sorted(current_keywords)


def extract_from_json_data(
    data: Any,
    source_url: str,
    items: Dict[str, Dict[str, Any]],
) -> None:
    for obj in walk_json(data):
        url_value = (
            obj.get("url")
            or obj.get("itemUrl")
            or obj.get("webUrl")
            or obj.get("canonicalUrl")
        )

        title_value = (
            obj.get("name")
            or obj.get("title")
            or obj.get("subject")
            or obj.get("headline")
        )

        description = (
            obj.get("description")
            or obj.get("body")
            or obj.get("text")
            or ""
        )

        if isinstance(url_value, str):
            add_item(
                items=items,
                source_url=source_url,
                candidate_url=url_value,
                candidate_title=str(title_value or ""),
                candidate_text=str(description or ""),
                candidate_price=extract_price_from_object(obj),
            )


def extract_from_html(
    soup: BeautifulSoup,
    source_url: str,
    items: Dict[str, Dict[str, Any]],
) -> None:
    for link in soup.find_all("a", href=True):
        href = normalize_text(str(link.get("href", "")))
        if not href:
            continue

        absolute_url = urljoin(source_url, href)
        if not is_probable_listing_url(absolute_url):
            continue

        candidates: List[str] = []

        for attribute in ("aria-label", "title", "data-title"):
            value = link.get(attribute)
            if isinstance(value, str):
                candidates.append(value)

        visible_text = link.get_text(" ", strip=True)
        if visible_text:
            candidates.append(visible_text)

        image = link.find("img")
        if image is not None:
            for attribute in ("alt", "title"):
                value = image.get(attribute)
                if isinstance(value, str):
                    candidates.append(value)

        container = link.find_parent(["article", "li", "div"])
        container_text = ""
        if container is not None:
            container_text = normalize_text(container.get_text(" ", strip=True))
            if container_text:
                candidates.append(container_text)

        title_candidates = [
            normalize_text(candidate)
            for candidate in candidates
            if 4 <= len(normalize_text(candidate)) <= 220
        ]

        title = max(title_candidates, key=len, default="")
        if len(title) > 180:
            shorter = [value for value in title_candidates if len(value) <= 180]
            title = max(shorter, key=len, default=title_from_url(absolute_url))

        add_item(
            items=items,
            source_url=source_url,
            candidate_url=absolute_url,
            candidate_title=title,
            candidate_text=container_text or visible_text,
        )


async def extract_items(url: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    diagnostics: Dict[str, Any] = {
        "url": url,
        "status": None,
        "links": 0,
        "listing_urls": 0,
        "extracted": 0,
        "priced": 0,
        "error": "",
    }

    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        diagnostics["status"] = response.status_code
        final_url = str(response.url)

        soup = BeautifulSoup(response.text, "html.parser")
        diagnostics["links"] = len(soup.find_all("a", href=True))

        items: Dict[str, Dict[str, Any]] = {}

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = script.string or script.get_text()
                if raw:
                    extract_from_json_data(json.loads(raw), final_url, items)
            except (json.JSONDecodeError, TypeError):
                continue

        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data is not None:
            try:
                raw = next_data.string or next_data.get_text()
                if raw:
                    extract_from_json_data(json.loads(raw), final_url, items)
            except json.JSONDecodeError:
                log.warning("__NEXT_DATA__ presente ma non leggibile.")

        extract_from_html(soup, final_url, items)

        all_listings = list(items.values())
        relevant_items = [
            item for item in all_listings
            if item.get("matched") and not item.get("excluded")
        ]

        diagnostics["listing_urls"] = len(all_listings)
        diagnostics["extracted"] = len(relevant_items)
        diagnostics["priced"] = sum(
            1 for item in relevant_items
            if item.get("price") is not None
        )
        diagnostics["archivable"] = sum(
            1 for item in all_listings
            if item.get("id") and item.get("url")
        )
        diagnostics["archivable_priced"] = sum(
            1 for item in all_listings
            if item.get("price") is not None
        )

        # Il collector riceve tutti gli annunci validi della categoria.
        # La selezione per parole chiave avviene dopo il salvataggio SQLite.
        return all_listings, diagnostics

    except Exception as exc:
        diagnostics["error"] = str(exc)
        log.exception("Errore estrazione %s: %s", url, exc)
        return [], diagnostics



async def extract_debug_samples(
    url: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    Restituisce alcuni annunci riconosciuti prima del filtro KEYWORDS.
    Serve esclusivamente per diagnosticare titolo, testo, prezzo e URL.
    """
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    final_url = str(response.url)
    soup = BeautifulSoup(response.text, "html.parser")
    items: Dict[str, Dict[str, Any]] = {}

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or script.get_text()
            if raw:
                extract_from_json_data(
                    json.loads(raw),
                    final_url,
                    items,
                )
        except (json.JSONDecodeError, TypeError):
            continue

    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data is not None:
        try:
            raw = next_data.string or next_data.get_text()
            if raw:
                extract_from_json_data(
                    json.loads(raw),
                    final_url,
                    items,
                )
        except json.JSONDecodeError:
            pass

    extract_from_html(soup, final_url, items)

    samples = list(items.values())[:limit]

    for sample in samples:
        combined = normalize_text(
            f"{sample.get('title', '')} {sample.get('text', '')}"
        )
        sample["debug_matches"] = matching_keywords(combined)

    return samples

# ============================================================
# STORICO E ANALISI ECONOMICA
# ============================================================

def update_history(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Modalità COLLECTOR:
    - salva ogni annuncio pertinente con prezzo;
    - aggiorna gli annunci già conosciuti;
    - conserva prima e ultima rilevazione;
    - registra eventuali variazioni di prezzo.
    """
    history = load_json(HISTORY_FILE, [])
    now = datetime.now(timezone.utc).isoformat()

    rows_by_id: Dict[str, Dict[str, Any]] = {
        str(row.get("id")): row
        for row in history
        if isinstance(row, dict) and row.get("id")
    }

    for item in items:
        item_id = str(item.get("id") or "")
        price = item.get("price")

        if not item_id or not isinstance(price, (int, float)):
            continue

        current_price = float(price)
        existing = rows_by_id.get(item_id)

        if existing is None:
            row = {
                "id": item_id,
                "product_key": item.get("product_key") or "",
                "brand": item.get("brand") or "",
                "model": item.get("model") or "",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source_url": item.get("source_url", ""),
                "first_seen": now,
                "last_seen": now,
                "observations": 1,
                "price": current_price,
                "price_history": [
                    {"at": now, "price": current_price}
                ],
            }
            history.append(row)
            rows_by_id[item_id] = row
            continue

        existing["last_seen"] = now
        existing["observations"] = int(existing.get("observations", 0)) + 1
        existing["title"] = item.get("title", existing.get("title", ""))
        existing["url"] = item.get("url", existing.get("url", ""))
        existing["product_key"] = (
            item.get("product_key")
            or existing.get("product_key", "")
        )
        existing["brand"] = item.get("brand") or existing.get("brand", "")
        existing["model"] = item.get("model") or existing.get("model", "")

        old_price = existing.get("price")
        if not isinstance(old_price, (int, float)) or float(old_price) != current_price:
            price_history = existing.setdefault("price_history", [])
            price_history.append({"at": now, "price": current_price})
            existing["price"] = current_price

    history = history[-30000:]
    save_json(HISTORY_FILE, history)
    return history


def update_collection_stats(
    diagnostics: List[Dict[str, Any]],
    relevant_items: List[Dict[str, Any]],
    new_items_count: int,
) -> Dict[str, Any]:
    stats = load_json(
        STATS_FILE,
        {
            "scans": 0,
            "total_relevant_seen": 0,
            "total_new_seen": 0,
            "last_scan": None,
        },
    )

    stats["scans"] = int(stats.get("scans", 0)) + 1
    stats["total_relevant_seen"] = int(
        stats.get("total_relevant_seen", 0)
    ) + len(relevant_items)
    stats["total_new_seen"] = int(
        stats.get("total_new_seen", 0)
    ) + new_items_count
    stats["last_scan"] = datetime.now(timezone.utc).isoformat()
    stats["last_sources"] = diagnostics

    save_json(STATS_FILE, stats)
    return stats


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def percentile(values: List[float], fraction: float) -> float:
    """Percentile lineare senza dipendenze esterne."""
    if not values:
        raise ValueError("Lista vuota")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * min(max(fraction, 0.0), 1.0)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index

    return (
        ordered[lower_index] * (1 - weight)
        + ordered[upper_index] * weight
    )


def remove_price_outliers(prices: List[float]) -> List[float]:
    """
    Elimina prezzi anomali usando l'intervallo interquartile.

    Con pochi dati mantiene tutti i prezzi: è preferibile dichiarare una
    bassa attendibilità piuttosto che eliminare confronti arbitrariamente.
    """
    clean = sorted(
        float(price)
        for price in prices
        if isinstance(price, (int, float)) and float(price) > 0
    )

    if len(clean) < 5:
        return clean

    q1 = percentile(clean, 0.25)
    q3 = percentile(clean, 0.75)
    iqr = q3 - q1

    if iqr <= 0:
        return clean

    lower_limit = max(5.0, q1 - 1.5 * iqr)
    upper_limit = q3 + 1.5 * iqr

    filtered = [
        price for price in clean
        if lower_limit <= price <= upper_limit
    ]

    # Non consentiamo al filtro di cancellare quasi tutto il campione.
    return filtered if len(filtered) >= 3 else clean


def market_confidence(
    prices: List[float],
    newest_seen: Optional[datetime],
) -> Tuple[int, str]:
    """
    Calcola un punteggio 0-100 basato su:
    - quantità di confronti;
    - dispersione dei prezzi;
    - freschezza del campione.
    """
    count = len(prices)
    if count == 0:
        return 0, "INSUFFICIENTE"

    if count >= HIGH_COMPARABLES:
        count_score = 60
    elif count >= GOOD_COMPARABLES:
        count_score = 48
    elif count >= MIN_COMPARABLES:
        count_score = 34
    else:
        count_score = min(25, count * 5)

    med = float(median(prices))
    q1 = percentile(prices, 0.25)
    q3 = percentile(prices, 0.75)
    relative_spread = (q3 - q1) / med if med > 0 else 1.0

    if relative_spread <= 0.15:
        spread_score = 30
    elif relative_spread <= 0.25:
        spread_score = 24
    elif relative_spread <= 0.40:
        spread_score = 16
    else:
        spread_score = 6

    freshness_score = 0
    if newest_seen is not None:
        age_days = max(
            0,
            (datetime.now(timezone.utc) - newest_seen).days,
        )
        if age_days <= 7:
            freshness_score = 10
        elif age_days <= 30:
            freshness_score = 7
        elif age_days <= MARKET_LOOKBACK_DAYS:
            freshness_score = 4

    score = min(100, count_score + spread_score + freshness_score)

    if score >= 80:
        label = "MOLTO ALTA"
    elif score >= 65:
        label = "ALTA"
    elif score >= 50:
        label = "DISCRETA"
    elif score >= MIN_CONFIDENCE_SCORE:
        label = "PRUDENTE"
    else:
        label = "BASSA"

    return score, label


def listing_condition_bucket(text: str) -> str:
    lowered = normalize_text(text).lower()

    damaged_terms = (
        "non funzionante", "non funziona", "da riparare", "da sistemare",
        "per ricambi", "rotto", "guasto", "non testato", "bloccato",
    )
    incomplete_terms = (
        "solo corpo", "solo console", "senza caricatore",
        "senza batteria", "senza accessori", "incompleto",
    )
    premium_terms = (
        "nuovo", "mai usato", "sigillato", "pari al nuovo",
    )

    if any(term in lowered for term in damaged_terms):
        return "damaged"
    if any(term in lowered for term in incomplete_terms):
        return "incomplete"
    if any(term in lowered for term in premium_terms):
        return "premium"
    return "standard"


def is_precise_product_key(key: str) -> bool:
    normalized = normalize_text(key).lower()
    return bool(normalized and ":" in normalized)


def comparable_rows_for_item(
    item: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    candidate_text = (
        f"{item.get('title', '')} "
        f"{item.get('text', item.get('description', ''))}"
    )
    candidate_bucket = listing_condition_bucket(candidate_text)

    filtered: List[Dict[str, Any]] = []

    for row in rows:
        row_text = (
            f"{row.get('title', '')} "
            f"{row.get('text', row.get('description', ''))}"
        )
        row_bucket = listing_condition_bucket(row_text)

        if candidate_bucket == "damaged":
            if row_bucket != "damaged":
                continue
        elif candidate_bucket == "incomplete":
            if row_bucket not in {"incomplete", "damaged"}:
                continue
        elif candidate_bucket == "premium":
            # "Come nuovo", "pari al nuovo" e "nuovo" non devono azzerare
            # il campione. Accettiamo premium e standard, ma mai rotti/incompleti.
            if row_bucket in {"damaged", "incomplete"}:
                continue
        else:
            if row_bucket in {"damaged", "incomplete"}:
                continue

        filtered.append(row)

    return filtered

def comparable_quality_score(
    item: Dict[str, Any],
    row: Dict[str, Any],
) -> int:
    """Assegna un punteggio 0-100 alla qualità del comparabile.

    Il product_key è già uguale perché la query DB lo impone. Il punteggio
    premia condizioni simili e parole rilevanti condivise, senza pretendere
    informazioni che spesso gli annunci non contengono.
    """
    score = 60

    item_text = normalize_text(
        f"{item.get('title', '')} {item.get('text', '')}"
    ).lower()
    row_text = normalize_text(
        f"{row.get('title', '')} "
        f"{row.get('text', row.get('description', ''))}"
    ).lower()

    if listing_condition_bucket(item_text) == listing_condition_bucket(row_text):
        score += 20

    item_tokens = {
        token for token in re.findall(r"[a-z0-9]+", item_text)
        if len(token) >= 3
    }
    row_tokens = {
        token for token in re.findall(r"[a-z0-9]+", row_text)
        if len(token) >= 3
    }

    shared = item_tokens.intersection(row_tokens)
    score += min(len(shared) * 2, 20)

    return min(score, 100)


def weighted_median(
    values_and_weights: List[Tuple[float, float]],
) -> float:
    """Mediana pesata senza dipendenze esterne."""
    if not values_and_weights:
        raise ValueError("Campione pesato vuoto")

    ordered = sorted(values_and_weights, key=lambda pair: pair[0])
    total_weight = sum(max(weight, 0.0) for _, weight in ordered)

    if total_weight <= 0:
        return float(median([value for value, _ in ordered]))

    threshold = total_weight / 2
    cumulative = 0.0

    for value, weight in ordered:
        cumulative += max(weight, 0.0)
        if cumulative >= threshold:
            return float(value)

    return float(ordered[-1][0])


def filter_by_central_deviation(
    prices: List[float],
) -> List[float]:
    """Secondo filtro prudenziale attorno alla mediana.

    Serve soprattutto con campioni piccoli nei quali l'IQR può non eliminare
    un annuncio chiaramente distante dal resto del mercato.
    """
    if len(prices) < 4:
        return prices

    center = float(median(prices))
    if center <= 0:
        return prices

    max_deviation = MAX_COMPARABLE_DEVIATION_PERCENT / 100
    filtered = [
        price for price in prices
        if abs(price - center) / center <= max_deviation
    ]

    return filtered if len(filtered) >= MIN_COMPARABLES else prices

def estimate_market_values(items: List[Dict[str, Any]]) -> None:
    """Price Engine Pro v1.8.

    Usa soltanto comparabili attivi, con product_key preciso e condizione
    omogenea. Applica due filtri anti-anomalia e una mediana pesata.
    Restituisce anche una fascia di mercato e dettagli su esclusioni e qualità.
    """
    for item in items:
        asking_price = item.get("price")
        key = str(item.get("product_key") or "").strip().lower()
        item_id = str(item.get("id") or "")

        comparable_rows = DB.active_comparables(
            key,
            exclude_listing_id=item_id,
            max_age_days=MARKET_LOOKBACK_DAYS,
        ) if is_precise_product_key(key) else []

        rows_before_condition_filter = len(comparable_rows)
        comparable_rows = comparable_rows_for_item(item, comparable_rows)
        condition_excluded = max(
            0,
            rows_before_condition_filter - len(comparable_rows),
        )

        priced_rows: List[Tuple[Dict[str, Any], float]] = []
        for row in comparable_rows:
            price = row.get("price")
            if isinstance(price, (int, float)) and float(price) > 0:
                priced_rows.append((row, float(price)))

        raw_prices = [price for _, price in priced_rows]
        iqr_prices = remove_price_outliers(raw_prices)
        comparable_prices = filter_by_central_deviation(iqr_prices)

        allowed_prices = set(comparable_prices)
        weighted_prices: List[Tuple[float, float]] = []

        for row, price in priced_rows:
            if price not in allowed_prices:
                continue
            quality = comparable_quality_score(item, row)
            weighted_prices.append((price, max(quality, 1)))

        newest_seen = max(
            (
                parse_iso_datetime(row.get("last_seen_at"))
                for row, price in priced_rows
                if price in allowed_prices
            ),
            default=None,
        )

        confidence_score, confidence_label = market_confidence(
            comparable_prices,
            newest_seen,
        )

        outliers_removed = max(0, len(raw_prices) - len(comparable_prices))
        average_quality = (
            round(
                sum(weight for _, weight in weighted_prices)
                / len(weighted_prices),
                1,
            )
            if weighted_prices else 0.0
        )

        item["raw_comparables"] = rows_before_condition_filter
        item["priced_comparables"] = len(raw_prices)
        item["comparables"] = len(comparable_prices)
        item["condition_excluded"] = condition_excluded
        item["outliers_removed"] = outliers_removed
        item["average_comparable_quality"] = average_quality
        item["confidence_score"] = confidence_score
        item["confidence_label"] = confidence_label
        item["market_lookback_days"] = MARKET_LOOKBACK_DAYS

        if comparable_prices:
            market_min = min(comparable_prices)
            market_low = percentile(comparable_prices, 0.25)
            market_high = percentile(comparable_prices, 0.75)

            item["market_min"] = round(market_min, 2)
            item["market_low"] = round(market_low, 2)
            item["market_high"] = round(market_high, 2)
        else:
            item["market_min"] = None
            item["market_low"] = None
            item["market_high"] = None

        if (
            asking_price is None
            or not is_precise_product_key(key)
            or len(comparable_prices) < MIN_COMPARABLES
            or confidence_score < MIN_CONFIDENCE_SCORE
            or not weighted_prices
        ):
            item["market_value"] = None
            item["quick_sale_value"] = None
            item["estimated_costs"] = None
            item["estimated_margin"] = None
            item["maximum_buy_price"] = None
            item["roi"] = None
            item["discount_vs_market_low"] = None
            continue

        market_value = weighted_median(weighted_prices)

        # Rivendita prudente: il più basso tra il 20° percentile e il 90%
        # della mediana pesata. Non usa mai il minimo assoluto come riferimento.
        quick_sale_value = min(
            percentile(comparable_prices, 0.20),
            market_value * 0.90,
        )

        estimated_costs = max(25.0, float(asking_price) * 0.06)
        maximum_buy_price = max(
            0.0,
            quick_sale_value - estimated_costs - MIN_MARGIN_EURO,
        )
        estimated_margin = (
            quick_sale_value - float(asking_price) - estimated_costs
        )
        roi = (
            estimated_margin / float(asking_price) * 100
            if float(asking_price) > 0
            else 0
        )

        market_low = float(item["market_low"])
        discount_vs_market_low = (
            (market_low - float(asking_price)) / market_low * 100
            if market_low > 0
            else 0
        )

        item["market_value"] = round(market_value, 2)
        item["quick_sale_value"] = round(quick_sale_value, 2)
        item["estimated_costs"] = round(estimated_costs, 2)
        item["maximum_buy_price"] = round(maximum_buy_price, 2)
        item["estimated_margin"] = round(estimated_margin, 2)
        item["roi"] = round(roi, 1)
        item["discount_vs_market_low"] = round(
            discount_vs_market_low,
            1,
        )

def analyze_deal(item: Dict[str, Any]) -> Dict[str, Any]:
    """Trasforma i dati economici in una decisione spiegabile."""
    risk = risk_analysis(f"{item.get('title', '')} {item.get('text', '')}")
    margin = item.get("estimated_margin")
    roi = item.get("roi")
    market_value = item.get("market_value")
    quick_sale_value = item.get("quick_sale_value")
    asking_price = item.get("price")
    costs = item.get("estimated_costs")
    confidence = int(item.get("confidence_score") or 0)
    discount_vs_market_low = item.get("discount_vs_market_low")
    precise_model = bool(item.get("model")) and is_precise_product_key(
        str(item.get("product_key") or "")
    )

    reasons: List[str] = []
    warnings: List[str] = []

    if not precise_model:
        warnings.append("modello o variante non identificati con precisione")
    if confidence < MIN_CONFIDENCE_SCORE:
        warnings.append("campione di mercato non ancora abbastanza attendibile")
    if risk["reasons"]:
        warnings.extend(f"rischio: {reason}" for reason in risk["reasons"][:3])

    score = 0.0
    discount_percent: Optional[float] = None
    max_offer: Optional[float] = None

    if (
        isinstance(asking_price, (int, float))
        and isinstance(market_value, (int, float))
        and market_value > 0
    ):
        discount_percent = (market_value - asking_price) / market_value * 100
        score += max(0.0, min(discount_percent, 30.0))
        if discount_percent >= 20:
            reasons.append("prezzo almeno 20% sotto il valore mediano")
        elif discount_percent >= MIN_MARKET_DISCOUNT_PERCENT:
            reasons.append("prezzo realmente sotto il valore mediano")
        else:
            warnings.append(
                f"prezzo troppo vicino al mercato: sconto {discount_percent:.1f}%"
            )

    if isinstance(discount_vs_market_low, (int, float)):
        if discount_vs_market_low >= MIN_DISCOUNT_VS_LOW_PERCENT:
            reasons.append(
                f"prezzo {discount_vs_market_low:.1f}% sotto la fascia bassa"
            )
        else:
            warnings.append(
                "prezzo non abbastanza sotto la fascia bassa del mercato"
            )

    if isinstance(roi, (int, float)):
        score += max(0.0, min(float(roi), 25.0))
        if roi >= MIN_ROI_PERCENT:
            reasons.append(f"ROI stimato almeno {MIN_ROI_PERCENT:.0f}%")

    if isinstance(margin, (int, float)):
        score += max(0.0, min(float(margin) / 8.0, 20.0))
        if margin >= MIN_MARGIN_EURO:
            reasons.append(f"margine stimato almeno {MIN_MARGIN_EURO:.0f} €")

    score += min(confidence / 100 * 15, 15)
    if confidence >= 65:
        reasons.append("stima di mercato con buona attendibilità")

    if precise_model:
        score += 10
        reasons.append("modello identificato con precisione")

    score -= {"BASSO": 0, "MEDIO": 15, "ALTO": 45}.get(risk["level"], 20)
    radar_score = int(round(max(0.0, min(score, 100.0))))

    if (
        isinstance(quick_sale_value, (int, float))
        and isinstance(costs, (int, float))
    ):
        max_offer = max(0.0, quick_sale_value - costs - MIN_MARGIN_EURO)

    if risk["level"] == "ALTO":
        decision = "SCARTA"
        label = "SCARTA: RISCHIO ALTO"
    elif margin is None or roi is None or confidence < MIN_CONFIDENCE_SCORE:
        decision = "DATI_INSUFFICIENTI"
        label = "DATI INSUFFICIENTI: NON COMPRARE ANCORA"
    elif not precise_model:
        decision = "DATI_INSUFFICIENTI"
        label = "DATI INSUFFICIENTI: MODELLO O VARIANTE DA CONFERMARE"
    elif discount_percent is None or discount_percent < MIN_MARKET_DISCOUNT_PERCENT:
        decision = "SCARTA"
        label = "SCARTA: PREZZO TROPPO VICINO AL MERCATO"
    elif (
        not isinstance(discount_vs_market_low, (int, float))
        or float(discount_vs_market_low) < MIN_DISCOUNT_VS_LOW_PERCENT
    ):
        decision = "SCARTA"
        label = "SCARTA: NON È SOTTO LA FASCIA BASSA DEL MERCATO"
    elif (
        isinstance(max_offer, (int, float))
        and isinstance(asking_price, (int, float))
        and float(asking_price) > float(max_offer)
    ):
        decision = "MONITORA"
        label = "MONITORA: COMPRA SOLO DOPO UNA TRATTATIVA"
    elif float(margin) >= MIN_MARGIN_EURO and float(roi) >= MIN_ROI_PERCENT:
        if risk["level"] == "BASSO":
            decision = "COMPRA"
            label = "COMPRA: CONTATTA E VERIFICA SUBITO"
        else:
            decision = "TRATTA"
            label = "TRATTA E VERIFICA PRIMA DI COMPRARE"
    elif float(margin) >= MIN_MARGIN_EURO * 0.5 or float(roi) >= MIN_ROI_PERCENT * 0.5:
        decision = "MONITORA"
        label = "MONITORA: INTERESSANTE SOLO A PREZZO PIÙ BASSO"
    else:
        decision = "SCARTA"
        label = "SCARTA: MARGINE O ROI INSUFFICIENTI"

    return {
        "decision": decision,
        "label": label,
        "radar_score": radar_score,
        "confidence": confidence,
        "risk": risk,
        "discount_percent": round(discount_percent, 1) if discount_percent is not None else None,
        "max_offer": round(max_offer, 2) if max_offer is not None else None,
        "reasons": reasons[:5],
        "warnings": warnings[:5],
    }


def is_valid_deal(item: Dict[str, Any]) -> bool:
    return analyze_deal(item)["decision"] in {"COMPRA", "TRATTA"}


def opportunity_score(item: Dict[str, Any]) -> float:
    analysis = analyze_deal(item)
    margin = max(float(item.get("estimated_margin") or 0), 0)
    return round(analysis["radar_score"] * 10 + margin, 2)


def verdict_for(item: Dict[str, Any], risk: Dict[str, Any]) -> str:
    # Manteniamo la firma per compatibilità con il resto dell'applicazione.
    return analyze_deal(item)["label"]


def euro(value: Optional[float]) -> str:
    if value is None:
        return "non disponibile"
    return f"{value:,.0f} €".replace(",", ".")


def build_message(item: Dict[str, Any]) -> str:
    analysis = analyze_deal(item)
    risk = analysis["risk"]

    title = html.escape(str(item.get("title", "")))
    url = html.escape(str(item.get("url", "")), quote=True)
    product_name = " ".join(
        part for part in [item.get("brand", ""), item.get("model", "")] if part
    ) or "non identificato"

    reasons = "\n".join(
        f"✅ {html.escape(reason)}" for reason in analysis["reasons"]
    ) or "• nessun elemento positivo sufficiente"
    warnings = "\n".join(
        f"⚠️ {html.escape(warning)}" for warning in analysis["warnings"]
    ) or "• nessuna anomalia testuale evidente"

    discount = analysis["discount_percent"]
    discount_text = f"{discount:.1f}%" if discount is not None else "non disponibile"

    return (
        f"🎯 <b>RADAR SCORE {analysis['radar_score']}/100</b>\n"
        f"🚦 <b>{html.escape(analysis['label'])}</b>\n\n"
        f"<b>{title}</b>\n"
        f"🧩 Prodotto: <b>{html.escape(product_name)}</b>\n\n"
        f"💰 Prezzo richiesto: <b>{euro(item.get('price'))}</b>\n"
        f"📉 Fascia mercato: <b>{euro(item.get('market_low'))} – {euro(item.get('market_high'))}</b>\n"
        f"📊 Valore centrale pesato: <b>{euro(item.get('market_value'))}</b>\n"
        f"⚡ Rivendita rapida prudente: <b>{euro(item.get('quick_sale_value'))}</b>\n"
        f"🏷 Sconto sul mercato: <b>{discount_text}</b>\n"
        f"🤝 Offerta massima prudente: <b>{euro(analysis.get('max_offer'))}</b>\n"
        f"🧾 Costi prudenziali: <b>{euro(item.get('estimated_costs'))}</b>\n"
        f"💵 Margine stimato: <b>{euro(item.get('estimated_margin'))}</b>\n"
        f"📈 ROI stimato: <b>{item.get('roi', 'non disponibile')}%</b>\n"
        f"🛡 Attendibilità: <b>{item.get('confidence_label', 'INSUFFICIENTE')}</b> "
        f"({item.get('confidence_score', 0)}/100)\n"
        f"📚 Confronti validi: <b>{item.get('comparables', 0)}</b> "
        f"su {item.get('raw_comparables', 0)}\n"
        f"🧹 Esclusi per condizione: <b>{item.get('condition_excluded', 0)}</b>\n"
        f"🚫 Prezzi anomali esclusi: <b>{item.get('outliers_removed', 0)}</b>\n"
        f"🎯 Qualità media comparabili: <b>{item.get('average_comparable_quality', 0)}/100</b>\n"
        f"⚠️ Rischio: <b>{risk['level']}</b> ({risk['score']}/100)\n\n"
        f"<b>PERCHÉ</b>\n{reasons}\n\n"
        f"<b>DA VERIFICARE</b>\n{warnings}\n\n"
        f'<a href="{url}">APRI DIRETTAMENTE L’ANNUNCIO</a>\n\n'
        "Non acquistare senza prova e verifica manuale."
    )


# ============================================================
# SCANSIONE E TELEGRAM
# ============================================================

async def scan_once(application: Application) -> Dict[str, Any]:
    if SCAN_LOCK.locked():
        return {"busy": True, "new": 0, "valid": 0, "diagnostics": []}

    async with SCAN_LOCK:
        if not SOURCE_URLS:
            log.warning("Nessuna SOURCE_URL configurata.")
            return {"busy": False, "new": 0, "valid": 0, "diagnostics": []}

        scan_token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        all_extracted: List[Dict[str, Any]] = []
        diagnostics: List[Dict[str, Any]] = []

        for source_url in SOURCE_URLS:
            extracted_items, source_diagnostics = await extract_items(source_url)
            diagnostics.append(source_diagnostics)
            all_extracted.extend(extracted_items)

        # Deduplica eventuali annunci presenti in più fonti/categorie.
        unique_items = {str(item["id"]): item for item in all_extracted}.values()
        all_extracted = list(unique_items)

        # Salviamo nel database ogni annuncio valido trovato nelle
        # categorie configurate, anche quando non contiene una keyword.
        # Solo gli annunci pertinenti vengono poi valutati economicamente.
        new_archived_items: List[Dict[str, Any]] = []
        relevant_items: List[Dict[str, Any]] = []
        new_items: List[Dict[str, Any]] = []

        for item in all_extracted:
            is_new = DB.upsert_listing(item, scan_token=scan_token)
            if is_new:
                new_archived_items.append(item)
            if item.get("matched") and not item.get("excluded"):
                relevant_items.append(item)
                if is_new:
                    new_items.append(item)

        all_sources_ok = bool(diagnostics) and all(not row.get("error") for row in diagnostics)
        if all_sources_ok:
            DB.mark_missing_after_scan(scan_token, source="subito", grace_hours=24)

        update_collection_stats(
            diagnostics=diagnostics,
            relevant_items=relevant_items,
            new_items_count=len(new_items),
        )
        estimate_market_values(relevant_items)

        analyses: Dict[str, Dict[str, Any]] = {}
        decision_counts = {
            "COMPRA": 0, "TRATTA": 0, "MONITORA": 0,
            "SCARTA": 0, "DATI_INSUFFICIENTI": 0,
        }

        global LAST_DECISION_DEBUG
        current_debug_rows: List[Dict[str, Any]] = []

        for item in relevant_items:
            analysis = analyze_deal(item)
            analyses[item["id"]] = analysis

            current_debug_rows.append({
                "id": item.get("id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "product_key": item.get("product_key"),
                "brand": item.get("brand"),
                "model": item.get("model"),
                "storage": item.get("storage"),
                "price": item.get("price"),
                "market_low": item.get("market_low"),
                "market_high": item.get("market_high"),
                "market_value": item.get("market_value"),
                "quick_sale_value": item.get("quick_sale_value"),
                "estimated_margin": item.get("estimated_margin"),
                "roi": item.get("roi"),
                "comparables": item.get("comparables", 0),
                "raw_comparables": item.get("raw_comparables", 0),
                "priced_comparables": item.get("priced_comparables", 0),
                "confidence_score": item.get("confidence_score", 0),
                "confidence_label": item.get("confidence_label", "INSUFFICIENTE"),
                "decision": analysis.get("decision"),
                "label": analysis.get("label"),
                "radar_score": analysis.get("radar_score"),
                "max_offer": analysis.get("max_offer"),
                "reasons": analysis.get("reasons", []),
                "warnings": analysis.get("warnings", []),
            })
            item["decision"] = analysis["decision"]
            item["rejection_reason"] = "; ".join(analysis.get("warnings", []))
            decision_counts[analysis["decision"]] = decision_counts.get(analysis["decision"], 0) + 1
            DB.record_evaluation(
                item["id"],
                {
                    "asking_price": item.get("price"),
                    "estimated_sale_price": item.get("quick_sale_value"),
                    "maximum_buy_price": item.get("maximum_buy_price") or analysis.get("max_offer"),
                    "gross_margin": item.get("estimated_margin"),
                    "net_margin": item.get("estimated_margin"),
                    "roi": item.get("roi"),
                    "comparables_count": item.get("comparables", 0),
                    "confidence": item.get("confidence_score", 0),
                    "decision": analysis["decision"],
                    "rejection_reason": item["rejection_reason"],
                },
            )

        LAST_DECISION_DEBUG = (
            current_debug_rows + LAST_DECISION_DEBUG
        )[:MAX_DECISION_DEBUG_ITEMS]

        valid_deals = [
            item for item in relevant_items
            if not DB.was_notified(item["id"])
            and analyses[item["id"]]["decision"] in {"COMPRA", "TRATTA"}
        ]
        valid_deals.sort(
            key=lambda item: (
                opportunity_score(item),
                float(item.get("estimated_margin") or 0),
                float(item.get("roi") or 0),
            ),
            reverse=True,
        )

        for item in valid_deals[:MAX_ALERTS_PER_SCAN]:
            message = build_message(item)
            sent_to_at_least_one = False
            for chat_id in subscribers():
                try:
                    await application.bot.send_message(
                        chat_id=chat_id, text=message, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    sent_to_at_least_one = True
                except Exception as exc:
                    log.warning("Invio fallito verso %s: %s", chat_id, exc)
            if sent_to_at_least_one:
                DB.mark_notified(item["id"], item.get("decision", "AFFARE"))

        log.info(
            "SCAN pertinenti=%s nuovi=%s affari_validi=%s iscritti=%s db=%s",
            len(relevant_items), len(new_items), len(valid_deals),
            len(subscribers()), DB.location_label,
        )
        return {
            "busy": False,
            "new": len(new_items),
            "archived": len(all_extracted),
            "new_archived": len(new_archived_items),
            "relevant": len(relevant_items),
            "valid": len(valid_deals),
            "buy": decision_counts.get("COMPRA", 0),
            "negotiate": decision_counts.get("TRATTA", 0),
            "monitor": decision_counts.get("MONITORA", 0),
            "discard": decision_counts.get("SCARTA", 0),
            "insufficient": decision_counts.get("DATI_INSUFFICIENTI", 0),
            "diagnostics": diagnostics,
        }



def reclassify_database_listings() -> Dict[str, int]:
    """Ricalcola la classificazione degli annunci già presenti."""
    counters = {
        "total": 0,
        "changed": 0,
        "recognized": 0,
        "unrecognized": 0,
    }
    placeholder = "%s" if DB.backend == "postgresql" else "?"

    with DB.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, description, brand, model, product_key
            FROM listings
            ORDER BY id
            """
        ).fetchall()

        for row in rows:
            counters["total"] += 1
            product = identify_product(
                f"{row['title'] or ''} {row['description'] or ''}"
            )

            new_brand = str(product.get("brand") or "")
            new_model = str(product.get("model") or "")
            new_variant = str(product.get("variant") or "")
            new_storage = str(product.get("storage") or "")
            new_key = str(product.get("product_key") or "").lower()
            confidence = int(product.get("recognition_confidence") or 0)

            if new_key:
                counters["recognized"] += 1
            else:
                counters["unrecognized"] += 1

            old_key = str(row["product_key"] or "").lower()
            old_brand = str(row["brand"] or "")
            old_model = str(row["model"] or "")

            if (
                new_key == old_key
                and new_brand == old_brand
                and new_model == old_model
            ):
                continue

            conn.execute(
                f"""
                UPDATE listings
                SET brand = {placeholder},
                    model = {placeholder},
                    variant = {placeholder},
                    storage = {placeholder},
                    product_key = {placeholder},
                    recognition_confidence = {placeholder},
                    updated_at = {placeholder}
                WHERE id = {placeholder}
                """,
                (
                    new_brand,
                    new_model,
                    new_variant,
                    new_storage,
                    new_key,
                    confidence,
                    datetime.now(timezone.utc).isoformat(),
                    str(row["id"]),
                ),
            )
            counters["changed"] += 1

    return counters


async def reclassify(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🧠 Riclassificazione v2 in corso…")

    try:
        result = reclassify_database_listings()
    except Exception as exc:
        log.exception("Riclassificazione v2 fallita")
        await update.message.reply_text(
            f"❌ Riclassificazione fallita:\n{str(exc)[:500]}"
        )
        return

    percentage = (
        result["recognized"] / result["total"] * 100
        if result["total"] else 0
    )

    await update.message.reply_text(
        "✅ RICLASSIFICAZIONE V2 COMPLETATA\n\n"
        f"Annunci analizzati: {result['total']}\n"
        f"Annunci modificati: {result['changed']}\n"
        f"Riconosciuti con chiave precisa: {result['recognized']}\n"
        f"Non identificati: {result['unrecognized']}\n"
        f"Tasso riconoscimento: {percentage:.1f}%\n\n"
        "Ora esegui /collector."
    )


# ============================================================
# COMANDI TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    add_subscriber(update.effective_chat.id)

    await update.message.reply_text(
        "✅ Radar Affari Decision Engine v3.2 Recognition + PostgreSQL + Vision attivato.\n\n"
        f"• Margine minimo: {MIN_MARGIN_EURO:.0f} €\n"
        f"• ROI minimo: {MIN_ROI_PERCENT:.0f}%\n"
        f"• Confronti minimi: {MIN_COMPARABLES}\n"
        f"• Storico usato: {MARKET_LOOKBACK_DAYS} giorni\n\n"
        "/status - stato del radar\n"
        "/fonti - diagnostica delle fonti\n"
        "/categorie - mostra le categorie attive\n"
        "/collector - stato archivio mercato\n"
        "/debug - mostra cosa legge il parser\n"
        "/test - prova Telegram\n"
        "/scan - scansione manuale\n"
        "/reclassify - riclassifica tutto lo storico\n"
        "/decisiondebug - dettaglio ultime valutazioni\n"
        "/topscarti - migliori scarti e quasi affari\n"
        "/topcompra - migliori compra e tratta\n"
        "/visiontest URL - analisi foto di un annuncio\n"
        "/reset - azzera memoria annunci\n"
        "/stop - disattiva avvisi"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    db_stats = DB.stats()
    stats = load_json(STATS_FILE, {"scans": 0})
    await update.message.reply_text(
        f"🧠 Versione: Decision Engine v3.2 Recognition + PostgreSQL + Vision\n"
        f"📡 Fonti configurate: {len(SOURCE_URLS)}\n"
        f"🔎 Parole chiave: {len(KEYWORDS)}\n"
        f"⏱ Controllo ogni {CHECK_MINUTES} minuti\n"
        f"💵 Margine minimo: {MIN_MARGIN_EURO:.0f} €\n"
        f"📈 ROI minimo: {MIN_ROI_PERCENT:.0f}%\n"
        f"🛡 Attendibilità minima: {MIN_CONFIDENCE_SCORE}/100\n"
        f"📅 Confronti attivi: ultimi {MARKET_LOOKBACK_DAYS} giorni\n"
        f"📚 Confronti minimi per stima: {MIN_COMPARABLES}\n"
        f"📉 Sconto minimo sul mercato: {MIN_MARKET_DISCOUNT_PERCENT:.0f}%\n"
        f"📉 Sconto minimo sulla fascia bassa: {MIN_DISCOUNT_VS_LOW_PERCENT:.0f}%\n"
        f"🗄 Annunci nel database: {db_stats['total']}\n"
        f"✅ Annunci attivi: {db_stats['active']}\n"
        f"⚠️ Annunci scomparsi: {db_stats['missing']}\n"
        f"🧩 Annunci riconosciuti: {db_stats['recognized']}\n"
        f"💶 Osservazioni prezzo: {db_stats['price_observations']}\n"
        f"🧮 Valutazioni salvate: {db_stats['evaluations']}\n"
        f"🔄 Scansioni registrate: {int(stats.get('scans', 0))}\n"
        f"👥 Iscritti: {len(subscribers())}\n"
        f"💾 Database: {DB.location_label}"
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "✅ TEST RADAR\n"
        "Collegamento Telegram ↔ applicazione funzionante."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🔎 Controllo in corso…")
    result = await scan_once(context.application)

    if result["busy"]:
        await update.message.reply_text(
            "⏳ Una scansione è già in corso. Riprova tra poco."
        )
        return

    await update.message.reply_text(
        "✅ CONTROLLO TERMINATO\n\n"
        f"Annunci letti e archiviati: {result.get('archived', 0)}\n"
        f"Nuovi nel database: {result.get('new_archived', 0)}\n"
        f"Annunci pertinenti: {result.get('relevant', 0)}\n"
        f"Nuovi pertinenti: {result['new']}\n"
        f"🟢 Compra: {result.get('buy', 0)}\n"
        f"🟡 Tratta e verifica: {result.get('negotiate', 0)}\n"
        f"👀 Monitora: {result.get('monitor', 0)}\n"
        f"🔴 Scarta: {result.get('discard', 0)}\n"
        f"⚪ Dati insufficienti: {result.get('insufficient', 0)}\n\n"
        f"Avvisi inviabili: {result['valid']}"
    )


async def fonti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not SOURCE_URLS:
        await update.message.reply_text(
            "❌ Nessuna SOURCE_URL configurata su Railway."
        )
        return

    await update.message.reply_text("🧪 Controllo fonti in corso…")

    lines: List[str] = []
    for source_url in SOURCE_URLS:
        _, diagnostics = await extract_items(source_url)
        if diagnostics["error"]:
            lines.append(
                f"❌ {source_url}\nErrore: {diagnostics['error'][:180]}"
            )
        else:
            lines.append(
                f"✅ {source_label(source_url)}\n"
                f"{source_url}\n"
                f"HTTP {diagnostics['status']}\n"
                f"Link totali: {diagnostics['links']}\n"
                f"URL annunci riconosciuti: {diagnostics['listing_urls']}\n"
                f"Archiviabili: {diagnostics.get('archivable', 0)}\n"
                f"Archiviabili con prezzo: {diagnostics.get('archivable_priced', 0)}\n"
                f"Annunci pertinenti: {diagnostics['extracted']}\n"
                f"Pertinenti con prezzo: {diagnostics['priced']}"
            )

    message = "\n\n".join(lines)
    await update.message.reply_text(message[:4000])




async def categorie(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    lines = [
        "🧭 CATEGORIE ATTIVE",
        "",
    ]

    for index, source_url in enumerate(SOURCE_URLS, start=1):
        lines.append(
            f"{index}. {source_label(source_url)}\n{source_url}"
        )

    await update.message.reply_text(
        "\n\n".join(lines)[:4000],
        disable_web_page_preview=True,
    )



async def debug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    if not SOURCE_URLS:
        await update.message.reply_text(
            "❌ Nessuna SOURCE_URL configurata."
        )
        return

    await update.message.reply_text(
        "🧪 Lettura dei primi annunci in corso…"
    )

    messages: List[str] = []

    for source_url in SOURCE_URLS[:3]:
        try:
            samples = await extract_debug_samples(
                source_url,
                limit=5,
            )
        except Exception as exc:
            messages.append(
                f"❌ Fonte: {source_url}\nErrore: {str(exc)[:250]}"
            )
            continue

        if not samples:
            messages.append(
                f"⚠️ Fonte: {source_url}\n"
                "Nessun annuncio riconosciuto."
            )
            continue

        lines = [f"📡 Fonte: {source_url}"]

        for index, item in enumerate(samples, start=1):
            title = normalize_text(
                str(item.get("title") or "")
            )[:220]
            text = normalize_text(
                str(item.get("text") or "")
            )[:300]
            price = item.get("price")
            matched = ", ".join(
                item.get("debug_matches", [])
            ) or "nessuna"
            url = str(item.get("url") or "")

            lines.append(
                f"\n#{index}\n"
                f"Titolo: {title or '[vuoto]'}\n"
                f"Prezzo: {price if price is not None else '[mancante]'}\n"
                f"Keyword: {matched}\n"
                f"Testo: {text or '[vuoto]'}\n"
                f"URL: {url}"
            )

        messages.append("\n".join(lines))

    for message in messages:
        # Telegram limita i messaggi a 4096 caratteri.
        for start_index in range(0, len(message), 3900):
            await update.message.reply_text(
                message[start_index:start_index + 3900],
                disable_web_page_preview=True,
            )

async def collector(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    db_stats = DB.stats()
    stats = load_json(STATS_FILE, {"scans": 0})
    top_products = DB.product_counts(8)
    top_text = "\n".join(
        f"• {row['product_key']}: {row['count']}" for row in top_products
    ) or "• nessun dato"

    await update.message.reply_text(
        "🗄 STATO COLLECTOR POSTGRESQL\n\n"
        f"Annunci archiviati: {db_stats['total']}\n"
        f"Annunci attivi: {db_stats['active']}\n"
        f"Annunci scomparsi: {db_stats['missing']}\n"
        f"Osservazioni prezzo: {db_stats['price_observations']}\n"
        f"Annunci con variazioni prezzo: {DB.price_change_listings_count()}\n"
        f"Valutazioni registrate: {db_stats['evaluations']}\n"
        f"Scansioni registrate: {int(stats.get('scans', 0))}\n\n"
        f"Prodotti più raccolti:\n{top_text}"
    )


def _decision_debug_block(row: Dict[str, Any], index: int) -> str:
    title = normalize_text(str(row.get("title") or ""))[:180]
    url = str(row.get("url") or "")
    product_key = str(row.get("product_key") or "non identificato")
    decision = str(row.get("decision") or "DATI_INSUFFICIENTI")
    label = str(row.get("label") or "")
    reasons = row.get("reasons") or []
    warnings = row.get("warnings") or []

    reasons_text = (
        "\n".join(f"✅ {reason}" for reason in reasons)
        if reasons else "• nessun motivo positivo sufficiente"
    )
    warnings_text = (
        "\n".join(f"⚠️ {warning}" for warning in warnings)
        if warnings else "• nessun avviso specifico"
    )

    return (
        f"#{index} {decision}\n"
        f"{title}\n"
        f"Prodotto: {product_key}\n"
        f"Prezzo: {euro(row.get('price'))}\n"
        f"Mercato: {euro(row.get('market_low'))} – {euro(row.get('market_high'))}\n"
        f"Valore: {euro(row.get('market_value'))}\n"
        f"Rivendita prudente: {euro(row.get('quick_sale_value'))}\n"
        f"Margine: {euro(row.get('estimated_margin'))}\n"
        f"ROI: {row.get('roi') if row.get('roi') is not None else 'n/d'}%\n"
        f"Comparabili validi: {row.get('comparables', 0)}\n"
        f"Con prezzo dopo filtro condizione: "
        f"{row.get('priced_comparables', 0)}\n"
        f"Candidati DB prima dei filtri: "
        f"{row.get('raw_comparables', 0)}\n"
        f"Attendibilità: {row.get('confidence_label', 'INSUFFICIENTE')} "
        f"({row.get('confidence_score', 0)}/100)\n"
        f"Radar score: {row.get('radar_score', 0)}/100\n"
        f"Offerta massima: {euro(row.get('max_offer'))}\n"
        f"Verdetto: {label}\n\n"
        f"PERCHÉ\n{reasons_text}\n\n"
        f"PROBLEMI\n{warnings_text}\n"
        f"Link: {url}"
    )


async def _send_debug_rows(
    update: Update,
    rows: List[Dict[str, Any]],
    heading: str,
    limit: int = 10,
) -> None:
    if update.message is None:
        return

    if not rows:
        await update.message.reply_text(
            "⚠️ Nessuna valutazione disponibile.\n"
            "Esegui prima /scan."
        )
        return

    await update.message.reply_text(
        f"{heading}\n"
        f"Mostro {min(len(rows), limit)} valutazioni."
    )

    for index, row in enumerate(rows[:limit], start=1):
        block = _decision_debug_block(row, index)
        for start_index in range(0, len(block), 3900):
            await update.message.reply_text(
                block[start_index:start_index + 3900],
                disable_web_page_preview=True,
            )


async def decisiondebug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _send_debug_rows(
        update,
        LAST_DECISION_DEBUG,
        "🧪 DECISION DEBUG",
        limit=10,
    )


async def topscarti(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    rows = [
        row for row in LAST_DECISION_DEBUG
        if row.get("decision") in {
            "SCARTA", "MONITORA", "DATI_INSUFFICIENTI"
        }
    ]
    rows.sort(
        key=lambda row: (
            float(row.get("radar_score") or 0),
            float(row.get("estimated_margin") or 0),
        ),
        reverse=True,
    )
    await _send_debug_rows(
        update,
        rows,
        "🔴 TOP SCARTI / QUASI AFFARI",
        limit=10,
    )


async def topcompra(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    rows = [
        row for row in LAST_DECISION_DEBUG
        if row.get("decision") in {"COMPRA", "TRATTA"}
    ]
    rows.sort(
        key=lambda row: (
            float(row.get("radar_score") or 0),
            float(row.get("estimated_margin") or 0),
        ),
        reverse=True,
    )
    await _send_debug_rows(
        update,
        rows,
        "🟢 TOP COMPRA / TRATTA",
        limit=10,
    )



def _vision_result_message(result: Dict[str, Any]) -> str:
    defects = result.get("visible_defects") or []
    accessories = result.get("visible_accessories") or []
    fraud = result.get("counterfeit_or_fraud_signals") or []

    defects_text = "\n".join(f"• {value}" for value in defects) or "• nessuno evidente"
    accessories_text = "\n".join(f"• {value}" for value in accessories) or "• nessuno riconoscibile"
    fraud_text = "\n".join(f"• {value}" for value in fraud) or "• nessun segnale evidente"

    return (
        "👁 RADAR VISION AI V1\n\n"
        f"Annuncio: {result.get('listing_title') or '[titolo assente]'}\n"
        f"Immagini analizzate: {result.get('images_analyzed', 0)}\n\n"
        f"Categoria: {result.get('category') or 'non determinata'}\n"
        f"Marca: {result.get('brand') or 'non determinata'}\n"
        f"Modello: {result.get('model') or 'non determinato'}\n"
        f"Variante: {result.get('variant') or 'non determinata'}\n"
        f"Memoria/taglia: {result.get('storage_or_size') or 'non determinata'}\n"
        f"Anno stimato: {result.get('estimated_year') or 'non determinato'}\n"
        f"Condizione visibile: {result.get('visible_condition') or 'unknown'}\n"
        f"Coerenza testo/foto: {result.get('text_image_consistency') or 'unknown'}\n"
        f"Confidenza riconoscimento: {result.get('recognition_confidence', 0)}/100\n"
        f"Confidenza condizione: {result.get('condition_confidence', 0)}/100\n\n"
        f"DIFETTI VISIBILI\n{defects_text}\n\n"
        f"ACCESSORI VISIBILI\n{accessories_text}\n\n"
        f"SEGNALI DI RISCHIO\n{fraud_text}\n\n"
        f"NOTE\n{result.get('notes') or 'nessuna'}\n\n"
        f"Link: {result.get('listing_url') or ''}\n\n"
        "L'analisi visiva non certifica funzionamento, autenticità o stato interno."
    )


def _knowledge_result_message(report: Dict[str, Any]) -> str:
    if not report.get("supported"):
        return (
            "🧠 KNOWLEDGE ENGINE\n\n"
            f"{report.get('message', 'Categoria non ancora supportata.')}"
        )

    repairs = report.get("repair_items") or []
    repairs_text = "\n".join(
        f"• {row.get('issue')}: {euro(row.get('min_cost'))} – {euro(row.get('max_cost'))} "
        f"(gravità {row.get('severity', 'non definita')})"
        for row in repairs
    ) or "• Nessuna riparazione identificata dalle fotografie."

    checklist_text = "\n".join(f"☐ {value}" for value in report.get("checklist") or [])
    questions_text = "\n".join(f"• {value}" for value in report.get("questions_for_seller") or [])
    no_buy_text = "\n".join(f"⛔ {value}" for value in report.get("do_not_buy_if") or [])

    roi = report.get("roi")
    roi_text = f"{roi}%" if roi is not None else "non disponibile"

    return (
        "🧠 RADAR KNOWLEDGE ENGINE V1\n\n"
        f"🚦 Verdetto: {report.get('verdict', 'VERIFICA')}\n"
        f"⭐ BUY SCORE: {report.get('buy_score', 0)}/100\n"
        f"⚠️ Rischio tecnico: {report.get('risk_score', 0)}/100\n\n"
        f"🔧 COSTI DI RIPARAZIONE\n{repairs_text}\n\n"
        f"Totale minimo: {euro(report.get('repair_cost_min'))}\n"
        f"Totale massimo prudente: {euro(report.get('repair_cost_max'))}\n"
        f"Riduzione rivendibilità: {report.get('resale_penalty_percent', 0)}%\n\n"
        f"💰 VALUTAZIONE ECONOMICA\n"
        f"Rivendita prudente: {euro(report.get('prudent_resale_value'))}\n"
        f"Prezzo massimo di acquisto: {euro(report.get('maximum_buy_price'))}\n"
        f"Margine stimato: {euro(report.get('estimated_margin'))}\n"
        f"ROI: {roi_text}\n\n"
        f"📋 CHECKLIST PRE-ACQUISTO\n{checklist_text}\n\n"
        f"❓ DOMANDE AL VENDITORE\n{questions_text}\n\n"
        f"🚫 NON COMPRARE SE\n{no_buy_text}"
    )


async def visiontest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n/visiontest URL_ANNUNCIO\n\n"
            "Esempio:\n/visiontest https://www.subito.it/..."
        )
        return

    url = context.args[0].strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Inserisci un URL completo.")
        return

    await update.message.reply_text(
        "👁 Analisi Vision + Knowledge Engine in corso… può richiedere fino a un minuto."
    )

    try:
        result = await analyze_listing(url)
        vision_message = _vision_result_message(result)

        asking_price = parse_price(
            f"{result.get('listing_title', '')} "
            f"{result.get('listing_description', '')}"
        )

        report = build_knowledge_report(
            vision_result=result,
            asking_price=asking_price,
            market_value=None,
            target_margin=MIN_MARGIN_EURO,
            transaction_costs=25.0,
        )
        knowledge_message = _knowledge_result_message(report)
        messages = [vision_message, knowledge_message]
    except Exception as exc:
        log.exception("Vision + Knowledge test fallito")
        await update.message.reply_text(
            f"❌ Analisi non riuscita:\n{str(exc)[:900]}"
        )
        return

    for message in messages:
        for start_index in range(0, len(message), 3900):
            await update.message.reply_text(
                message[start_index:start_index + 3900],
                disable_web_page_preview=True,
            )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    removed = DB.clear_notifications()
    await update.message.reply_text(
        f"♻️ Notifiche azzerate: {removed}.\n"
        "Il database degli annunci e lo storico prezzi sono stati mantenuti."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔕 Avvisi disattivati.")


# ============================================================
# CICLO AUTOMATICO
# ============================================================

async def scan_loop(application: Application) -> None:
    await asyncio.sleep(10)

    while True:
        try:
            await scan_once(application)
        except Exception:
            log.exception("Errore durante la scansione automatica")

        await asyncio.sleep(CHECK_MINUTES * 60)


async def post_init(application: Application) -> None:
    application.create_task(scan_loop(application))


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Variabile TELEGRAM_BOT_TOKEN mancante. "
            "Configura il token nelle variabili ambiente di Railway."
        )

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(CommandHandler("scan", scan))
    application.add_handler(CommandHandler("fonti", fonti))
    application.add_handler(CommandHandler("categorie", categorie))
    application.add_handler(CommandHandler("collector", collector))
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("reclassify", reclassify))
    application.add_handler(CommandHandler("decisiondebug", decisiondebug))
    application.add_handler(CommandHandler("topscarti", topscarti))
    application.add_handler(CommandHandler("topcompra", topcompra))
    application.add_handler(CommandHandler("visiontest", visiontest))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("stop", stop))

    log.info(
        "Avvio Radar Affari Decision Engine v3.2 Recognition + PostgreSQL + Vision: %s fonti, %s parole chiave, dati=%s",
        len(SOURCE_URLS),
        len(KEYWORDS),
        DATA_DIR,
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from radar_database import RadarDatabase
from product_identifier import identify_product as catalog_identify_product
from visual_analyzer import analyze_listing
from knowledge_engine import build_knowledge_report


# ============================================================
# CONFIGURAZIONE
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("radar-affari")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_MINUTES = max(int(os.getenv("CHECK_MINUTES", "15")), 5)

MIN_MARGIN_EURO = float(os.getenv("MIN_MARGIN_EURO", "80"))
MIN_ROI_PERCENT = float(os.getenv("MIN_ROI_PERCENT", "20"))
MAX_ALERTS_PER_SCAN = max(int(os.getenv("MAX_ALERTS_PER_SCAN", "30")), 1)

# Parametri del motore di valutazione v1.5.
# Il Radar usa solo confronti recenti, elimina prezzi anomali e assegna
# un livello di attendibilità alla stima.
MARKET_LOOKBACK_DAYS = max(int(os.getenv("MARKET_LOOKBACK_DAYS", "90")), 7)
MIN_COMPARABLES = max(int(os.getenv("MIN_COMPARABLES", "3")), 3)
GOOD_COMPARABLES = max(int(os.getenv("GOOD_COMPARABLES", "12")), MIN_COMPARABLES)
HIGH_COMPARABLES = max(int(os.getenv("HIGH_COMPARABLES", "25")), GOOD_COMPARABLES)
MIN_CONFIDENCE_SCORE = min(
    max(int(os.getenv("MIN_CONFIDENCE_SCORE", "45")), 0),
    100,
)

MIN_MARKET_DISCOUNT_PERCENT = float(
    os.getenv("MIN_MARKET_DISCOUNT_PERCENT", "12")
)

# Price Engine Pro v1.8
# Limita l'impatto dei comparabili molto lontani dal prezzo centrale
# e richiede un vantaggio reale rispetto alla fascia bassa del mercato.
MAX_COMPARABLE_DEVIATION_PERCENT = float(
    os.getenv("MAX_COMPARABLE_DEVIATION_PERCENT", "45")
)
MIN_DISCOUNT_VS_LOW_PERCENT = float(
    os.getenv("MIN_DISCOUNT_VS_LOW_PERCENT", "5")
)

KEYWORDS = [
    value.strip().lower()
    for value in os.getenv(
        "KEYWORDS",
        (
            "hilti,festool,makita,bosch professional,leica,topcon,trimble,"
            "bici elettrica,ebike,engwe,ado,haibike,cube,specialized,trek,"
            "faema,la marzocco,rational,berkel,abbattitore,impastatrice,"
            "dyson,folletto,iphone,ipad,macbook,playstation,ps5,xbox,"
            "nintendo switch"
        ),
    ).split(",")
    if value.strip()
]

# Fonti mirate: evitiamo la pagina generica "vendita" e leggiamo
# direttamente le categorie dove è più probabile trovare prodotti utili.
DEFAULT_SOURCE_URLS = [
    "https://www.subito.it/annunci-piemonte/vendita/biciclette/",
    "https://www.subito.it/annunci-piemonte/vendita/telefonia/",
    "https://www.subito.it/annunci-piemonte/vendita/informatica/",
    "https://www.subito.it/annunci-piemonte/vendita/elettrodomestici/",
    "https://www.subito.it/annunci-piemonte/vendita/fotografia/",
]

configured_sources = [
    value.strip()
    for value in os.getenv("SOURCE_URLS", "").split(",")
    if value.strip()
]

# Se su Railway esiste ancora soltanto la vecchia pagina generica,
# usiamo automaticamente le categorie mirate.
GENERIC_SOURCE_URLS = {
    "https://www.subito.it/annunci-piemonte/vendita",
    "https://www.subito.it/annunci-piemonte/vendita/",
}

normalized_configured_sources = {
    value.rstrip("/")
    for value in configured_sources
}

if (
    not configured_sources
    or normalized_configured_sources
    == {value.rstrip("/") for value in GENERIC_SOURCE_URLS}
):
    SOURCE_URLS = DEFAULT_SOURCE_URLS
else:
    # Mantiene eventuali fonti personalizzate e aggiunge quelle principali,
    # evitando duplicati.
    SOURCE_URLS = list(dict.fromkeys(
        configured_sources + DEFAULT_SOURCE_URLS
    ))

configured_data_dir = os.getenv("DATA_DIR", "").strip()
if configured_data_dir:
    DATA_DIR = Path(configured_data_dir)
elif Path("/data").exists():
    DATA_DIR = Path("/data/radar_affari_ai")
else:
    DATA_DIR = Path(tempfile.gettempdir()) / "radar_affari_ai"

DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "radar_state.json"
SUBSCRIBERS_FILE = DATA_DIR / "radar_subscribers.json"
HISTORY_FILE = DATA_DIR / "radar_history.json"
STATS_FILE = DATA_DIR / "radar_stats.json"
DB_FILE = DATA_DIR / "radar_affari.sqlite3"
DATABASE_TARGET = os.getenv("DATABASE_URL", "").strip() or str(DB_FILE)
DB = RadarDatabase(DATABASE_TARGET)
DB.migrate_json_files(
    history_file=HISTORY_FILE,
    state_file=STATE_FILE,
    subscribers_file=SUBSCRIBERS_FILE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}

SCAN_LOCK = asyncio.Lock()

LAST_DECISION_DEBUG: List[Dict[str, Any]] = []
MAX_DECISION_DEBUG_ITEMS = 50



def source_label(url: str) -> str:
    path = urlparse(url).path.lower()
    for category in (
        "biciclette",
        "telefonia",
        "informatica",
        "elettrodomestici",
        "fotografia",
    ):
        if f"/{category}/" in path:
            return category.capitalize()
    return urlparse(url).netloc or "Fonte"


# ============================================================
# ARCHIVIO LOCALE
# ============================================================

def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Impossibile leggere %s: %s", path, exc)
        return default


def save_json(path: Path, data: Any) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.error("Impossibile salvare %s: %s", path, exc)


def subscribers() -> List[int]:
    return DB.subscribers()


def add_subscriber(chat_id: int) -> None:
    DB.add_subscriber(chat_id)


def remove_subscriber(chat_id: int) -> None:
    DB.remove_subscriber(chat_id)


# ============================================================
# FUNZIONI DI PULIZIA E RICONOSCIMENTO
# ============================================================

def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def create_item_id(url: str) -> str:
    clean_url = url.split("?")[0].rstrip("/")
    return hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:20]


def parse_price(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return number if 5 <= number <= 500000 else None

    text = normalize_text(str(value)).replace("\u00a0", " ")
    if not text:
        return None

    def convert_number(raw: str) -> Optional[float]:
        cleaned = raw.strip().replace(" ", "")

        if "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")

        try:
            number = float(cleaned)
        except ValueError:
            return None

        return number if 5 <= number <= 500000 else None

    number_pattern = (
        r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?"
        r"|\d+(?:,\d{1,2})?)"
    )

    euro_matches = re.findall(
        rf"(?<!\d){number_pattern}\s*(?:€|euro\b)",
        text,
        flags=re.IGNORECASE,
    )

    euro_values = [
        number
        for raw in euro_matches
        if (number := convert_number(raw)) is not None
    ]

    if euro_values:
        return euro_values[-1]

    context_matches = re.findall(
        rf"(?:prezzo|richiesta|chiedo|vendo\s+a|vendita\s+a|"
        rf"a\s+soli|offerta)\s*[:\-]?\s*{number_pattern}",
        text,
        flags=re.IGNORECASE,
    )

    context_values = [
        number
        for raw in context_matches
        if (number := convert_number(raw)) is not None
    ]

    return context_values[-1] if context_values else None


def title_from_url(url: str) -> str:
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d{5,}\.html?$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"\.html?$", "", slug, flags=re.IGNORECASE)
    slug = slug.replace("-", " ").replace("_", " ")
    return normalize_text(slug)


def matching_keywords(text: str) -> List[str]:
    lowered = normalize_text(text).lower()
    return [keyword for keyword in KEYWORDS if keyword in lowered]


def identify_product(text: str) -> Dict[str, Any]:
    """Usa il catalogo prodotti v2 con controlli anti-falso-positivo."""
    result = catalog_identify_product(text)
    normalized = normalize_text(text).lower()

    detected_model = str(result.get("model") or "").strip().lower()
    product_key = str(result.get("product_key") or "").strip().lower()

    # Sicurezza: modelli iPhone nuovi/non presenti nel catalogo non devono
    # essere ricondotti per somiglianza a modelli precedenti.
    mentions_iphone_17 = bool(
        re.search(r"\biphone\s*17\b|\biphone\s*17\s*(air|pro|max|pro\s*max)\b", normalized)
    )
    mentions_iphone_air = bool(re.search(r"\biphone\s*(17\s*)?air\b", normalized))

    if mentions_iphone_17 and "17" not in detected_model and ":17" not in product_key:
        return {
            "brand": "",
            "model": "",
            "variant": "",
            "storage": "",
            "recognition_confidence": 0,
            "product_key": "unidentified",
        }

    if mentions_iphone_air and "air" not in detected_model and ":air" not in product_key:
        return {
            "brand": "",
            "model": "",
            "variant": "",
            "storage": "",
            "recognition_confidence": 0,
            "product_key": "unidentified",
        }

    return result

def is_probable_listing_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path.lower().rstrip("/")

    if not host or not path:
        return False

    if "subito.it" in host:
        excluded_sections = (
            "/cerco-lavoro/",
            "/offerte-lavoro/",
            "/lavoro-servizi/",
            "/lavoro_servizi/",
            "/uffici-locali-commerciali/",
            "/appartamenti/",
            "/case-vacanze/",
            "/case_vacanze/",
            "/terreni-rustici/",
            "/terreni_rustici/",
            "/ville-singole-e-a-schiera/",
            "/garage-e-box/",
            "/auto/",
            "/moto-e-scooter/",
            "/veicoli-commerciali/",
        )
        if any(section in f"{path}/" for section in excluded_sections):
            return False
        return bool(re.search(r"-\d{5,}\.html?$", path))

    if "ebay." in host:
        return "/itm/" in path

    if "vinted." in host:
        return "/items/" in path

    return False


def risk_analysis(text: str) -> Dict[str, Any]:
    lowered = normalize_text(text).lower()

    high_risk_terms = [
        "non funzionante", "non funziona", "da riparare", "da sistemare",
        "per ricambi", "rotto", "guasto", "non testato",
        "non so se funziona", "senza garanzia", "bloccato",
        "account bloccato", "imei bloccato",
    ]

    medium_risk_terms = [
        "senza caricatore", "senza batteria", "manca", "difetto",
        "segni di usura", "solo spedizione", "visto e piaciuto",
    ]

    found_high = [term for term in high_risk_terms if term in lowered]
    found_medium = [term for term in medium_risk_terms if term in lowered]

    score = min(100, len(found_high) * 35 + len(found_medium) * 15)
    level = "ALTO" if found_high else "MEDIO" if found_medium else "BASSO"

    return {
        "score": score,
        "level": level,
        "reasons": (found_high + found_medium)[:4],
    }


# ============================================================
# ESTRAZIONE ANNUNCI
# ============================================================

def walk_json(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_price_from_object(obj: Dict[str, Any]) -> Optional[float]:
    possible_values = [
        obj.get("price"),
        obj.get("priceValue"),
        obj.get("amount"),
        obj.get("value"),
    ]

    offers = obj.get("offers")
    if isinstance(offers, dict):
        possible_values.extend([
            offers.get("price"),
            offers.get("lowPrice"),
            offers.get("highPrice"),
        ])

    for value in possible_values:
        if isinstance(value, dict):
            for nested_key in ("value", "amount", "price"):
                parsed = parse_price(value.get(nested_key))
                if parsed is not None:
                    return parsed
        else:
            parsed = parse_price(value)
            if parsed is not None:
                return parsed

    return None


def add_item(
    items: Dict[str, Dict[str, Any]],
    source_url: str,
    candidate_url: str,
    candidate_title: str = "",
    candidate_text: str = "",
    candidate_price: Optional[float] = None,
) -> None:
    absolute_url = urljoin(source_url, candidate_url).split("#")[0]

    if not is_probable_listing_url(absolute_url):
        return

    title = normalize_text(candidate_title)
    full_text = normalize_text(candidate_text)

    if len(title) < 4:
        title = title_from_url(absolute_url)

    combined_text = normalize_text(f"{title} {full_text}")
    matched = matching_keywords(combined_text)

    if len(title) < 4:
        return

    product = identify_product(combined_text)
    item_id = create_item_id(absolute_url)

    text_price = parse_price(full_text)
    price = candidate_price if candidate_price is not None else text_price

    new_item = {
        "id": item_id,
        "title": title[:180],
        "url": absolute_url,
        "text": full_text[:1000],
        "price": price,
        "matched": matched,
        "brand": product["brand"],
        "model": product["model"],
        "variant": product.get("variant", ""),
        "storage": product.get("storage", ""),
        "recognition_confidence": product.get("recognition_confidence", 0),
        "product_key": product["product_key"],
        "excluded": bool(product.get("excluded", False)),
        "exclusion_reason": str(product.get("exclusion_reason") or ""),
        "source": "subito",
        "category": source_label(source_url).lower(),
        "source_url": source_url,
        "relevant": bool(matched) and not bool(product.get("excluded", False)),
    }

    current = items.get(item_id)
    if current is None:
        items[item_id] = new_item
        return

    if len(new_item["text"]) > len(current.get("text", "")):
        current["text"] = new_item["text"]
    if len(new_item["title"]) > len(current.get("title", "")):
        current["title"] = new_item["title"]
    if current.get("price") is None and price is not None:
        current["price"] = price

    current_keywords = set(current.get("matched", []))
    current_keywords.update(matched)
    current["matched"] = sorted(current_keywords)


def extract_from_json_data(
    data: Any,
    source_url: str,
    items: Dict[str, Dict[str, Any]],
) -> None:
    for obj in walk_json(data):
        url_value = (
            obj.get("url")
            or obj.get("itemUrl")
            or obj.get("webUrl")
            or obj.get("canonicalUrl")
        )

        title_value = (
            obj.get("name")
            or obj.get("title")
            or obj.get("subject")
            or obj.get("headline")
        )

        description = (
            obj.get("description")
            or obj.get("body")
            or obj.get("text")
            or ""
        )

        if isinstance(url_value, str):
            add_item(
                items=items,
                source_url=source_url,
                candidate_url=url_value,
                candidate_title=str(title_value or ""),
                candidate_text=str(description or ""),
                candidate_price=extract_price_from_object(obj),
            )


def extract_from_html(
    soup: BeautifulSoup,
    source_url: str,
    items: Dict[str, Dict[str, Any]],
) -> None:
    for link in soup.find_all("a", href=True):
        href = normalize_text(str(link.get("href", "")))
        if not href:
            continue

        absolute_url = urljoin(source_url, href)
        if not is_probable_listing_url(absolute_url):
            continue

        candidates: List[str] = []

        for attribute in ("aria-label", "title", "data-title"):
            value = link.get(attribute)
            if isinstance(value, str):
                candidates.append(value)

        visible_text = link.get_text(" ", strip=True)
        if visible_text:
            candidates.append(visible_text)

        image = link.find("img")
        if image is not None:
            for attribute in ("alt", "title"):
                value = image.get(attribute)
                if isinstance(value, str):
                    candidates.append(value)

        container = link.find_parent(["article", "li", "div"])
        container_text = ""
        if container is not None:
            container_text = normalize_text(container.get_text(" ", strip=True))
            if container_text:
                candidates.append(container_text)

        title_candidates = [
            normalize_text(candidate)
            for candidate in candidates
            if 4 <= len(normalize_text(candidate)) <= 220
        ]

        title = max(title_candidates, key=len, default="")
        if len(title) > 180:
            shorter = [value for value in title_candidates if len(value) <= 180]
            title = max(shorter, key=len, default=title_from_url(absolute_url))

        add_item(
            items=items,
            source_url=source_url,
            candidate_url=absolute_url,
            candidate_title=title,
            candidate_text=container_text or visible_text,
        )


async def extract_items(url: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    diagnostics: Dict[str, Any] = {
        "url": url,
        "status": None,
        "links": 0,
        "listing_urls": 0,
        "extracted": 0,
        "priced": 0,
        "error": "",
    }

    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        diagnostics["status"] = response.status_code
        final_url = str(response.url)

        soup = BeautifulSoup(response.text, "html.parser")
        diagnostics["links"] = len(soup.find_all("a", href=True))

        items: Dict[str, Dict[str, Any]] = {}

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = script.string or script.get_text()
                if raw:
                    extract_from_json_data(json.loads(raw), final_url, items)
            except (json.JSONDecodeError, TypeError):
                continue

        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data is not None:
            try:
                raw = next_data.string or next_data.get_text()
                if raw:
                    extract_from_json_data(json.loads(raw), final_url, items)
            except json.JSONDecodeError:
                log.warning("__NEXT_DATA__ presente ma non leggibile.")

        extract_from_html(soup, final_url, items)

        all_listings = list(items.values())
        relevant_items = [
            item for item in all_listings
            if item.get("matched") and not item.get("excluded")
        ]

        diagnostics["listing_urls"] = len(all_listings)
        diagnostics["extracted"] = len(relevant_items)
        diagnostics["priced"] = sum(
            1 for item in relevant_items
            if item.get("price") is not None
        )
        diagnostics["archivable"] = sum(
            1 for item in all_listings
            if item.get("id") and item.get("url")
        )
        diagnostics["archivable_priced"] = sum(
            1 for item in all_listings
            if item.get("price") is not None
        )

        # Il collector riceve tutti gli annunci validi della categoria.
        # La selezione per parole chiave avviene dopo il salvataggio SQLite.
        return all_listings, diagnostics

    except Exception as exc:
        diagnostics["error"] = str(exc)
        log.exception("Errore estrazione %s: %s", url, exc)
        return [], diagnostics



async def extract_debug_samples(
    url: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    Restituisce alcuni annunci riconosciuti prima del filtro KEYWORDS.
    Serve esclusivamente per diagnosticare titolo, testo, prezzo e URL.
    """
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    final_url = str(response.url)
    soup = BeautifulSoup(response.text, "html.parser")
    items: Dict[str, Dict[str, Any]] = {}

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or script.get_text()
            if raw:
                extract_from_json_data(
                    json.loads(raw),
                    final_url,
                    items,
                )
        except (json.JSONDecodeError, TypeError):
            continue

    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data is not None:
        try:
            raw = next_data.string or next_data.get_text()
            if raw:
                extract_from_json_data(
                    json.loads(raw),
                    final_url,
                    items,
                )
        except json.JSONDecodeError:
            pass

    extract_from_html(soup, final_url, items)

    samples = list(items.values())[:limit]

    for sample in samples:
        combined = normalize_text(
            f"{sample.get('title', '')} {sample.get('text', '')}"
        )
        sample["debug_matches"] = matching_keywords(combined)

    return samples

# ============================================================
# STORICO E ANALISI ECONOMICA
# ============================================================

def update_history(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Modalità COLLECTOR:
    - salva ogni annuncio pertinente con prezzo;
    - aggiorna gli annunci già conosciuti;
    - conserva prima e ultima rilevazione;
    - registra eventuali variazioni di prezzo.
    """
    history = load_json(HISTORY_FILE, [])
    now = datetime.now(timezone.utc).isoformat()

    rows_by_id: Dict[str, Dict[str, Any]] = {
        str(row.get("id")): row
        for row in history
        if isinstance(row, dict) and row.get("id")
    }

    for item in items:
        item_id = str(item.get("id") or "")
        price = item.get("price")

        if not item_id or not isinstance(price, (int, float)):
            continue

        current_price = float(price)
        existing = rows_by_id.get(item_id)

        if existing is None:
            row = {
                "id": item_id,
                "product_key": item.get("product_key") or "",
                "brand": item.get("brand") or "",
                "model": item.get("model") or "",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source_url": item.get("source_url", ""),
                "first_seen": now,
                "last_seen": now,
                "observations": 1,
                "price": current_price,
                "price_history": [
                    {"at": now, "price": current_price}
                ],
            }
            history.append(row)
            rows_by_id[item_id] = row
            continue

        existing["last_seen"] = now
        existing["observations"] = int(existing.get("observations", 0)) + 1
        existing["title"] = item.get("title", existing.get("title", ""))
        existing["url"] = item.get("url", existing.get("url", ""))
        existing["product_key"] = (
            item.get("product_key")
            or existing.get("product_key", "")
        )
        existing["brand"] = item.get("brand") or existing.get("brand", "")
        existing["model"] = item.get("model") or existing.get("model", "")

        old_price = existing.get("price")
        if not isinstance(old_price, (int, float)) or float(old_price) != current_price:
            price_history = existing.setdefault("price_history", [])
            price_history.append({"at": now, "price": current_price})
            existing["price"] = current_price

    history = history[-30000:]
    save_json(HISTORY_FILE, history)
    return history


def update_collection_stats(
    diagnostics: List[Dict[str, Any]],
    relevant_items: List[Dict[str, Any]],
    new_items_count: int,
) -> Dict[str, Any]:
    stats = load_json(
        STATS_FILE,
        {
            "scans": 0,
            "total_relevant_seen": 0,
            "total_new_seen": 0,
            "last_scan": None,
        },
    )

    stats["scans"] = int(stats.get("scans", 0)) + 1
    stats["total_relevant_seen"] = int(
        stats.get("total_relevant_seen", 0)
    ) + len(relevant_items)
    stats["total_new_seen"] = int(
        stats.get("total_new_seen", 0)
    ) + new_items_count
    stats["last_scan"] = datetime.now(timezone.utc).isoformat()
    stats["last_sources"] = diagnostics

    save_json(STATS_FILE, stats)
    return stats


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def percentile(values: List[float], fraction: float) -> float:
    """Percentile lineare senza dipendenze esterne."""
    if not values:
        raise ValueError("Lista vuota")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * min(max(fraction, 0.0), 1.0)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index

    return (
        ordered[lower_index] * (1 - weight)
        + ordered[upper_index] * weight
    )


def remove_price_outliers(prices: List[float]) -> List[float]:
    """
    Elimina prezzi anomali usando l'intervallo interquartile.

    Con pochi dati mantiene tutti i prezzi: è preferibile dichiarare una
    bassa attendibilità piuttosto che eliminare confronti arbitrariamente.
    """
    clean = sorted(
        float(price)
        for price in prices
        if isinstance(price, (int, float)) and float(price) > 0
    )

    if len(clean) < 5:
        return clean

    q1 = percentile(clean, 0.25)
    q3 = percentile(clean, 0.75)
    iqr = q3 - q1

    if iqr <= 0:
        return clean

    lower_limit = max(5.0, q1 - 1.5 * iqr)
    upper_limit = q3 + 1.5 * iqr

    filtered = [
        price for price in clean
        if lower_limit <= price <= upper_limit
    ]

    # Non consentiamo al filtro di cancellare quasi tutto il campione.
    return filtered if len(filtered) >= 3 else clean


def market_confidence(
    prices: List[float],
    newest_seen: Optional[datetime],
) -> Tuple[int, str]:
    """
    Calcola un punteggio 0-100 basato su:
    - quantità di confronti;
    - dispersione dei prezzi;
    - freschezza del campione.
    """
    count = len(prices)
    if count == 0:
        return 0, "INSUFFICIENTE"

    if count >= HIGH_COMPARABLES:
        count_score = 60
    elif count >= GOOD_COMPARABLES:
        count_score = 48
    elif count >= MIN_COMPARABLES:
        count_score = 34
    else:
        count_score = min(25, count * 5)

    med = float(median(prices))
    q1 = percentile(prices, 0.25)
    q3 = percentile(prices, 0.75)
    relative_spread = (q3 - q1) / med if med > 0 else 1.0

    if relative_spread <= 0.15:
        spread_score = 30
    elif relative_spread <= 0.25:
        spread_score = 24
    elif relative_spread <= 0.40:
        spread_score = 16
    else:
        spread_score = 6

    freshness_score = 0
    if newest_seen is not None:
        age_days = max(
            0,
            (datetime.now(timezone.utc) - newest_seen).days,
        )
        if age_days <= 7:
            freshness_score = 10
        elif age_days <= 30:
            freshness_score = 7
        elif age_days <= MARKET_LOOKBACK_DAYS:
            freshness_score = 4

    score = min(100, count_score + spread_score + freshness_score)

    if score >= 80:
        label = "MOLTO ALTA"
    elif score >= 65:
        label = "ALTA"
    elif score >= 50:
        label = "DISCRETA"
    elif score >= MIN_CONFIDENCE_SCORE:
        label = "PRUDENTE"
    else:
        label = "BASSA"

    return score, label


def listing_condition_bucket(text: str) -> str:
    lowered = normalize_text(text).lower()

    damaged_terms = (
        "non funzionante", "non funziona", "da riparare", "da sistemare",
        "per ricambi", "rotto", "guasto", "non testato", "bloccato",
    )
    incomplete_terms = (
        "solo corpo", "solo console", "senza caricatore",
        "senza batteria", "senza accessori", "incompleto",
    )
    premium_terms = (
        "nuovo", "mai usato", "sigillato", "pari al nuovo",
    )

    if any(term in lowered for term in damaged_terms):
        return "damaged"
    if any(term in lowered for term in incomplete_terms):
        return "incomplete"
    if any(term in lowered for term in premium_terms):
        return "premium"
    return "standard"


def is_precise_product_key(key: str) -> bool:
    normalized = normalize_text(key).lower()
    return bool(normalized and ":" in normalized)


def comparable_rows_for_item(
    item: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    candidate_text = (
        f"{item.get('title', '')} "
        f"{item.get('text', item.get('description', ''))}"
    )
    candidate_bucket = listing_condition_bucket(candidate_text)

    filtered: List[Dict[str, Any]] = []

    for row in rows:
        row_text = (
            f"{row.get('title', '')} "
            f"{row.get('text', row.get('description', ''))}"
        )
        row_bucket = listing_condition_bucket(row_text)

        if candidate_bucket == "damaged":
            if row_bucket != "damaged":
                continue
        elif candidate_bucket == "incomplete":
            if row_bucket not in {"incomplete", "damaged"}:
                continue
        elif candidate_bucket == "premium":
            # "Come nuovo", "pari al nuovo" e "nuovo" non devono azzerare
            # il campione. Accettiamo premium e standard, ma mai rotti/incompleti.
            if row_bucket in {"damaged", "incomplete"}:
                continue
        else:
            if row_bucket in {"damaged", "incomplete"}:
                continue

        filtered.append(row)

    return filtered

def comparable_quality_score(
    item: Dict[str, Any],
    row: Dict[str, Any],
) -> int:
    """Assegna un punteggio 0-100 alla qualità del comparabile.

    Il product_key è già uguale perché la query DB lo impone. Il punteggio
    premia condizioni simili e parole rilevanti condivise, senza pretendere
    informazioni che spesso gli annunci non contengono.
    """
    score = 60

    item_text = normalize_text(
        f"{item.get('title', '')} {item.get('text', '')}"
    ).lower()
    row_text = normalize_text(
        f"{row.get('title', '')} "
        f"{row.get('text', row.get('description', ''))}"
    ).lower()

    if listing_condition_bucket(item_text) == listing_condition_bucket(row_text):
        score += 20

    item_tokens = {
        token for token in re.findall(r"[a-z0-9]+", item_text)
        if len(token) >= 3
    }
    row_tokens = {
        token for token in re.findall(r"[a-z0-9]+", row_text)
        if len(token) >= 3
    }

    shared = item_tokens.intersection(row_tokens)
    score += min(len(shared) * 2, 20)

    return min(score, 100)


def weighted_median(
    values_and_weights: List[Tuple[float, float]],
) -> float:
    """Mediana pesata senza dipendenze esterne."""
    if not values_and_weights:
        raise ValueError("Campione pesato vuoto")

    ordered = sorted(values_and_weights, key=lambda pair: pair[0])
    total_weight = sum(max(weight, 0.0) for _, weight in ordered)

    if total_weight <= 0:
        return float(median([value for value, _ in ordered]))

    threshold = total_weight / 2
    cumulative = 0.0

    for value, weight in ordered:
        cumulative += max(weight, 0.0)
        if cumulative >= threshold:
            return float(value)

    return float(ordered[-1][0])


def filter_by_central_deviation(
    prices: List[float],
) -> List[float]:
    """Secondo filtro prudenziale attorno alla mediana.

    Serve soprattutto con campioni piccoli nei quali l'IQR può non eliminare
    un annuncio chiaramente distante dal resto del mercato.
    """
    if len(prices) < 4:
        return prices

    center = float(median(prices))
    if center <= 0:
        return prices

    max_deviation = MAX_COMPARABLE_DEVIATION_PERCENT / 100
    filtered = [
        price for price in prices
        if abs(price - center) / center <= max_deviation
    ]

    return filtered if len(filtered) >= MIN_COMPARABLES else prices

def estimate_market_values(items: List[Dict[str, Any]]) -> None:
    """Price Engine Pro v1.8.

    Usa soltanto comparabili attivi, con product_key preciso e condizione
    omogenea. Applica due filtri anti-anomalia e una mediana pesata.
    Restituisce anche una fascia di mercato e dettagli su esclusioni e qualità.
    """
    for item in items:
        asking_price = item.get("price")
        key = str(item.get("product_key") or "").strip().lower()
        item_id = str(item.get("id") or "")

        comparable_rows = DB.active_comparables(
            key,
            exclude_listing_id=item_id,
            max_age_days=MARKET_LOOKBACK_DAYS,
        ) if is_precise_product_key(key) else []

        rows_before_condition_filter = len(comparable_rows)
        comparable_rows = comparable_rows_for_item(item, comparable_rows)
        condition_excluded = max(
            0,
            rows_before_condition_filter - len(comparable_rows),
        )

        priced_rows: List[Tuple[Dict[str, Any], float]] = []
        for row in comparable_rows:
            price = row.get("price")
            if isinstance(price, (int, float)) and float(price) > 0:
                priced_rows.append((row, float(price)))

        raw_prices = [price for _, price in priced_rows]
        iqr_prices = remove_price_outliers(raw_prices)
        comparable_prices = filter_by_central_deviation(iqr_prices)

        allowed_prices = set(comparable_prices)
        weighted_prices: List[Tuple[float, float]] = []

        for row, price in priced_rows:
            if price not in allowed_prices:
                continue
            quality = comparable_quality_score(item, row)
            weighted_prices.append((price, max(quality, 1)))

        newest_seen = max(
            (
                parse_iso_datetime(row.get("last_seen_at"))
                for row, price in priced_rows
                if price in allowed_prices
            ),
            default=None,
        )

        confidence_score, confidence_label = market_confidence(
            comparable_prices,
            newest_seen,
        )

        outliers_removed = max(0, len(raw_prices) - len(comparable_prices))
        average_quality = (
            round(
                sum(weight for _, weight in weighted_prices)
                / len(weighted_prices),
                1,
            )
            if weighted_prices else 0.0
        )

        item["raw_comparables"] = rows_before_condition_filter
        item["priced_comparables"] = len(raw_prices)
        item["comparables"] = len(comparable_prices)
        item["condition_excluded"] = condition_excluded
        item["outliers_removed"] = outliers_removed
        item["average_comparable_quality"] = average_quality
        item["confidence_score"] = confidence_score
        item["confidence_label"] = confidence_label
        item["market_lookback_days"] = MARKET_LOOKBACK_DAYS

        if comparable_prices:
            market_min = min(comparable_prices)
            market_low = percentile(comparable_prices, 0.25)
            market_high = percentile(comparable_prices, 0.75)

            item["market_min"] = round(market_min, 2)
            item["market_low"] = round(market_low, 2)
            item["market_high"] = round(market_high, 2)
        else:
            item["market_min"] = None
            item["market_low"] = None
            item["market_high"] = None

        if (
            asking_price is None
            or not is_precise_product_key(key)
            or len(comparable_prices) < MIN_COMPARABLES
            or confidence_score < MIN_CONFIDENCE_SCORE
            or not weighted_prices
        ):
            item["market_value"] = None
            item["quick_sale_value"] = None
            item["estimated_costs"] = None
            item["estimated_margin"] = None
            item["maximum_buy_price"] = None
            item["roi"] = None
            item["discount_vs_market_low"] = None
            continue

        market_value = weighted_median(weighted_prices)

        # Rivendita prudente: il più basso tra il 20° percentile e il 90%
        # della mediana pesata. Non usa mai il minimo assoluto come riferimento.
        quick_sale_value = min(
            percentile(comparable_prices, 0.20),
            market_value * 0.90,
        )

        estimated_costs = max(25.0, float(asking_price) * 0.06)
        maximum_buy_price = max(
            0.0,
            quick_sale_value - estimated_costs - MIN_MARGIN_EURO,
        )
        estimated_margin = (
            quick_sale_value - float(asking_price) - estimated_costs
        )
        roi = (
            estimated_margin / float(asking_price) * 100
            if float(asking_price) > 0
            else 0
        )

        market_low = float(item["market_low"])
        discount_vs_market_low = (
            (market_low - float(asking_price)) / market_low * 100
            if market_low > 0
            else 0
        )

        item["market_value"] = round(market_value, 2)
        item["quick_sale_value"] = round(quick_sale_value, 2)
        item["estimated_costs"] = round(estimated_costs, 2)
        item["maximum_buy_price"] = round(maximum_buy_price, 2)
        item["estimated_margin"] = round(estimated_margin, 2)
        item["roi"] = round(roi, 1)
        item["discount_vs_market_low"] = round(
            discount_vs_market_low,
            1,
        )

def analyze_deal(item: Dict[str, Any]) -> Dict[str, Any]:
    """Trasforma i dati economici in una decisione spiegabile."""
    risk = risk_analysis(f"{item.get('title', '')} {item.get('text', '')}")
    margin = item.get("estimated_margin")
    roi = item.get("roi")
    market_value = item.get("market_value")
    quick_sale_value = item.get("quick_sale_value")
    asking_price = item.get("price")
    costs = item.get("estimated_costs")
    confidence = int(item.get("confidence_score") or 0)
    discount_vs_market_low = item.get("discount_vs_market_low")
    precise_model = bool(item.get("model")) and is_precise_product_key(
        str(item.get("product_key") or "")
    )

    reasons: List[str] = []
    warnings: List[str] = []

    if not precise_model:
        warnings.append("modello o variante non identificati con precisione")
    if confidence < MIN_CONFIDENCE_SCORE:
        warnings.append("campione di mercato non ancora abbastanza attendibile")
    if risk["reasons"]:
        warnings.extend(f"rischio: {reason}" for reason in risk["reasons"][:3])

    score = 0.0
    discount_percent: Optional[float] = None
    max_offer: Optional[float] = None

    if (
        isinstance(asking_price, (int, float))
        and isinstance(market_value, (int, float))
        and market_value > 0
    ):
        discount_percent = (market_value - asking_price) / market_value * 100
        score += max(0.0, min(discount_percent, 30.0))
        if discount_percent >= 20:
            reasons.append("prezzo almeno 20% sotto il valore mediano")
        elif discount_percent >= MIN_MARKET_DISCOUNT_PERCENT:
            reasons.append("prezzo realmente sotto il valore mediano")
        else:
            warnings.append(
                f"prezzo troppo vicino al mercato: sconto {discount_percent:.1f}%"
            )

    if isinstance(discount_vs_market_low, (int, float)):
        if discount_vs_market_low >= MIN_DISCOUNT_VS_LOW_PERCENT:
            reasons.append(
                f"prezzo {discount_vs_market_low:.1f}% sotto la fascia bassa"
            )
        else:
            warnings.append(
                "prezzo non abbastanza sotto la fascia bassa del mercato"
            )

    if isinstance(roi, (int, float)):
        score += max(0.0, min(float(roi), 25.0))
        if roi >= MIN_ROI_PERCENT:
            reasons.append(f"ROI stimato almeno {MIN_ROI_PERCENT:.0f}%")

    if isinstance(margin, (int, float)):
        score += max(0.0, min(float(margin) / 8.0, 20.0))
        if margin >= MIN_MARGIN_EURO:
            reasons.append(f"margine stimato almeno {MIN_MARGIN_EURO:.0f} €")

    score += min(confidence / 100 * 15, 15)
    if confidence >= 65:
        reasons.append("stima di mercato con buona attendibilità")

    if precise_model:
        score += 10
        reasons.append("modello identificato con precisione")

    score -= {"BASSO": 0, "MEDIO": 15, "ALTO": 45}.get(risk["level"], 20)
    radar_score = int(round(max(0.0, min(score, 100.0))))

    if (
        isinstance(quick_sale_value, (int, float))
        and isinstance(costs, (int, float))
    ):
        max_offer = max(0.0, quick_sale_value - costs - MIN_MARGIN_EURO)

    if risk["level"] == "ALTO":
        decision = "SCARTA"
        label = "SCARTA: RISCHIO ALTO"
    elif margin is None or roi is None or confidence < MIN_CONFIDENCE_SCORE:
        decision = "DATI_INSUFFICIENTI"
        label = "DATI INSUFFICIENTI: NON COMPRARE ANCORA"
    elif not precise_model:
        decision = "DATI_INSUFFICIENTI"
        label = "DATI INSUFFICIENTI: MODELLO O VARIANTE DA CONFERMARE"
    elif discount_percent is None or discount_percent < MIN_MARKET_DISCOUNT_PERCENT:
        decision = "SCARTA"
        label = "SCARTA: PREZZO TROPPO VICINO AL MERCATO"
    elif (
        not isinstance(discount_vs_market_low, (int, float))
        or float(discount_vs_market_low) < MIN_DISCOUNT_VS_LOW_PERCENT
    ):
        decision = "SCARTA"
        label = "SCARTA: NON È SOTTO LA FASCIA BASSA DEL MERCATO"
    elif (
        isinstance(max_offer, (int, float))
        and isinstance(asking_price, (int, float))
        and float(asking_price) > float(max_offer)
    ):
        decision = "MONITORA"
        label = "MONITORA: COMPRA SOLO DOPO UNA TRATTATIVA"
    elif float(margin) >= MIN_MARGIN_EURO and float(roi) >= MIN_ROI_PERCENT:
        if risk["level"] == "BASSO":
            decision = "COMPRA"
            label = "COMPRA: CONTATTA E VERIFICA SUBITO"
        else:
            decision = "TRATTA"
            label = "TRATTA E VERIFICA PRIMA DI COMPRARE"
    elif float(margin) >= MIN_MARGIN_EURO * 0.5 or float(roi) >= MIN_ROI_PERCENT * 0.5:
        decision = "MONITORA"
        label = "MONITORA: INTERESSANTE SOLO A PREZZO PIÙ BASSO"
    else:
        decision = "SCARTA"
        label = "SCARTA: MARGINE O ROI INSUFFICIENTI"

    return {
        "decision": decision,
        "label": label,
        "radar_score": radar_score,
        "confidence": confidence,
        "risk": risk,
        "discount_percent": round(discount_percent, 1) if discount_percent is not None else None,
        "max_offer": round(max_offer, 2) if max_offer is not None else None,
        "reasons": reasons[:5],
        "warnings": warnings[:5],
    }


def is_valid_deal(item: Dict[str, Any]) -> bool:
    return analyze_deal(item)["decision"] in {"COMPRA", "TRATTA"}


def opportunity_score(item: Dict[str, Any]) -> float:
    analysis = analyze_deal(item)
    margin = max(float(item.get("estimated_margin") or 0), 0)
    return round(analysis["radar_score"] * 10 + margin, 2)


def verdict_for(item: Dict[str, Any], risk: Dict[str, Any]) -> str:
    # Manteniamo la firma per compatibilità con il resto dell'applicazione.
    return analyze_deal(item)["label"]


def euro(value: Optional[float]) -> str:
    if value is None:
        return "non disponibile"
    return f"{value:,.0f} €".replace(",", ".")


def build_message(item: Dict[str, Any]) -> str:
    analysis = analyze_deal(item)
    risk = analysis["risk"]

    title = html.escape(str(item.get("title", "")))
    url = html.escape(str(item.get("url", "")), quote=True)
    product_name = " ".join(
        part for part in [item.get("brand", ""), item.get("model", "")] if part
    ) or "non identificato"

    reasons = "\n".join(
        f"✅ {html.escape(reason)}" for reason in analysis["reasons"]
    ) or "• nessun elemento positivo sufficiente"
    warnings = "\n".join(
        f"⚠️ {html.escape(warning)}" for warning in analysis["warnings"]
    ) or "• nessuna anomalia testuale evidente"

    discount = analysis["discount_percent"]
    discount_text = f"{discount:.1f}%" if discount is not None else "non disponibile"

    return (
        f"🎯 <b>RADAR SCORE {analysis['radar_score']}/100</b>\n"
        f"🚦 <b>{html.escape(analysis['label'])}</b>\n\n"
        f"<b>{title}</b>\n"
        f"🧩 Prodotto: <b>{html.escape(product_name)}</b>\n\n"
        f"💰 Prezzo richiesto: <b>{euro(item.get('price'))}</b>\n"
        f"📉 Fascia mercato: <b>{euro(item.get('market_low'))} – {euro(item.get('market_high'))}</b>\n"
        f"📊 Valore centrale pesato: <b>{euro(item.get('market_value'))}</b>\n"
        f"⚡ Rivendita rapida prudente: <b>{euro(item.get('quick_sale_value'))}</b>\n"
        f"🏷 Sconto sul mercato: <b>{discount_text}</b>\n"
        f"🤝 Offerta massima prudente: <b>{euro(analysis.get('max_offer'))}</b>\n"
        f"🧾 Costi prudenziali: <b>{euro(item.get('estimated_costs'))}</b>\n"
        f"💵 Margine stimato: <b>{euro(item.get('estimated_margin'))}</b>\n"
        f"📈 ROI stimato: <b>{item.get('roi', 'non disponibile')}%</b>\n"
        f"🛡 Attendibilità: <b>{item.get('confidence_label', 'INSUFFICIENTE')}</b> "
        f"({item.get('confidence_score', 0)}/100)\n"
        f"📚 Confronti validi: <b>{item.get('comparables', 0)}</b> "
        f"su {item.get('raw_comparables', 0)}\n"
        f"🧹 Esclusi per condizione: <b>{item.get('condition_excluded', 0)}</b>\n"
        f"🚫 Prezzi anomali esclusi: <b>{item.get('outliers_removed', 0)}</b>\n"
        f"🎯 Qualità media comparabili: <b>{item.get('average_comparable_quality', 0)}/100</b>\n"
        f"⚠️ Rischio: <b>{risk['level']}</b> ({risk['score']}/100)\n\n"
        f"<b>PERCHÉ</b>\n{reasons}\n\n"
        f"<b>DA VERIFICARE</b>\n{warnings}\n\n"
        f'<a href="{url}">APRI DIRETTAMENTE L’ANNUNCIO</a>\n\n'
        "Non acquistare senza prova e verifica manuale."
    )


# ============================================================
# SCANSIONE E TELEGRAM
# ============================================================

async def scan_once(application: Application) -> Dict[str, Any]:
    if SCAN_LOCK.locked():
        return {"busy": True, "new": 0, "valid": 0, "diagnostics": []}

    async with SCAN_LOCK:
        if not SOURCE_URLS:
            log.warning("Nessuna SOURCE_URL configurata.")
            return {"busy": False, "new": 0, "valid": 0, "diagnostics": []}

        scan_token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        all_extracted: List[Dict[str, Any]] = []
        diagnostics: List[Dict[str, Any]] = []

        for source_url in SOURCE_URLS:
            extracted_items, source_diagnostics = await extract_items(source_url)
            diagnostics.append(source_diagnostics)
            all_extracted.extend(extracted_items)

        # Deduplica eventuali annunci presenti in più fonti/categorie.
        unique_items = {str(item["id"]): item for item in all_extracted}.values()
        all_extracted = list(unique_items)

        # Salviamo nel database ogni annuncio valido trovato nelle
        # categorie configurate, anche quando non contiene una keyword.
        # Solo gli annunci pertinenti vengono poi valutati economicamente.
        new_archived_items: List[Dict[str, Any]] = []
        relevant_items: List[Dict[str, Any]] = []
        new_items: List[Dict[str, Any]] = []

        for item in all_extracted:
            is_new = DB.upsert_listing(item, scan_token=scan_token)
            if is_new:
                new_archived_items.append(item)
            if item.get("matched") and not item.get("excluded"):
                relevant_items.append(item)
                if is_new:
                    new_items.append(item)

        all_sources_ok = bool(diagnostics) and all(not row.get("error") for row in diagnostics)
        if all_sources_ok:
            DB.mark_missing_after_scan(scan_token, source="subito", grace_hours=24)

        update_collection_stats(
            diagnostics=diagnostics,
            relevant_items=relevant_items,
            new_items_count=len(new_items),
        )
        estimate_market_values(relevant_items)

        analyses: Dict[str, Dict[str, Any]] = {}
        decision_counts = {
            "COMPRA": 0, "TRATTA": 0, "MONITORA": 0,
            "SCARTA": 0, "DATI_INSUFFICIENTI": 0,
        }

        global LAST_DECISION_DEBUG
        current_debug_rows: List[Dict[str, Any]] = []

        for item in relevant_items:
            analysis = analyze_deal(item)
            analyses[item["id"]] = analysis

            current_debug_rows.append({
                "id": item.get("id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "product_key": item.get("product_key"),
                "brand": item.get("brand"),
                "model": item.get("model"),
                "storage": item.get("storage"),
                "price": item.get("price"),
                "market_low": item.get("market_low"),
                "market_high": item.get("market_high"),
                "market_value": item.get("market_value"),
                "quick_sale_value": item.get("quick_sale_value"),
                "estimated_margin": item.get("estimated_margin"),
                "roi": item.get("roi"),
                "comparables": item.get("comparables", 0),
                "raw_comparables": item.get("raw_comparables", 0),
                "priced_comparables": item.get("priced_comparables", 0),
                "confidence_score": item.get("confidence_score", 0),
                "confidence_label": item.get("confidence_label", "INSUFFICIENTE"),
                "decision": analysis.get("decision"),
                "label": analysis.get("label"),
                "radar_score": analysis.get("radar_score"),
                "max_offer": analysis.get("max_offer"),
                "reasons": analysis.get("reasons", []),
                "warnings": analysis.get("warnings", []),
            })
            item["decision"] = analysis["decision"]
            item["rejection_reason"] = "; ".join(analysis.get("warnings", []))
            decision_counts[analysis["decision"]] = decision_counts.get(analysis["decision"], 0) + 1
            DB.record_evaluation(
                item["id"],
                {
                    "asking_price": item.get("price"),
                    "estimated_sale_price": item.get("quick_sale_value"),
                    "maximum_buy_price": item.get("maximum_buy_price") or analysis.get("max_offer"),
                    "gross_margin": item.get("estimated_margin"),
                    "net_margin": item.get("estimated_margin"),
                    "roi": item.get("roi"),
                    "comparables_count": item.get("comparables", 0),
                    "confidence": item.get("confidence_score", 0),
                    "decision": analysis["decision"],
                    "rejection_reason": item["rejection_reason"],
                },
            )

        LAST_DECISION_DEBUG = (
            current_debug_rows + LAST_DECISION_DEBUG
        )[:MAX_DECISION_DEBUG_ITEMS]

        valid_deals = [
            item for item in relevant_items
            if not DB.was_notified(item["id"])
            and analyses[item["id"]]["decision"] in {"COMPRA", "TRATTA"}
        ]
        valid_deals.sort(
            key=lambda item: (
                opportunity_score(item),
                float(item.get("estimated_margin") or 0),
                float(item.get("roi") or 0),
            ),
            reverse=True,
        )

        for item in valid_deals[:MAX_ALERTS_PER_SCAN]:
            message = build_message(item)
            sent_to_at_least_one = False
            for chat_id in subscribers():
                try:
                    await application.bot.send_message(
                        chat_id=chat_id, text=message, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    sent_to_at_least_one = True
                except Exception as exc:
                    log.warning("Invio fallito verso %s: %s", chat_id, exc)
            if sent_to_at_least_one:
                DB.mark_notified(item["id"], item.get("decision", "AFFARE"))

        log.info(
            "SCAN pertinenti=%s nuovi=%s affari_validi=%s iscritti=%s db=%s",
            len(relevant_items), len(new_items), len(valid_deals),
            len(subscribers()), DB.location_label,
        )
        return {
            "busy": False,
            "new": len(new_items),
            "archived": len(all_extracted),
            "new_archived": len(new_archived_items),
            "relevant": len(relevant_items),
            "valid": len(valid_deals),
            "buy": decision_counts.get("COMPRA", 0),
            "negotiate": decision_counts.get("TRATTA", 0),
            "monitor": decision_counts.get("MONITORA", 0),
            "discard": decision_counts.get("SCARTA", 0),
            "insufficient": decision_counts.get("DATI_INSUFFICIENTI", 0),
            "diagnostics": diagnostics,
        }



def reclassify_database_listings() -> Dict[str, int]:
    """Ricalcola la classificazione degli annunci già presenti."""
    counters = {
        "total": 0,
        "changed": 0,
        "recognized": 0,
        "unrecognized": 0,
    }
    placeholder = "%s" if DB.backend == "postgresql" else "?"

    with DB.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, description, brand, model, product_key
            FROM listings
            ORDER BY id
            """
        ).fetchall()

        for row in rows:
            counters["total"] += 1
            product = identify_product(
                f"{row['title'] or ''} {row['description'] or ''}"
            )

            new_brand = str(product.get("brand") or "")
            new_model = str(product.get("model") or "")
            new_variant = str(product.get("variant") or "")
            new_storage = str(product.get("storage") or "")
            new_key = str(product.get("product_key") or "").lower()
            confidence = int(product.get("recognition_confidence") or 0)

            if new_key:
                counters["recognized"] += 1
            else:
                counters["unrecognized"] += 1

            old_key = str(row["product_key"] or "").lower()
            old_brand = str(row["brand"] or "")
            old_model = str(row["model"] or "")

            if (
                new_key == old_key
                and new_brand == old_brand
                and new_model == old_model
            ):
                continue

            conn.execute(
                f"""
                UPDATE listings
                SET brand = {placeholder},
                    model = {placeholder},
                    variant = {placeholder},
                    storage = {placeholder},
                    product_key = {placeholder},
                    recognition_confidence = {placeholder},
                    updated_at = {placeholder}
                WHERE id = {placeholder}
                """,
                (
                    new_brand,
                    new_model,
                    new_variant,
                    new_storage,
                    new_key,
                    confidence,
                    datetime.now(timezone.utc).isoformat(),
                    str(row["id"]),
                ),
            )
            counters["changed"] += 1

    return counters


async def reclassify(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🧠 Riclassificazione v2 in corso…")

    try:
        result = reclassify_database_listings()
    except Exception as exc:
        log.exception("Riclassificazione v2 fallita")
        await update.message.reply_text(
            f"❌ Riclassificazione fallita:\n{str(exc)[:500]}"
        )
        return

    percentage = (
        result["recognized"] / result["total"] * 100
        if result["total"] else 0
    )

    await update.message.reply_text(
        "✅ RICLASSIFICAZIONE V2 COMPLETATA\n\n"
        f"Annunci analizzati: {result['total']}\n"
        f"Annunci modificati: {result['changed']}\n"
        f"Riconosciuti con chiave precisa: {result['recognized']}\n"
        f"Non identificati: {result['unrecognized']}\n"
        f"Tasso riconoscimento: {percentage:.1f}%\n\n"
        "Ora esegui /collector."
    )


# ============================================================
# COMANDI TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    add_subscriber(update.effective_chat.id)

    await update.message.reply_text(
        "✅ Radar Affari Decision Engine v3.2 Recognition + PostgreSQL + Vision attivato.\n\n"
        f"• Margine minimo: {MIN_MARGIN_EURO:.0f} €\n"
        f"• ROI minimo: {MIN_ROI_PERCENT:.0f}%\n"
        f"• Confronti minimi: {MIN_COMPARABLES}\n"
        f"• Storico usato: {MARKET_LOOKBACK_DAYS} giorni\n\n"
        "/status - stato del radar\n"
        "/fonti - diagnostica delle fonti\n"
        "/categorie - mostra le categorie attive\n"
        "/collector - stato archivio mercato\n"
        "/debug - mostra cosa legge il parser\n"
        "/test - prova Telegram\n"
        "/scan - scansione manuale\n"
        "/reclassify - riclassifica tutto lo storico\n"
        "/decisiondebug - dettaglio ultime valutazioni\n"
        "/topscarti - migliori scarti e quasi affari\n"
        "/topcompra - migliori compra e tratta\n"
        "/visiontest URL - analisi foto di un annuncio\n"
        "/reset - azzera memoria annunci\n"
        "/stop - disattiva avvisi"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    db_stats = DB.stats()
    stats = load_json(STATS_FILE, {"scans": 0})
    await update.message.reply_text(
        f"🧠 Versione: Decision Engine v3.2 Recognition + PostgreSQL + Vision\n"
        f"📡 Fonti configurate: {len(SOURCE_URLS)}\n"
        f"🔎 Parole chiave: {len(KEYWORDS)}\n"
        f"⏱ Controllo ogni {CHECK_MINUTES} minuti\n"
        f"💵 Margine minimo: {MIN_MARGIN_EURO:.0f} €\n"
        f"📈 ROI minimo: {MIN_ROI_PERCENT:.0f}%\n"
        f"🛡 Attendibilità minima: {MIN_CONFIDENCE_SCORE}/100\n"
        f"📅 Confronti attivi: ultimi {MARKET_LOOKBACK_DAYS} giorni\n"
        f"📚 Confronti minimi per stima: {MIN_COMPARABLES}\n"
        f"📉 Sconto minimo sul mercato: {MIN_MARKET_DISCOUNT_PERCENT:.0f}%\n"
        f"📉 Sconto minimo sulla fascia bassa: {MIN_DISCOUNT_VS_LOW_PERCENT:.0f}%\n"
        f"🗄 Annunci nel database: {db_stats['total']}\n"
        f"✅ Annunci attivi: {db_stats['active']}\n"
        f"⚠️ Annunci scomparsi: {db_stats['missing']}\n"
        f"🧩 Annunci riconosciuti: {db_stats['recognized']}\n"
        f"💶 Osservazioni prezzo: {db_stats['price_observations']}\n"
        f"🧮 Valutazioni salvate: {db_stats['evaluations']}\n"
        f"🔄 Scansioni registrate: {int(stats.get('scans', 0))}\n"
        f"👥 Iscritti: {len(subscribers())}\n"
        f"💾 Database: {DB.location_label}"
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "✅ TEST RADAR\n"
        "Collegamento Telegram ↔ applicazione funzionante."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🔎 Controllo in corso…")
    result = await scan_once(context.application)

    if result["busy"]:
        await update.message.reply_text(
            "⏳ Una scansione è già in corso. Riprova tra poco."
        )
        return

    await update.message.reply_text(
        "✅ CONTROLLO TERMINATO\n\n"
        f"Annunci letti e archiviati: {result.get('archived', 0)}\n"
        f"Nuovi nel database: {result.get('new_archived', 0)}\n"
        f"Annunci pertinenti: {result.get('relevant', 0)}\n"
        f"Nuovi pertinenti: {result['new']}\n"
        f"🟢 Compra: {result.get('buy', 0)}\n"
        f"🟡 Tratta e verifica: {result.get('negotiate', 0)}\n"
        f"👀 Monitora: {result.get('monitor', 0)}\n"
        f"🔴 Scarta: {result.get('discard', 0)}\n"
        f"⚪ Dati insufficienti: {result.get('insufficient', 0)}\n\n"
        f"Avvisi inviabili: {result['valid']}"
    )


async def fonti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not SOURCE_URLS:
        await update.message.reply_text(
            "❌ Nessuna SOURCE_URL configurata su Railway."
        )
        return

    await update.message.reply_text("🧪 Controllo fonti in corso…")

    lines: List[str] = []
    for source_url in SOURCE_URLS:
        _, diagnostics = await extract_items(source_url)
        if diagnostics["error"]:
            lines.append(
                f"❌ {source_url}\nErrore: {diagnostics['error'][:180]}"
            )
        else:
            lines.append(
                f"✅ {source_label(source_url)}\n"
                f"{source_url}\n"
                f"HTTP {diagnostics['status']}\n"
                f"Link totali: {diagnostics['links']}\n"
                f"URL annunci riconosciuti: {diagnostics['listing_urls']}\n"
                f"Archiviabili: {diagnostics.get('archivable', 0)}\n"
                f"Archiviabili con prezzo: {diagnostics.get('archivable_priced', 0)}\n"
                f"Annunci pertinenti: {diagnostics['extracted']}\n"
                f"Pertinenti con prezzo: {diagnostics['priced']}"
            )

    message = "\n\n".join(lines)
    await update.message.reply_text(message[:4000])




async def categorie(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    lines = [
        "🧭 CATEGORIE ATTIVE",
        "",
    ]

    for index, source_url in enumerate(SOURCE_URLS, start=1):
        lines.append(
            f"{index}. {source_label(source_url)}\n{source_url}"
        )

    await update.message.reply_text(
        "\n\n".join(lines)[:4000],
        disable_web_page_preview=True,
    )



async def debug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    if not SOURCE_URLS:
        await update.message.reply_text(
            "❌ Nessuna SOURCE_URL configurata."
        )
        return

    await update.message.reply_text(
        "🧪 Lettura dei primi annunci in corso…"
    )

    messages: List[str] = []

    for source_url in SOURCE_URLS[:3]:
        try:
            samples = await extract_debug_samples(
                source_url,
                limit=5,
            )
        except Exception as exc:
            messages.append(
                f"❌ Fonte: {source_url}\nErrore: {str(exc)[:250]}"
            )
            continue

        if not samples:
            messages.append(
                f"⚠️ Fonte: {source_url}\n"
                "Nessun annuncio riconosciuto."
            )
            continue

        lines = [f"📡 Fonte: {source_url}"]

        for index, item in enumerate(samples, start=1):
            title = normalize_text(
                str(item.get("title") or "")
            )[:220]
            text = normalize_text(
                str(item.get("text") or "")
            )[:300]
            price = item.get("price")
            matched = ", ".join(
                item.get("debug_matches", [])
            ) or "nessuna"
            url = str(item.get("url") or "")

            lines.append(
                f"\n#{index}\n"
                f"Titolo: {title or '[vuoto]'}\n"
                f"Prezzo: {price if price is not None else '[mancante]'}\n"
                f"Keyword: {matched}\n"
                f"Testo: {text or '[vuoto]'}\n"
                f"URL: {url}"
            )

        messages.append("\n".join(lines))

    for message in messages:
        # Telegram limita i messaggi a 4096 caratteri.
        for start_index in range(0, len(message), 3900):
            await update.message.reply_text(
                message[start_index:start_index + 3900],
                disable_web_page_preview=True,
            )

async def collector(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    db_stats = DB.stats()
    stats = load_json(STATS_FILE, {"scans": 0})
    top_products = DB.product_counts(8)
    top_text = "\n".join(
        f"• {row['product_key']}: {row['count']}" for row in top_products
    ) or "• nessun dato"

    await update.message.reply_text(
        "🗄 STATO COLLECTOR POSTGRESQL\n\n"
        f"Annunci archiviati: {db_stats['total']}\n"
        f"Annunci attivi: {db_stats['active']}\n"
        f"Annunci scomparsi: {db_stats['missing']}\n"
        f"Osservazioni prezzo: {db_stats['price_observations']}\n"
        f"Annunci con variazioni prezzo: {DB.price_change_listings_count()}\n"
        f"Valutazioni registrate: {db_stats['evaluations']}\n"
        f"Scansioni registrate: {int(stats.get('scans', 0))}\n\n"
        f"Prodotti più raccolti:\n{top_text}"
    )


def _decision_debug_block(row: Dict[str, Any], index: int) -> str:
    title = normalize_text(str(row.get("title") or ""))[:180]
    url = str(row.get("url") or "")
    product_key = str(row.get("product_key") or "non identificato")
    decision = str(row.get("decision") or "DATI_INSUFFICIENTI")
    label = str(row.get("label") or "")
    reasons = row.get("reasons") or []
    warnings = row.get("warnings") or []

    reasons_text = (
        "\n".join(f"✅ {reason}" for reason in reasons)
        if reasons else "• nessun motivo positivo sufficiente"
    )
    warnings_text = (
        "\n".join(f"⚠️ {warning}" for warning in warnings)
        if warnings else "• nessun avviso specifico"
    )

    return (
        f"#{index} {decision}\n"
        f"{title}\n"
        f"Prodotto: {product_key}\n"
        f"Prezzo: {euro(row.get('price'))}\n"
        f"Mercato: {euro(row.get('market_low'))} – {euro(row.get('market_high'))}\n"
        f"Valore: {euro(row.get('market_value'))}\n"
        f"Rivendita prudente: {euro(row.get('quick_sale_value'))}\n"
        f"Margine: {euro(row.get('estimated_margin'))}\n"
        f"ROI: {row.get('roi') if row.get('roi') is not None else 'n/d'}%\n"
        f"Comparabili validi: {row.get('comparables', 0)}\n"
        f"Con prezzo dopo filtro condizione: "
        f"{row.get('priced_comparables', 0)}\n"
        f"Candidati DB prima dei filtri: "
        f"{row.get('raw_comparables', 0)}\n"
        f"Attendibilità: {row.get('confidence_label', 'INSUFFICIENTE')} "
        f"({row.get('confidence_score', 0)}/100)\n"
        f"Radar score: {row.get('radar_score', 0)}/100\n"
        f"Offerta massima: {euro(row.get('max_offer'))}\n"
        f"Verdetto: {label}\n\n"
        f"PERCHÉ\n{reasons_text}\n\n"
        f"PROBLEMI\n{warnings_text}\n"
        f"Link: {url}"
    )


async def _send_debug_rows(
    update: Update,
    rows: List[Dict[str, Any]],
    heading: str,
    limit: int = 10,
) -> None:
    if update.message is None:
        return

    if not rows:
        await update.message.reply_text(
            "⚠️ Nessuna valutazione disponibile.\n"
            "Esegui prima /scan."
        )
        return

    await update.message.reply_text(
        f"{heading}\n"
        f"Mostro {min(len(rows), limit)} valutazioni."
    )

    for index, row in enumerate(rows[:limit], start=1):
        block = _decision_debug_block(row, index)
        for start_index in range(0, len(block), 3900):
            await update.message.reply_text(
                block[start_index:start_index + 3900],
                disable_web_page_preview=True,
            )


async def decisiondebug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _send_debug_rows(
        update,
        LAST_DECISION_DEBUG,
        "🧪 DECISION DEBUG",
        limit=10,
    )


async def topscarti(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    rows = [
        row for row in LAST_DECISION_DEBUG
        if row.get("decision") in {
            "SCARTA", "MONITORA", "DATI_INSUFFICIENTI"
        }
    ]
    rows.sort(
        key=lambda row: (
            float(row.get("radar_score") or 0),
            float(row.get("estimated_margin") or 0),
        ),
        reverse=True,
    )
    await _send_debug_rows(
        update,
        rows,
        "🔴 TOP SCARTI / QUASI AFFARI",
        limit=10,
    )


async def topcompra(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    rows = [
        row for row in LAST_DECISION_DEBUG
        if row.get("decision") in {"COMPRA", "TRATTA"}
    ]
    rows.sort(
        key=lambda row: (
            float(row.get("radar_score") or 0),
            float(row.get("estimated_margin") or 0),
        ),
        reverse=True,
    )
    await _send_debug_rows(
        update,
        rows,
        "🟢 TOP COMPRA / TRATTA",
        limit=10,
    )



def _vision_result_message(result: Dict[str, Any]) -> str:
    defects = result.get("visible_defects") or []
    accessories = result.get("visible_accessories") or []
    fraud = result.get("counterfeit_or_fraud_signals") or []

    defects_text = "\n".join(f"• {value}" for value in defects) or "• nessuno evidente"
    accessories_text = "\n".join(f"• {value}" for value in accessories) or "• nessuno riconoscibile"
    fraud_text = "\n".join(f"• {value}" for value in fraud) or "• nessun segnale evidente"

    return (
        "👁 RADAR VISION AI V1\n\n"
        f"Annuncio: {result.get('listing_title') or '[titolo assente]'}\n"
        f"Immagini analizzate: {result.get('images_analyzed', 0)}\n\n"
        f"Categoria: {result.get('category') or 'non determinata'}\n"
        f"Marca: {result.get('brand') or 'non determinata'}\n"
        f"Modello: {result.get('model') or 'non determinato'}\n"
        f"Variante: {result.get('variant') or 'non determinata'}\n"
        f"Memoria/taglia: {result.get('storage_or_size') or 'non determinata'}\n"
        f"Anno stimato: {result.get('estimated_year') or 'non determinato'}\n"
        f"Condizione visibile: {result.get('visible_condition') or 'unknown'}\n"
        f"Coerenza testo/foto: {result.get('text_image_consistency') or 'unknown'}\n"
        f"Confidenza riconoscimento: {result.get('recognition_confidence', 0)}/100\n"
        f"Confidenza condizione: {result.get('condition_confidence', 0)}/100\n\n"
        f"DIFETTI VISIBILI\n{defects_text}\n\n"
        f"ACCESSORI VISIBILI\n{accessories_text}\n\n"
        f"SEGNALI DI RISCHIO\n{fraud_text}\n\n"
        f"NOTE\n{result.get('notes') or 'nessuna'}\n\n"
        f"Link: {result.get('listing_url') or ''}\n\n"
        "L'analisi visiva non certifica funzionamento, autenticità o stato interno."
    )


def _knowledge_result_message(report: Dict[str, Any]) -> str:
    if not report.get("supported"):
        return (
            "🧠 KNOWLEDGE ENGINE\n\n"
            f"{report.get('message', 'Categoria non ancora supportata.')}"
        )

    repairs = report.get("repair_items") or []
    repairs_text = "\n".join(
        f"• {row.get('issue')}: {euro(row.get('min_cost'))} – {euro(row.get('max_cost'))} "
        f"(gravità {row.get('severity', 'non definita')})"
        for row in repairs
    ) or "• Nessuna riparazione identificata dalle fotografie."

    checklist_text = "\n".join(f"☐ {value}" for value in report.get("checklist") or [])
    questions_text = "\n".join(f"• {value}" for value in report.get("questions_for_seller") or [])
    no_buy_text = "\n".join(f"⛔ {value}" for value in report.get("do_not_buy_if") or [])

    roi = report.get("roi")
    roi_text = f"{roi}%" if roi is not None else "non disponibile"

    return (
        "🧠 RADAR KNOWLEDGE ENGINE V1\n\n"
        f"🚦 Verdetto: {report.get('verdict', 'VERIFICA')}\n"
        f"⭐ BUY SCORE: {report.get('buy_score', 0)}/100\n"
        f"⚠️ Rischio tecnico: {report.get('risk_score', 0)}/100\n\n"
        f"🔧 COSTI DI RIPARAZIONE\n{repairs_text}\n\n"
        f"Totale minimo: {euro(report.get('repair_cost_min'))}\n"
        f"Totale massimo prudente: {euro(report.get('repair_cost_max'))}\n"
        f"Riduzione rivendibilità: {report.get('resale_penalty_percent', 0)}%\n\n"
        f"💰 VALUTAZIONE ECONOMICA\n"
        f"Rivendita prudente: {euro(report.get('prudent_resale_value'))}\n"
        f"Prezzo massimo di acquisto: {euro(report.get('maximum_buy_price'))}\n"
        f"Margine stimato: {euro(report.get('estimated_margin'))}\n"
        f"ROI: {roi_text}\n\n"
        f"📋 CHECKLIST PRE-ACQUISTO\n{checklist_text}\n\n"
        f"❓ DOMANDE AL VENDITORE\n{questions_text}\n\n"
        f"🚫 NON COMPRARE SE\n{no_buy_text}"
    )


async def visiontest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n/visiontest URL_ANNUNCIO\n\n"
            "Esempio:\n/visiontest https://www.subito.it/..."
        )
        return

    url = context.args[0].strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Inserisci un URL completo.")
        return

    await update.message.reply_text(
        "👁 Analisi Vision + Knowledge Engine in corso… può richiedere fino a un minuto."
    )

    try:
        result = await analyze_listing(url)
        vision_message = _vision_result_message(result)

        asking_price = parse_price(
            f"{result.get('listing_title', '')} "
            f"{result.get('listing_description', '')}"
        )

        report = build_knowledge_report(
            vision_result=result,
            asking_price=asking_price,
            market_value=None,
            target_margin=MIN_MARGIN_EURO,
            transaction_costs=25.0,
        )
        knowledge_message = _knowledge_result_message(report)
        messages = [vision_message, knowledge_message]
    except Exception as exc:
        log.exception("Vision + Knowledge test fallito")
        await update.message.reply_text(
            f"❌ Analisi non riuscita:\n{str(exc)[:900]}"
        )
        return

    for message in messages:
        for start_index in range(0, len(message), 3900):
            await update.message.reply_text(
                message[start_index:start_index + 3900],
                disable_web_page_preview=True,
            )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    removed = DB.clear_notifications()
    await update.message.reply_text(
        f"♻️ Notifiche azzerate: {removed}.\n"
        "Il database degli annunci e lo storico prezzi sono stati mantenuti."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔕 Avvisi disattivati.")


# ============================================================
# CICLO AUTOMATICO
# ============================================================

async def scan_loop(application: Application) -> None:
    await asyncio.sleep(10)

    while True:
        try:
            await scan_once(application)
        except Exception:
            log.exception("Errore durante la scansione automatica")

        await asyncio.sleep(CHECK_MINUTES * 60)


async def post_init(application: Application) -> None:
    application.create_task(scan_loop(application))


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Variabile TELEGRAM_BOT_TOKEN mancante. "
            "Configura il token nelle variabili ambiente di Railway."
        )

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(CommandHandler("scan", scan))
    application.add_handler(CommandHandler("fonti", fonti))
    application.add_handler(CommandHandler("categorie", categorie))
    application.add_handler(CommandHandler("collector", collector))
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("reclassify", reclassify))
    application.add_handler(CommandHandler("decisiondebug", decisiondebug))
    application.add_handler(CommandHandler("topscarti", topscarti))
    application.add_handler(CommandHandler("topcompra", topcompra))
    application.add_handler(CommandHandler("visiontest", visiontest))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("stop", stop))

    log.info(
        "Avvio Radar Affari Decision Engine v3.2 Recognition + PostgreSQL + Vision: %s fonti, %s parole chiave, dati=%s",
        len(SOURCE_URLS),
        len(KEYWORDS),
        DATA_DIR,
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
