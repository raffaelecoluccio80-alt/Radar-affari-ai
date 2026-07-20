import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("radar-affari")


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_MINUTES = max(int(os.getenv("CHECK_MINUTES", "15")), 5)

KEYWORDS = [
    keyword.strip().lower()
    for keyword in os.getenv(
        "KEYWORDS",
        (
            "hilti,festool,makita,bosch professional,leica,topcon,trimble,"
            "bici elettrica,ebike,haibike,cube,specialized,trek,faema,"
            "la marzocco,rational,berkel,abbattitore,impastatrice"
        ),
    ).split(",")
    if keyword.strip()
]

SOURCE_URLS = [
    url.strip()
    for url in os.getenv("SOURCE_URLS", "").split(",")
    if url.strip()
]

DATA_DIR = Path(tempfile.gettempdir()) / "radar_affari_ai"
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "radar_state_v2.json"
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


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def create_item_id(url: str, title: str) -> str:
    value = f"{url}|{title}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:20]


def matching_keywords(title: str) -> List[str]:
    lowered = title.lower()
    return [keyword for keyword in KEYWORDS if keyword in lowered]


def is_probable_listing_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if not host:
        return False

    if "subito.it" in host:
        if path in ("", "/"):
            return False
        if re.search(r"-\d{6,}\.htm$", path):
            return True
        return path.count("/") >= 2 and path.endswith(".htm")

    return True


def add_item(
    items: Dict[str, Dict[str, str]],
    source_url: str,
    candidate_url: str,
    candidate_title: str,
) -> None:
    title = normalize_text(candidate_title)
    absolute_url = urljoin(source_url, candidate_url)

    if len(title) < 8:
        return
    if not is_probable_listing_url(absolute_url):
        return

    matched = matching_keywords(title)
    if not matched:
        return

    item_id = create_item_id(absolute_url, title)
    items[item_id] = {
        "id": item_id,
        "title": title[:180],
        "url": absolute_url,
        "matched": ", ".join(matched[:4]),
    }


def walk_json(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_from_json_data(
    data: Any,
    source_url: str,
    items: Dict[str, Dict[str, str]],
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

        if isinstance(url_value, str) and isinstance(title_value, str):
            add_item(items, source_url, url_value, title_value)

        item = obj.get("item")
        if isinstance(item, dict):
            nested_url = item.get("url")
            nested_title = item.get("name") or item.get("title")
            if isinstance(nested_url, str) and isinstance(nested_title, str):
                add_item(items, source_url, nested_url, nested_title)


def extract_from_html(
    soup: BeautifulSoup,
    source_url: str,
    items: Dict[str, Dict[str, str]],
) -> None:
    for link in soup.find_all("a", href=True):
        href = str(link.get("href", "")).strip()
        if not href:
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

        container = link.find_parent(["article", "li"])
        if container is not None:
            container_text = container.get_text(" ", strip=True)
            if container_text:
                candidates.append(container_text)

        title = max(
            (normalize_text(candidate) for candidate in candidates if candidate),
            key=len,
            default="",
        )
        add_item(items, source_url, href, title)


async def extract_items(url: str) -> List[Dict[str, str]]:
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    log.info(
        "FONTE final_url=%s status=%s chars=%s",
        response.url,
        response.status_code,
        len(response.text),
    )

    soup = BeautifulSoup(response.text, "html.parser")
    all_links = soup.find_all("a", href=True)
    log.info("FONTE links_found=%s", len(all_links))

    items: Dict[str, Dict[str, str]] = {}

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if script.string:
                extract_from_json_data(
                    json.loads(script.string),
                    str(response.url),
                    items,
                )
        except (json.JSONDecodeError, TypeError):
            continue

    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data is not None and next_data.string:
        try:
            extract_from_json_data(
                json.loads(next_data.string),
                str(response.url),
                items,
            )
        except json.JSONDecodeError:
            log.warning("__NEXT_DATA__ presente ma non leggibile.")

    extract_from_html(soup, str(response.url), items)

    log.info("FONTE matched_items=%s", len(items))
    return list(items.values())


async def scan_once(application: Application) -> int:
    if not SOURCE_URLS:
        log.warning("Nessuna SOURCE_URL configurata.")
        return 0

    state = load_json(STATE_FILE, {"seen": []})
    seen = set(state.get("seen", []))
    new_items: List[Dict[str, str]] = []

    for source_url in SOURCE_URLS:
        try:
            extracted_items = await extract_items(source_url)
            for item in extracted_items:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    new_items.append(item)
        except Exception as exc:
            log.exception("Errore fonte %s: %s", source_url, exc)

    save_json(STATE_FILE, {"seen": list(seen)[-5000:]})

    for item in new_items[:30]:
        safe_title = html.escape(item["title"])
        safe_matched = html.escape(item["matched"])
        safe_url = html.escape(item["url"], quote=True)

        message = (
            "🔔 <b>NUOVO ANNUNCIO POTENZIALE</b>\n\n"
            f"<b>{safe_title}</b>\n"
            f"🔎 Parole trovate: {safe_matched}\n\n"
            f'<a href="{safe_url}">Apri annuncio</a>\n\n'
            "⚠️ Verifica prezzo, provenienza e margine prima di comprare."
        )

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        f"📡 Fonti configurate: {len(SOURCE_URLS)}\n"
        f"🔎 Parole chiave: {len(KEYWORDS)}\n"
        f"⏱ Controllo ogni {CHECK_MINUTES} minuti\n"
        f"👥 Iscritti: {len(subscribers())}"
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "🧪 TEST RADAR\n"
        "Collegamento Telegram ↔ applicazione funzionante."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🔎 Controllo in corso…")
    count = await scan_once(context.application)
    await update.message.reply_text(
        "✅ Controllo terminato.\n"
        f"Nuovi elementi trovati: {count}"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    save_json(STATE_FILE, {"seen": []})
    await update.message.reply_text(
        "♻️ Memoria degli annunci azzerata.\n"
        "Ora invia /scan per ripetere il test."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
