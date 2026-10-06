# X Auto-Repost Bot

Ein einfacher Python-Bot, der die X API v2 nach vordefinierten Begriffen durchsucht und passende neue Posts automatisch repostiert.

## Funktionen

- mehrere Suchbegriffe
- Ausschlusswörter
- Ausschluss bestimmter Accounts
- optional nur Deutsch
- Mindestanzahl Likes/Reposts
- Altersfilter für Posts
- maximale Reposts pro Zyklus
- SQLite-Datenbank gegen doppelte Reposts
- Dry-Run-Modus
- automatische Wiederholung
- keine API-Schlüssel im Quellcode

## 1. Voraussetzungen

- Python 3.10+
- X Developer Account / App mit API-Zugriff
- User Access Token mit den erforderlichen Schreibrechten
- Bearer Token für die Suche

X kann API-Zugriff, Rate Limits, Schreibrechte und Kosten je nach aktuellem Plan begrenzen. Prüfe die aktuellen Bedingungen im X Developer Portal.

## 2. Installation

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Dann:

```bash
pip install -r requirements.txt
```

## 3. Zugangsdaten

`.env.example` nach `.env` kopieren und die drei Werte eintragen:

```text
X_BEARER_TOKEN=...
X_USER_ACCESS_TOKEN=...
X_USER_ID=...
```

Die `.env` niemals öffentlich auf GitHub hochladen.

## 4. Suchbegriffe einstellen

`config.json` bearbeiten:

```json
{
  "keywords": ["Fotografie", "KI", "OpenAI"],
  "exclude_words": ["Werbung"],
  "exclude_users": ["spamaccount"],
  "language": "de",
  "interval_minutes": 10,
  "max_posts_per_cycle": 3,
  "min_likes": 0,
  "min_retweets": 0,
  "post_max_age_minutes": 15,
  "dry_run": true
}
```

### Wichtig: zuerst Dry Run

Lass zunächst

```json
"dry_run": true
```

gesetzt. Dann wird nichts repostiert, aber im Log siehst du, was der Bot auswählen würde.

Wenn alles passt:

```json
"dry_run": false
```

## 5. Start

```bash
python bot.py
```

Der Bot läuft anschließend dauerhaft und prüft alle `interval_minutes` Minuten.

## 6. Wie die Suche funktioniert

Aus den Keywords wird ungefähr folgende X-Suche gebaut:

```text
(Fotografie OR KI OR OpenAI OR ChatGPT) -is:retweet -is:reply lang:de
```

Zusätzlich werden Ausschluss-Accounts und lokale Filter angewendet.

## 7. Sicherheit

- Tokens gehören ausschließlich in `.env`.
- `bot.sqlite3` enthält die bereits bearbeiteten Post-IDs.
- Bei API-Fehlern wird ein fehlgeschlagener Post nicht als erfolgreich verarbeitet gespeichert.
- Rate-Limit-Fehler führen nicht zu aggressivem Wiederholen.

## 8. Betrieb auf einem Server

Für einen dauerhaft laufenden Bot eignet sich ein kleiner Linux-VPS.

Beispiel mit systemd:

```ini
[Unit]
Description=X Auto Repost Bot
After=network.target

[Service]
WorkingDirectory=/opt/x-auto-repost-bot
ExecStart=/opt/x-auto-repost-bot/.venv/bin/python /opt/x-auto-repost-bot/bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Danach:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now x-auto-repost-bot
sudo systemctl status x-auto-repost-bot
```
