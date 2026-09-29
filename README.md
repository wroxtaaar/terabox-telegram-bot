# TeraBox Telegram Bot

Render-friendly Telegram bot using Telethon MTProto for outbound file delivery.

## Design

- No `ndus` cookie dependency in the bot interface.
- Telegram credentials are environment variables only.
- Telethon uses an in-memory `StringSession`, avoiding persistent session files.
- TeraBox resolution is isolated in `app/terabox.py`.
- Direct TeraBox URL delivery is attempted through Telegram before adding a streamed fallback.
- No FFmpeg or persistent media storage in the initial design.

## Required environment variables

`BOT_TOKEN`, `API_ID`, and `API_HASH`.

Optional: `WEBHOOK_SECRET` and `PORT`.

## Render

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
python -m app
```

The service binds to `0.0.0.0:$PORT` and exposes `GET /health`.

Do not commit `.env` or Telegram credentials.

## Status

The Telegram transport is implemented. The anonymous TeraBox resolver and webhook registration are the next implementation steps.
