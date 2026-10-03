# DiskWala Downloader

Standalone DiskWala downloader for the Oracle VPS.

## Flow

Telegram -> DiskWala official web client -> signed API requests -> media URL -> download -> Telegram.

The resolver does not implement or forge DiskWala's Appicrypt signing. It runs DiskWala's own JavaScript in Chromium and proxies the browser's generated API requests through Playwright's browser-context request client when Chromium reports a network failure.

## Environment

Required:
- BOT_TOKEN
- API_ID
- API_HASH

Optional:
- TELEGRAM_ALLOWED_CHAT_IDS
- LOG_LEVEL
- PORT (default 18080)
- MAX_DOWNLOAD_BYTES (default 10 GiB)
- RESOLVE_TIMEOUT_SECONDS (default 35)
- DOWNLOAD_TIMEOUT_SECONDS (default 1800)

## Run

docker compose up -d --build
docker compose logs -f --tail=150

Health:
curl http://127.0.0.1:3000/health
