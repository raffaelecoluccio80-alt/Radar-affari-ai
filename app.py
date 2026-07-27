import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional
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

KEYWORDS = [
    value.strip().lower()
    for value in os.getenv(
        "KEYWORDS",
        (
            "hilti,festool,makita,bosch professional,leica,topcon,trimble,"
            "bici elettrica,ebike,haibike,cube,specialized,trek,faema,"
            "la marzocco,rational,berkel,abbattitore,impastatrice,"
            "dyson,folletto,iphone,ipad,macbook,playstation,xbox,nintendo switch"
        ),
    ).split(",")
    if value.strip()
]

SOURCE_URLS = [
    value.strip()
    for value in os.getenv("SOURCE_URLS", "").split(",")
    if value.strip()
]

DATA_DIR = Path(tempfile.gettempdir()) / "radar_affari_ai"
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "radar_state.json"
SUBSCRIBERS_FILE = DATA_DIR / "radar_subscribers.json"

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
        return float(value) if float(value) > 0 else None

    text = normalize_text(str(value))
    if not text:
        return None

    text = text.replace("\u00a0", " ")
    matches = re.findall(r"(?<!\d)(\d{1,3}(?:[.\s]\d{3})*|\d+)(?:,\d{1,2})?\s*€?", text)

    values: List[float] = []
    for match in matches:
        cleaned = match.replace(".", "").replace(" ", "")
        try:
            number = float(cleaned)
        except ValueError:
            continue

        if 5 <= number <= 500000:
            values.append(number)

    return values[0] if values else None


def title_from_url(url: str) -> str:
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d{5,}\.htm$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"\.htm$", "", slug, flags=re.IGNORECASE)
    slug = slug.replace("-", " ").replace("_", " ")
    return normalize_text(slug)


def matching_keywords(text: str) -> List[str]:
    lowered = normalize_text(text).lower()
    return [keyword for keyword in KEYWORDS if keyword in lowered]


def is_probable_listing_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if not host or not path or path == "/":
        return False

    if "subito.it" in host:
        if re.search(r"-\d{5,}\.htm$", path):
            return True
        if path.endswith(".htm") and path.count("/") >= 2:
            return True
        return False

    if "ebay." in host:
        return "/itm/" in path

    if "vinted." in host:
        return "/items/" in path

    if "wallapop." in host:
        return "/item/" in path

    return False


def risk_analysis(text: str) -> Dict[str, Any]:
    lowered = normalize_text(text).lower()

    high_risk_terms = [
        "non funzionante",
        "non funziona",
        "da riparare",
        "da sistemare",
        "per ricambi",
        "rotto",
        "guasto",
        "non testato",
        "non so se funziona",
        "senza garanzia",
        "bloccato",
        "account bloccato",
        "imei bloccato",
    ]

    medium_risk_terms = [
        "senza caricatore",
        "senza batteria",
        "manca",
        "difetto",
        "segni di usura",
        "solo spedizione",
        "visto e piaciuto",
    ]

    found_high = [term for term in high_risk_terms if term in lowered]
    found_medium = [term for term in medium_risk_terms if term in lowered]

    score = min(100, len(found_high) * 35 + len(found_medium) * 15)

    if found_high:
        level = "ALTO"
    elif found_medium:
        level = "MEDIO"
    else:
        level = "BASSO"

    reasons = found_high + found_medium
    return {
        "score": score,
        "level": level,
        "reasons": reasons[:4],
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
        possible_values.extend(
            [
                offers.get("price"),
                offers.get("lowPrice"),
                offers.get("highPrice"),
            ]
        )

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
    absolute_url = urljoin(source_url, candidate_url)
    absolute_url = absolute_url.split("#")[0]

    if not is_probable_listing_url(absolute_url):
        return

    title = normalize_text(candidate_title)
    full_text = normalize_text(candidate_text)

    if len(title) < 4:
        title = title_from_url(absolute_url)

    combined_text = normalize_text(f"{title} {full_text}")
    matched = matching_keywords(combined_text)

    # Per non perdere tutti gli annunci, accettiamo anche il titolo ricavato
    # dall'URL. Il filtro per parole chiave viene applicato successivamente.
    if len(title) < 4:
        return

    item_id = create_item_id(absolute_url)
    price = candidate_price or parse_price(full_text)

    current = items.get(item_id)
    new_item = {
        "id": item_id,
        "title": title[:180],
        "url": absolute_url,
        "text": full_text[:700],
        "price": price,
        "matched": matched,
    }

    if current is None:
        items[item_id] = new_item
        return

    # Mantiene la versione più completa dello stesso annuncio.
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


async def extract_items(url: str) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

        final_url = str(response.url)

        log.info(
            "FONTE final_url=%s status=%s chars=%s",
            final_url,
            response.status_code,
            len(response.text),
        )

        soup = BeautifulSoup(response.text, "html.parser")
        all_links = soup.find_all("a", href=True)
        log.info("FONTE links_found=%s", len(all_links))

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

        # Cerca anche oggetti JSON inclusi in altri script.
        for script in soup.find_all("script"):
            raw = script.string or ""
            if not raw or len(raw) > 2_000_000:
                continue
            if '"url"' not in raw and '"itemUrl"' not in raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            extract_from_json_data(data, final_url, items)

        extract_from_html(soup, final_url, items)

        all_items = list(items.values())
        log.info("FONTE extracted_listings=%s", len(all_items))

        # Se esistono parole chiave, invia solo gli annunci coerenti.
        filtered = [
            item
            for item in all_items
            if item.get("matched")
        ]

        log.info("FONTE keyword_matched_items=%s", len(filtered))

        # Diagnostica utile nei log.
        for sample in all_items[:5]:
            log.info(
                "CAMPIONE title=%s price=%s url=%s",
                sample.get("title"),
                sample.get("price"),
                sample.get("url"),
            )

        return filtered


# ============================================================
# ANALISI ECONOMICA PRELIMINARE
# ============================================================

def estimate_market_values(items: List[Dict[str, Any]]) -> None:
    priced_items = [
        item for item in items
        if isinstance(item.get("price"), (int, float))
    ]

    keyword_prices: Dict[str, List[float]] = {}

    for item in priced_items:
        for keyword in item.get("matched", []):
            keyword_prices.setdefault(keyword, []).append(float(item["price"]))

    for item in items:
        price = item.get("price")
        matched = item.get("matched", [])

        comparable_prices: List[float] = []
        for keyword in matched:
            comparable_prices.extend(keyword_prices.get(keyword, []))

        if price is None or len(comparable_prices) < 3:
            item["market_value"] = None
            item["estimated_margin"] = None
            item["roi"] = None
            continue

        market_value = float(median(comparable_prices))
        quick_sale_value = market_value * 0.90
        estimated_costs = max(20.0, float(price) * 0.05)
        estimated_margin = quick_sale_value - float(price) - estimated_costs
        roi = (estimated_margin / float(price) * 100) if float(price) > 0 else 0

        item["market_value"] = round(market_value, 2)
        item["quick_sale_value"] = round(quick_sale_value, 2)
        item["estimated_margin"] = round(estimated_margin, 2)
        item["roi"] = round(roi, 1)


def verdict_for(item: Dict[str, Any], risk: Dict[str, Any]) -> str:
    margin = item.get("estimated_margin")
    roi = item.get("roi")

    if risk["level"] == "ALTO":
        return "SCARTA O VERIFICA MOLTO BENE"

    if margin is None or roi is None:
        return "DA APPROFONDIRE: DATI INSUFFICIENTI"

    if margin >= 100 and roi >= 20 and risk["level"] == "BASSO":
        return "BUON AFFARE: CONTATTARE SUBITO"

    if margin >= 80 and roi >= 15:
        return "INTERESSANTE: VERIFICARE E TRATTARE"

    return "MARGINE INSUFFICIENTE"


def euro(value: Optional[float]) -> str:
    if value is None:
        return "non disponibile"
    return f"{value:,.0f} €".replace(",", ".")


def build_message(item: Dict[str, Any]) -> str:
    risk = risk_analysis(
        f"{item.get('title', '')} {item.get('text', '')}"
    )
    verdict = verdict_for(item, risk)

    title = html.escape(str(item.get("title", "")))
    url = html.escape(str(item.get("url", "")), quote=True)
    matched = ", ".join(item.get("matched", [])) or "nessuna"
    matched = html.escape(matched)

    reasons = ", ".join(risk["reasons"]) if risk["reasons"] else "nessun segnale evidente"
    reasons = html.escape(reasons)

    return (
        "🔎 <b>NUOVO ANNUNCIO</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💰 Prezzo richiesto: <b>{euro(item.get('price'))}</b>\n"
        f"📊 Valore medio stimato: <b>{euro(item.get('market_value'))}</b>\n"
        f"⚡ Rivendita rapida stimata: <b>{euro(item.get('quick_sale_value'))}</b>\n"
        f"💵 Margine preliminare: <b>{euro(item.get('estimated_margin'))}</b>\n"
        f"📈 ROI preliminare: <b>{item.get('roi', 'non disponibile')}%</b>\n\n"
        f"⚠️ Rischio: <b>{risk['level']}</b> ({risk['score']}/100)\n"
        f"Motivi: {reasons}\n"
        f"🔑 Parole trovate: {matched}\n\n"
        f"🚦 <b>VERDETTO: {html.escape(verdict)}</b>\n\n"
        f'<a href="{url}">Apri annuncio</a>\n\n'
        "Prima di comprare: prova completa, verifica identità del venditore, "
        "numero seriale e provenienza."
    )


# ============================================================
# SCANSIONE E TELEGRAM
# ============================================================

async def scan_once(application: Application) -> int:
    if not SOURCE_URLS:
        log.warning("Nessuna SOURCE_URL configurata.")
        return 0

    state = load_json(STATE_FILE, {"seen": []})
    seen = set(state.get("seen", []))
    new_items: List[Dict[str, Any]] = []

    for source_url in SOURCE_URLS:
        try:
            extracted_items = await extract_items(source_url)
            estimate_market_values(extracted_items)

            for item in extracted_items:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    new_items.append(item)

        except Exception as exc:
            log.exception("Errore fonte %s: %s", source_url, exc)

    save_json(
        STATE_FILE,
        {"seen": list(seen)[-5000:]},
    )

    log.info("SCAN new_items=%s subscribers=%s", len(new_items), len(subscribers()))

    for item in new_items[:30]:
        message = build_message(item)

        for chat_id in subscribers():
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                log.warning("Invio fallito verso %s: %s", chat_id, exc)

    return len(new_items)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_chat is None or update.message is None:
        return

    add_subscriber(update.effective_chat.id)

    await update.message.reply_text(
        "✅ Radar Affari attivato.\n\n"
        "/status - mostra lo stato\n"
        "/test - prova il collegamento\n"
        "/scan - esegue una scansione\n"
        "/reset - dimentica gli annunci già visti\n"
        "/stop - disattiva gli avvisi"
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        f"📡 Fonti configurate: {len(SOURCE_URLS)}\n"
        f"🔎 Parole chiave: {len(KEYWORDS)}\n"
        f"⏱ Controllo ogni {CHECK_MINUTES} minuti\n"
        f"👥 Iscritti: {len(subscribers())}"
    )


async def test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "✅ TEST RADAR\n"
        "Collegamento Telegram ↔ applicazione funzionante."
    )


async def scan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🔎 Controllo in corso…")
    count = await scan_once(context.application)

    await update.message.reply_text(
        "✅ Controllo terminato.\n"
        f"Nuovi elementi trovati: {count}"
    )


async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    save_json(STATE_FILE, {"seen": []})

    await update.message.reply_text(
        "♻️ Memoria degli annunci azzerata.\n"
        "Ora invia /scan per ripetere il test."
    )


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_chat is None or update.message is None:
        return

    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔕 Avvisi disattivati.")


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
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("stop", stop))

    log.info(
        "Avvio Radar Affari AI: %s fonti, %s parole chiave.",
        len(SOURCE_URLS),
        len(KEYWORDS),
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()





