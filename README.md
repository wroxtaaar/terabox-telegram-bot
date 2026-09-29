# TeraBox Telegram Bot

Render-friendly Telegram bot using Telethon MTProto for outbound file delivery.

## Current architecture

1. Telegram sends an update to the Render webhook.
2. The bot extracts a TeraBox share URL.
3. app/terabox.py queries TeraBox's public share API without an ndus cookie.
4. If TeraBox supplies an anonymous direct URL, Telethon sends that URL to Telegram.
5. If TeraBox only returns metadata, the bot reports that the share needs a delivery-capable resolver rather than pretending the share URL is downloadable.

This intentionally avoids Playwright, FFmpeg, persistent media storage, and long-lived TeraBox login cookies.

## Required environment variables

- BOT_TOKEN
- API_ID
- API_HASH

Optional:

- WEBHOOK_SECRET — secret path component for the Telegram webhook.
- PUBLIC_BASE_URL — Render service URL. When set, the app registers the Telegram webhook automatically at startup.
- PORT — defaults to 10000.

## Render

Build:

    pip install -r requirements.txt

Start:

    python -m app

The service binds to 0.0.0.0:$PORT and exposes GET /health.

Set PUBLIC_BASE_URL to the exact Render service URL. Keep Telegram credentials and .env out of Git.

## Important TeraBox limitation

The public share API is useful for anonymous metadata, but current TeraBox behavior can return an empty or missing dlink to anonymous callers. In that situation there is no direct media URL for Telegram to fetch. The bot therefore fails explicitly instead of using a stale cookie or silently depending on a third-party resolver.

## Test fixture

The parser tests include:

https://1024terabox.com/s/1SUlU5TvNnS2rzSsEWMiHBA

The fixture validates URL recognition and the TeraBox /s/1... share-code convention; it does not claim that the live share is currently downloadable.

## Telegram transport

Telethon is used for outbound delivery because it uses Telegram's MTProto transport rather than the ordinary Bot API upload-size path. The bot keeps its Telethon session in memory with StringSession; no session file is committed.

## Status

- Repository scaffold: complete.
- Anonymous share metadata resolver: implemented.
- Telegram webhook registration: implemented.
- Direct anonymous delivery: supported only when TeraBox actually returns a usable direct URL.
- Fallback streaming/download path: not enabled yet.


## Docker

The repository is Docker-ready for local testing and Render deployment. It uses the official Python 3.12 slim image to keep the runtime small while avoiding Alpine compatibility tradeoffs.

### Local Docker test

Create a .env file from .env.example and fill in the Telegram credentials. Then run:

    docker compose build
    docker compose up

Health check: http://localhost:10000/health

For a one-off container:

    docker build -t terabox-telegram-bot .
    docker run --rm --env-file .env -p 10000:10000 terabox-telegram-bot

### Render

Set Render to use Docker for this repository. Render will build the included Dockerfile; no Python build/start commands are needed.

Required environment variables remain BOT_TOKEN, API_ID, and API_HASH. For the Telegram webhook, PUBLIC_BASE_URL must be the public Render HTTPS URL, for example https://your-service.onrender.com.
