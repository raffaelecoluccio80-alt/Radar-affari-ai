import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin

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

STATE_FILE = DATA_DIR / "radar_state.json"
SUBSCRIBERS_FILE = DATA_DIR / "radar_subscribers.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
}


def load_json(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Impossibile leggere %s: %s", path, exc)
        return default


def save_json(path: Path, data) -> None:
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


async def extract_items(url: str) -> List[Dict[str, str]]:
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=25,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    items: Dict[str, Dict[str, str]] = {}

    for link in soup.find_all("a", href=True):
        title = normalize_text(link.get_text(" ", strip=True))

        if len(title) < 8:
            continue

        matched_keywords = [
            keyword
            for keyword in KEYWORDS
            if keyword in title.lower()
        ]

        if not matched_keywords:
            continue

        href = str(link["href"]).strip()

        if not href:
            continue

        absolute_url = urljoin(url, href)
        item_id = create_item_id(absolute_url, title)

        items[item_id] = {
            "id": item_id,
            "title": title[:180],
            "url": absolute_url,
            "matched": ", ".join(matched_keywords[:4]),
        }

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
            log.warning("Errore fonte %s: %s", source_url, exc)

    save_json(STATE_FILE, {"seen": list(seen)[-5000:]})

    for item in new_items[:30]:
        message = (
            "🚨 <b>NUOVO ANNUNCIO POTENZIALE</b>\n\n"
            f"<b>{item['title']}</b>\n"
            f"🔎 Parole trovate: {item['matched']}\n\n"
            f"👉 <a href=\"{item['url']}\">Apri annuncio</a>\n\n"
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
        "🔥 TEST RADAR\n"
        "Collegamento Telegram ↔ applicazione funzionante."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🔍 Controllo in corso…")
    count = await scan_once(context.application)
    await update.message.reply_text(
        f"✅ Controllo terminato.\n"
        f"Nuovi elementi trovati: {count}"
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
    application.add_handler(CommandHandler("stop", stop))

    log.info(
        "Avvio Radar Affari AI: %s fonti, %s parole chiave.",
        len(SOURCE_URLS),
        len(KEYWORDS),
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
