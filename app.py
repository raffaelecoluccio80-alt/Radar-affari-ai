import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


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

SOURCE_URLS = [
    value.strip()
    for value in os.getenv("SOURCE_URLS", "").split(",")
    if value.strip()
]

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
    values = load_json(SUBSCRIBERS_FILE, [])
    return [int(value) for value in values]


def add_subscriber(chat_id: int) -> None:
    ids = set(subscribers())
    ids.add(chat_id)
    save_json(SUBSCRIBERS_FILE, sorted(ids))


def remove_subscriber(chat_id: int) -> None:
    ids = set(subscribers())
    ids.discard(chat_id)
    save_json(SUBSCRIBERS_FILE, sorted(ids))


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


def identify_product(text: str) -> Dict[str, str]:
    lowered = normalize_text(text).lower()

    rules: List[Tuple[str, str, str]] = [
        ("engwe", r"\bep[-\s]?2\s*pro\b", "EP-2 Pro"),
        ("engwe", r"\bengine\s*pro\b", "Engine Pro"),
        ("engwe", r"\bl20\b", "L20"),
        ("ado", r"\ba20f\b", "A20F"),
        ("dyson", r"\bv15\b", "V15"),
        ("dyson", r"\bv12\b", "V12"),
        ("dyson", r"\bv11\b", "V11"),
        ("dyson", r"\bv10\b", "V10"),
        ("dyson", r"\bv8\b", "V8"),
        ("apple", r"\biphone\s*15\s*pro\s*max\b", "iPhone 15 Pro Max"),
        ("apple", r"\biphone\s*15\s*pro\b", "iPhone 15 Pro"),
        ("apple", r"\biphone\s*14\s*pro\s*max\b", "iPhone 14 Pro Max"),
        ("apple", r"\biphone\s*14\s*pro\b", "iPhone 14 Pro"),
        ("apple", r"\biphone\s*13\s*pro\s*max\b", "iPhone 13 Pro Max"),
        ("apple", r"\biphone\s*13\s*pro\b", "iPhone 13 Pro"),
        ("apple", r"\biphone\s*13\b", "iPhone 13"),
        ("apple", r"\biphone\s*12\s*pro\s*max\b", "iPhone 12 Pro Max"),
        ("apple", r"\biphone\s*12\s*pro\b", "iPhone 12 Pro"),
        ("apple", r"\biphone\s*12\b", "iPhone 12"),
        ("sony", r"\bps5\b|\bplaystation\s*5\b", "PlayStation 5"),
        ("nintendo", r"\bswitch\s*oled\b", "Switch OLED"),
    ]

    for brand, pattern, model in rules:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return {
                "brand": brand,
                "model": model,
                "product_key": f"{brand}:{model}".lower(),
            }

    for keyword in KEYWORDS:
        if keyword in lowered:
            return {
                "brand": keyword,
                "model": "",
                "product_key": keyword,
            }

    return {"brand": "", "model": "", "product_key": ""}


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
        "product_key": product["product_key"],
        "source_url": source_url,
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
            if item.get("matched")
        ]

        diagnostics["listing_urls"] = len(all_listings)
        diagnostics["extracted"] = len(relevant_items)
        diagnostics["priced"] = sum(
            1 for item in relevant_items
            if item.get("price") is not None
        )

        return relevant_items, diagnostics

    except Exception as exc:
        diagnostics["error"] = str(exc)
        log.exception("Errore estrazione %s: %s", url, exc)
        return [], diagnostics


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


def estimate_market_values(
    items: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
) -> None:
    historical_prices: Dict[str, List[float]] = {}

    for row in history:
        key = str(row.get("product_key") or "").strip().lower()
        price = row.get("price")
        if key and isinstance(price, (int, float)):
            historical_prices.setdefault(key, []).append(float(price))

    for item in items:
        price = item.get("price")
        key = str(item.get("product_key") or "").strip().lower()
        comparable_prices = historical_prices.get(key, [])

        if price is None or not key or len(comparable_prices) < 3:
            item["market_value"] = None
            item["quick_sale_value"] = None
            item["estimated_costs"] = None
            item["estimated_margin"] = None
            item["roi"] = None
            item["comparables"] = len(comparable_prices)
            continue

        market_value = float(median(comparable_prices))
        quick_sale_value = market_value * 0.90
        estimated_costs = max(20.0, float(price) * 0.05)
        estimated_margin = quick_sale_value - float(price) - estimated_costs
        roi = estimated_margin / float(price) * 100 if float(price) > 0 else 0

        item["market_value"] = round(market_value, 2)
        item["quick_sale_value"] = round(quick_sale_value, 2)
        item["estimated_costs"] = round(estimated_costs, 2)
        item["estimated_margin"] = round(estimated_margin, 2)
        item["roi"] = round(roi, 1)
        item["comparables"] = len(comparable_prices)


def is_valid_deal(item: Dict[str, Any]) -> bool:
    risk = risk_analysis(f"{item.get('title', '')} {item.get('text', '')}")
    margin = item.get("estimated_margin")
    roi = item.get("roi")

    if margin is None or roi is None:
        return False

    return (
        float(margin) >= MIN_MARGIN_EURO
        and float(roi) >= MIN_ROI_PERCENT
        and risk["level"] != "ALTO"
    )


def opportunity_score(item: Dict[str, Any]) -> float:
    margin = max(float(item.get("estimated_margin") or 0), 0)
    roi = max(float(item.get("roi") or 0), 0)
    risk = risk_analysis(f"{item.get('title', '')} {item.get('text', '')}")

    risk_penalty = {"BASSO": 0, "MEDIO": 15, "ALTO": 100}.get(
        risk["level"], 30
    )

    return round(margin + (roi * 2) - risk_penalty, 2)


def verdict_for(item: Dict[str, Any], risk: Dict[str, Any]) -> str:
    margin = item.get("estimated_margin")
    roi = item.get("roi")

    if risk["level"] == "ALTO":
        return "SCARTA: RISCHIO ALTO"
    if margin is None or roi is None:
        return "SCARTA: DATI ECONOMICI INSUFFICIENTI"
    if float(margin) < MIN_MARGIN_EURO:
        return f"SCARTA: MARGINE INFERIORE A {MIN_MARGIN_EURO:.0f} €"
    if float(roi) < MIN_ROI_PERCENT:
        return f"SCARTA: ROI INFERIORE AL {MIN_ROI_PERCENT:.0f}%"
    if risk["level"] == "MEDIO":
        return "TRATTA: MARGINE VALIDO, SERVONO VERIFICHE"
    return "CANDIDATO: CONTATTARE E VERIFICARE"


def euro(value: Optional[float]) -> str:
    if value is None:
        return "non disponibile"
    return f"{value:,.0f} €".replace(",", ".")


def build_message(item: Dict[str, Any]) -> str:
    risk = risk_analysis(f"{item.get('title', '')} {item.get('text', '')}")
    verdict = verdict_for(item, risk)

    title = html.escape(str(item.get("title", "")))
    url = html.escape(str(item.get("url", "")), quote=True)
    product_name = " ".join(
        part for part in [item.get("brand", ""), item.get("model", "")] if part
    ) or "non identificato"

    reasons = ", ".join(risk["reasons"]) if risk["reasons"] else "nessun segnale evidente"

    return (
        "🚨 <b>AFFARE CANDIDATO</b>\n\n"
        f"<b>{title}</b>\n"
        f"🧩 Prodotto: <b>{html.escape(product_name)}</b>\n\n"
        f"💰 Prezzo richiesto: <b>{euro(item.get('price'))}</b>\n"
        f"📊 Valore medio stimato: <b>{euro(item.get('market_value'))}</b>\n"
        f"⚡ Rivendita rapida stimata: <b>{euro(item.get('quick_sale_value'))}</b>\n"
        f"🧾 Costi prudenziali: <b>{euro(item.get('estimated_costs'))}</b>\n"
        f"💵 Margine stimato: <b>{euro(item.get('estimated_margin'))}</b>\n"
        f"📈 ROI stimato: <b>{item.get('roi', 'non disponibile')}%</b>\n"
        f"📚 Confronti disponibili: <b>{item.get('comparables', 0)}</b>\n"
        f"🎯 Indice opportunità: <b>{opportunity_score(item)}</b>\n\n"
        f"⚠️ Rischio: <b>{risk['level']}</b> ({risk['score']}/100)\n"
        f"Motivi: {html.escape(reasons)}\n\n"
        f"🚦 <b>VERDETTO: {html.escape(verdict)}</b>\n\n"
        f'<a href="{url}">APRI SUBITO L’ANNUNCIO</a>\n\n'
        "Non acquistare senza verifica manuale."
    )


# ============================================================
# SCANSIONE E TELEGRAM
# ============================================================

async def scan_once(application: Application) -> Dict[str, Any]:
    if SCAN_LOCK.locked():
        return {
            "busy": True,
            "new": 0,
            "valid": 0,
            "diagnostics": [],
        }

    async with SCAN_LOCK:
        if not SOURCE_URLS:
            log.warning("Nessuna SOURCE_URL configurata.")
            return {
                "busy": False,
                "new": 0,
                "valid": 0,
                "diagnostics": [],
            }

        state = load_json(STATE_FILE, {"observed": [], "notified": []})
        observed = set(state.get("observed", []))
        notified = set(state.get("notified", []))

        all_extracted: List[Dict[str, Any]] = []
        new_items: List[Dict[str, Any]] = []
        diagnostics: List[Dict[str, Any]] = []

        for source_url in SOURCE_URLS:
            extracted_items, source_diagnostics = await extract_items(source_url)
            diagnostics.append(source_diagnostics)
            all_extracted.extend(extracted_items)

            for item in extracted_items:
                if item["id"] not in observed:
                    observed.add(item["id"])
                    new_items.append(item)

        history = update_history(all_extracted)
        update_collection_stats(
            diagnostics=diagnostics,
            relevant_items=all_extracted,
            new_items_count=len(new_items),
        )
        estimate_market_values(new_items, history)

        valid_deals = [
            item for item in new_items
            if item["id"] not in notified and is_valid_deal(item)
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
                        chat_id=chat_id,
                        text=message,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    sent_to_at_least_one = True
                except Exception as exc:
                    log.warning("Invio fallito verso %s: %s", chat_id, exc)

            if sent_to_at_least_one:
                notified.add(item["id"])

        save_json(
            STATE_FILE,
            {
                "observed": list(observed)[-10000:],
                "notified": list(notified)[-5000:],
            },
        )

        log.info(
            "SCAN nuovi=%s affari_validi=%s iscritti=%s",
            len(new_items),
            len(valid_deals),
            len(subscribers()),
        )

        return {
            "busy": False,
            "new": len(new_items),
            "valid": len(valid_deals),
            "diagnostics": diagnostics,
        }


# ============================================================
# COMANDI TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    add_subscriber(update.effective_chat.id)

    await update.message.reply_text(
        "✅ Radar Affari attivato.\n\n"
        f"• Margine minimo: {MIN_MARGIN_EURO:.0f} €\n"
        f"• ROI minimo: {MIN_ROI_PERCENT:.0f}%\n\n"
        "/status - stato del radar\n"
        "/fonti - diagnostica delle fonti\n"
        "/collector - stato archivio mercato\n"
        "/test - prova Telegram\n"
        "/scan - scansione manuale\n"
        "/reset - azzera memoria annunci\n"
        "/stop - disattiva avvisi"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    history = load_json(HISTORY_FILE, [])
    state = load_json(STATE_FILE, {"observed": [], "notified": []})
    stats = load_json(STATS_FILE, {"scans": 0})

    identified_models = {
        str(row.get("product_key"))
        for row in history
        if isinstance(row, dict)
        and row.get("product_key")
        and ":" in str(row.get("product_key"))
    }

    await update.message.reply_text(
        f"📡 Fonti configurate: {len(SOURCE_URLS)}\n"
        f"🔎 Parole chiave: {len(KEYWORDS)}\n"
        f"⏱ Controllo ogni {CHECK_MINUTES} minuti\n"
        f"💵 Margine minimo: {MIN_MARGIN_EURO:.0f} €\n"
        f"📈 ROI minimo: {MIN_ROI_PERCENT:.0f}%\n"
        f"📚 Annunci nel Collector: {len(history)}\n"
        f"🧩 Modelli identificati: {len(identified_models)}\n"
        f"🔄 Scansioni registrate: {int(stats.get('scans', 0))}\n"
        f"👁 Annunci osservati: {len(state.get('observed', []))}\n"
        f"🔔 Annunci notificati: {len(state.get('notified', []))}\n"
        f"👥 Iscritti: {len(subscribers())}\n"
        f"💾 Cartella dati: {DATA_DIR}"
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
        "✅ Controllo terminato.\n"
        f"Nuovi annunci: {result['new']}\n"
        f"Affari validi: {result['valid']}"
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
                f"✅ {source_url}\n"
                f"HTTP {diagnostics['status']}\n"
                f"Link totali: {diagnostics['links']}\n"
                f"URL annunci riconosciuti: {diagnostics['listing_urls']}\n"
                f"Annunci pertinenti: {diagnostics['extracted']}\n"
                f"Pertinenti con prezzo: {diagnostics['priced']}"
            )

    message = "\n\n".join(lines)
    await update.message.reply_text(message[:4000])


async def collector(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    history = load_json(HISTORY_FILE, [])
    stats = load_json(STATS_FILE, {"scans": 0})

    by_product: Dict[str, int] = {}
    price_changes = 0

    for row in history:
        if not isinstance(row, dict):
            continue

        key = str(row.get("product_key") or "non identificato")
        by_product[key] = by_product.get(key, 0) + 1

        if len(row.get("price_history", [])) > 1:
            price_changes += 1

    top_products = sorted(
        by_product.items(),
        key=lambda pair: pair[1],
        reverse=True,
    )[:8]

    top_text = "\n".join(
        f"• {name}: {count}"
        for name, count in top_products
    ) or "• nessun dato"

    await update.message.reply_text(
        "🗄 STATO COLLECTOR\n\n"
        f"Annunci archiviati: {len(history)}\n"
        f"Scansioni registrate: {int(stats.get('scans', 0))}\n"
        f"Annunci con variazioni prezzo: {price_changes}\n\n"
        f"Prodotti più raccolti:\n{top_text}"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    save_json(STATE_FILE, {"observed": [], "notified": []})
    await update.message.reply_text(
        "♻️ Memoria annunci azzerata.\n"
        "Lo storico prezzi è stato mantenuto."
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
    application.add_handler(CommandHandler("collector", collector))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("stop", stop))

    log.info(
        "Avvio Radar Affari Collector: %s fonti, %s parole chiave, dati=%s",
        len(SOURCE_URLS),
        len(KEYWORDS),
        DATA_DIR,
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()



