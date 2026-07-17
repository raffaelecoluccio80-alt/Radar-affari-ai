import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("radar-affari")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHECK_MINUTES = int(os.getenv("CHECK_MINUTES", "15"))
KEYWORDS = [x.strip().lower() for x in os.getenv(
    "KEYWORDS",
    "hilti,festool,makita,bosch professional,leica,topcon,trimble,"
    "bici elettrica,ebike,haibike,cube,specialized,trek,faema,"
    "la marzocco,rational,berkel,abbattitore,impastatrice"
).split(",") if x.strip()]
SOURCE_URLS = [x.strip() for x in os.getenv("SOURCE_URLS", "").split(",") if x.strip()]

STATE_FILE = Path("/tmp/radar_state.json")
SUBSCRIBERS_FILE = Path("/tmp/radar_subscribers.json")
HEADERS = {"User-Agent": "Mozilla/5.0 Safari/605.1.15"}

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

def subscribers():
    return [int(x) for x in load_json(SUBSCRIBERS_FILE, [])]

def add_subscriber(chat_id: int):
    ids = set(subscribers())
    ids.add(chat_id)
    save_json(SUBSCRIBERS_FILE, sorted(ids))

def remove_subscriber(chat_id: int):
    ids = set(subscribers())
    ids.discard(chat_id)
    save_json(SUBSCRIBERS_FILE, sorted(ids))

def norm(text: str):
    return re.sub(r"\s+", " ", text or "").strip()

def uid(url: str, title: str):
    return hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:20]

async def extract_items(url: str) -> List[Dict[str, str]]:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        r = await client.get(url)
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        title = norm(a.get_text(" ", strip=True))
        if len(title) < 8:
            continue
        matched = [k for k in KEYWORDS if k in title.lower()]
        if not matched:
            continue
        href = str(a["href"]).strip()
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        item = {
            "id": uid(href, title),
            "title": title[:180],
            "url": href,
            "matched": ", ".join(matched[:4]),
        }
        out[item["id"]] = item
    return list(out.values())

async def scan_once(application: Application):
    if not SOURCE_URLS:
        return 0
    seen = set(load_json(STATE_FILE, {"seen": []}).get("seen", []))
    new_items = []
    for url in SOURCE_URLS:
        try:
            for item in await extract_items(url):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    new_items.append(item)
        except Exception as exc:
            log.warning("Errore fonte %s: %s", url, exc)
    save_json(STATE_FILE, {"seen": list(seen)[-5000:]})
    for item in new_items[:30]:
        text = (
            "🚨 <b>NUOVO ANNUNCIO POTENZIALE</b>\n\n"
            f"<b>{item['title']}</b>\n"
            f"🔎 Parole trovate: {item['matched']}\n\n"
            f"👉 <a href=\"{item['url']}\">Apri annuncio</a>\n\n"
            "⚠️ Verifica prezzo, provenienza e margine prima di comprare."
        )
        for chat_id in subscribers():
            try:
                await application.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception as exc:
                log.warning("Invio fallito: %s", exc)
    return len(new_items)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "✅ Radar Affari attivato.\n"
        "/status stato\n/test prova\n/scan controllo ora\n/stop disattiva"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📡 Fonti: {len(SOURCE_URLS)}\n"
        f"Parole chiave: {len(KEYWORDS)}\n"
        f"Controllo ogni {CHECK_MINUTES} minuti\n"
        f"Iscritti: {len(subscribers())}"
    )

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 TEST RADAR\nCollegamento Telegram ↔ Railway funzionante.")

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Controllo in corso…")
    count = await scan_once(context.application)
    await update.message.reply_text(f"✅ Finito. Nuovi elementi: {count}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔕 Avvisi disattivati.")

async def loop(application: Application):
    await asyncio.sleep(10)
    while True:
        try:
            await scan_once(application)
        except Exception:
            log.exception("Errore scansione")
        await asyncio.sleep(max(CHECK_MINUTES, 5) * 60)

async def post_init(application: Application):
    application.create_task(loop(application))

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("stop", stop))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
