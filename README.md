Python

mport asyncio

import hashlib

import html

import json

import logging

import os

import re

from pathlib import Path

from typing import Dict, List

from urllib.parse import urljoin, urlparse

import httpx

from bs4 import BeautifulSoup

from telegram import Update

from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s | %(levelname)s | %(message)s",

)

log = logging.getLogger("radar-affari")

# =========================

# CONFIGURAZIONE

# =========================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

CHECK_MINUTES = int(os.getenv("CHECK_MINUTES", "15"))

DEFAULT_KEYWORDS = (

    "dyson,iphone,ipad,macbook,playstation,ps5,xbox,nintendo switch,"

    "bici elettrica,ebike,fiido,haibike,cube,specialized,cannondale,"

    "festool,hilti,makita,bosch professional,milwaukee,dewalt,"

    "fotocamera,obiettivo,canon,nikon,sony,leica,"

    "robot aspirapolvere,roborock,folletto,vorwerk,"

    "macchina caffè,la marzocco,rational,berkel"

)

KEYWORDS = [

    item.strip().lower()

    for item in os.getenv("KEYWORDS", DEFAULT_KEYWORDS).split(",")

    if item.strip()

]

def split_sources(value: str) -> List[str]:

    """Accetta URL separati da virgola, punto e virgola o nuova riga."""

    return [

        item.strip()

        for item in re.split(r"[,;\n]+", value or "")

        if item.strip()

    ]

SOURCE_URLS = split_sources(os.getenv("SOURCE_URLS", ""))

STATE_FILE = Path("/tmp/radar_state.json")

SUBSCRIBERS_FILE = Path("/tmp/radar_subscribers.json")

HEADERS = {

    "User-Agent": (

        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "

        "AppleWebKit/605.1.15 (KHTML, like Gecko) "

        "Version/17.0 Mobile/15E148 Safari/604.1"

    ),

    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",

}

# =========================

# SALVATAGGIO DATI

# =========================

def load_json(path: Path, default):

    try:

        return json.loads(path.read_text(encoding="utf-8"))

    except Exception:

        return default

def save_json(path: Path, data) -> None:

    path.write_text(

        json.dumps(data, ensure_ascii=False),

        encoding="utf-8",

    )

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

# =========================

# ESTRAZIONE ANNUNCI

# =========================

def normalize_text(text: str) -> str:

    return re.sub(r"\s+", " ", text or "").strip()

def item_id(url: str) -> str:

    return hashlib.sha256(url.encode("utf-8")).hexdigest()

def keyword_matches(text: str) -> List[str]:

    normalized = normalize_text(text).lower()

    return [keyword for keyword in KEYWORDS if keyword in normalized]

def is_probable_subito_ad(url: str) -> bool:

    parsed = urlparse(url)

    if "subito.it" not in parsed.netloc:

        return False

    # Gli annunci di Subito normalmente terminano con .htm

    return bool(re.search(r"\.htm(?:$|\?)", url, re.IGNORECASE))

def extract_title(anchor) -> str:

    title = normalize_text(anchor.get_text(" ", strip=True))

    if len(title) >= 5:

        return title

    image = anchor.find("img")

    if image:

        image_title = normalize_text(image.get("alt", ""))

        if len(image_title) >= 5:

            return image_title

    for attribute in ("aria-label", "title"):

        value = normalize_text(anchor.get(attribute, ""))

        if len(value) >= 5:

            return value

    # Ultimo tentativo: cerca il testo nel contenitore dell'annuncio

    parent = anchor.parent

    for _ in range(4):

        if parent is None:

            break

        parent_text = normalize_text(parent.get_text(" ", strip=True))

        if 5 <= len(parent_text) <= 350:

            return parent_text

        parent = parent.parent

    return ""

async def extract_items(source_url: str) -> List[Dict]:

    timeout = httpx.Timeout(25.0)

    async with httpx.AsyncClient(

        headers=HEADERS,

        timeout=timeout,

        follow_redirects=True,

    ) as client:

        response = await client.get(source_url)

        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    items: Dict[str, Dict] = {}

    for anchor in soup.find_all("a", href=True):

        url = urljoin(source_url, anchor["href"])

        if not is_probable_subito_ad(url):

            continue

        title = extract_title(anchor)

        if not title:

            continue

        matches = keyword_matches(title)

        if not matches:

            continue

        unique_id = item_id(url)

        items[unique_id] = {

            "id": unique_id,

            "title": title[:250],

            "url": url,

            "matches": matches[:8],

            "source": urlparse(source_url).netloc,

        }

    log.info(

        "Fonte %s: trovati %s annunci pertinenti",

        source_url,

        len(items),

    )

    return list(items.values())

# =========================

# SCANSIONE

# =========================

async def scan_once(application: Application) -> int:

    if not SOURCE_URLS:

        log.warning("Nessuna fonte presente in SOURCE_URLS")

        return 0

    state = load_json(STATE_FILE, {"seen": []})

    seen = set(state.get("seen", []))

    new_items: List[Dict] = []

    for source_url in SOURCE_URLS:

        try:

            extracted = await extract_items(source_url)

            for item in extracted:

                if item["id"] not in seen:

                    seen.add(item["id"])

                    new_items.append(item)

        except Exception as exc:

            log.exception(

                "Errore durante la lettura della fonte %s: %s",

                source_url,

                exc,

            )

    save_json(STATE_FILE, {"seen": list(seen)[-5000:]})

    chat_ids = subscribers()

    for item in new_items[:30]:

        safe_title = html.escape(item["title"])

        safe_url = html.escape(item["url"], quote=True)

        safe_matches = html.escape(", ".join(item["matches"]))

        message = (

            "🚨 <b>NUOVO ANNUNCIO POTENZIALE</b>\n\n"

            f"<b>{safe_title}</b>\n\n"

            f"🔎 Parole trovate: {safe_matches}\n"

            f"🌐 Fonte: {html.escape(item['source'])}\n\n"

            f"👉 <a href=\"{safe_url}\">Apri l'annuncio</a>\n\n"

            "⚠️ Controlla prezzo, condizioni, autenticità e provenienza "

            "prima dell'acquisto."

        )

        for chat_id in chat_ids:

            try:

                await application.bot.send_message(

                    chat_id=chat_id,

                    text=message,

                    parse_mode="HTML",

                    disable_web_page_preview=False,

                )

            except Exception as exc:

                log.warning(

                    "Invio fallito alla chat %s: %s",

                    chat_id,

                    exc,

                )

    return len(new_items)

# =========================

# COMANDI TELEGRAM

# =========================

async def start(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    add_subscriber(update.effective_chat.id)

    await update.message.reply_text(

        "✅ Radar Affari attivato.\n\n"

        "/status — stato del radar\n"

        "/test — prova collegamento\n"

        "/scan — controllo immediato\n"

        "/reset — dimentica annunci già letti\n"

        "/stop — disattiva notifiche"

    )

async def status(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    await update.message.reply_text(

        f"📡 Fonti: {len(SOURCE_URLS)}\n"

        f"🔑 Parole chiave: {len(KEYWORDS)}\n"

        f"⏱ Controllo ogni {CHECK_MINUTES} minuti\n"

        f"👤 Iscritti: {len(subscribers())}"

    )

async def test(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    await update.message.reply_text(

        "🔥 TEST RADAR\n"

        "Collegamento Telegram ↔ Railway funzionante."

    )

async def scan(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    await update.message.reply_text("🔍 Controllo in corso...")

    count = await scan_once(context.application)

    await update.message.reply_text(

        f"✅ Finito. Nuovi elementi: {count}"

    )

async def reset(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    save_json(STATE_FILE, {"seen": []})

    await update.message.reply_text(

        "♻️ Memoria annunci azzerata.\n"

        "Adesso invia /scan."

    )

async def stop(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    remove_subscriber(update.effective_chat.id)

    await update.message.reply_text(

        "🔕 Notifiche automatiche disattivate.\n"

        "Invia /start per riattivarle."

    )

# =========================

# CONTROLLO AUTOMATICO

# =========================

async def automatic_loop(application: Application) -> None:

    await asyncio.sleep(15)

    while True:

        try:

            await scan_once(application)

        except Exception:

            log.exception("Errore nel controllo automatico")

        await asyncio.sleep(max(CHECK_MINUTES, 1) * 60)

async def post_init(application: Application) -> None:

    application.create_task(automatic_loop(application))

def main() -> None:

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

        "Avvio Radar Affari: %s fonti, %s parole chiave",

        len(SOURCE_URLS),

        len(KEYWORDS),

    )

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":

    main()
