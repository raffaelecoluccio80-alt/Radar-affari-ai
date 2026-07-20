# Radar Affari AI

Bot Telegram per monitorare annunci online e segnalare possibili affari.

## Funzioni attuali

- scansione periodica degli annunci
- ricerca per parole chiave
- notifiche Telegram
- comandi `/start`, `/test`, `/status`, `/scan` e `/stop`

## Variabili ambiente richieste

- `TELEGRAM_BOT_TOKEN`
- `CHECK_MINUTES`
- `SOURCE_URLS`
- `KEYWORDS`

## Avvio locale

```bash
pip install -r requirements.txt
python app.py
