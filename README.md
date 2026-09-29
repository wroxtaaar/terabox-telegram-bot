# TeraBox Telegram Bot

The application is split so Render stays lightweight and observable while the Oracle VPS performs the heavy work.

## Architecture

1. Telegram sends updates to the Render controller webhook.
2. Render validates the TeraBox URL, creates a job ID, and queues the job on the Oracle VPS.
3. Oracle runs a long-lived Playwright Chromium instance to resolve the TeraBox share.
4. Oracle downloads the resolved file to local temporary disk and uploads it to Telegram using Telethon/MTProto.
5. Oracle posts job status callbacks back to Render so the Render logs show resolving, downloading, uploading, completion, and failures.
6. Render uses the Telegram Bot API only for webhook registration and lightweight status messages.

Heavy work intentionally does **not** run on Render:
- Chromium / Playwright
- TeraBox resolution
- file downloads
- Telethon / MTProto upload

## Render controller

Required:
- BOT_TOKEN
- PUBLIC_BASE_URL

Optional:
- WEBHOOK_SECRET
- VPS_WORKER_URL
- VPS_WORKER_SECRET
- VPS_CALLBACK_SECRET
- VPS_WORKER_REQUEST_TIMEOUT (default 20 seconds)
- PORT (default 10000)

Render build/start:
- Dockerfile in the repository
- Start command inside the Dockerfile: `python -m app`

Health:
- `GET /health`

Worker callback:
- `POST /worker-callback/{VPS_CALLBACK_SECRET}`

The Render service should stay on the lightweight `main` branch. Its purpose is ingress, validation, queue submission, and observable logs.

## Oracle VPS worker

The complete worker stack is under `vps/`.

It contains:
- `vps/worker.py` — queue, Playwright lifecycle, downloads, Telethon delivery, callbacks
- `vps/browser_resolver.py` — Chromium-based TeraBox resolver
- `vps/Dockerfile` — installs Playwright + Chromium on the VPS
- `vps/docker-compose.yml` — always-on worker container
- `vps/.env.example` — required VPS settings

On the VPS:

    git clone https://github.com/wroxtaaar/terabox-telegram-bot.git
    cd terabox-telegram-bot
    cp vps/.env.example .env
    # edit .env with BOT_TOKEN, API_ID, API_HASH and VPS_WORKER_SECRET
    docker compose -f vps/docker-compose.yml up -d --build

The worker listens on port 18080.

Health:
- `GET /health`

Job endpoint:
- `POST /job`
- Header: `X-Worker-Secret: <VPS_WORKER_SECRET>`

For public internet access, expose the VPS worker through your preferred HTTPS reverse proxy/tunnel. Keep `VPS_WORKER_SECRET` enabled even when using HTTPS.

## Telegram transport

The Oracle worker uses Telethon/MTProto for file delivery. Render no longer starts a Telethon client and does not download TeraBox media.

## Browser behavior

Chromium is kept alive for the lifetime of the VPS worker. Each resolution receives a fresh browser context, while the browser process itself is reused. The worker starts with one resolver job at a time to keep the 2-core Oracle instance conservative.

The resolver:
- opens the TeraBox share page in Chromium
- reads short-lived page tokens/session state
- inspects page data and TeraBox web API responses from the browser context
- obtains a direct download URL
- downloads that URL immediately so short-lived links do not expire before Telegram delivery

No long-lived TeraBox `ndus` cookie is required by the worker.

## Failure behavior

Every job produces structured Render callback logs:
- queued
- resolving
- resolved
- downloading
- uploading
- completed
- failed

If the VPS cannot resolve or deliver a file, Render reports the terminal error to the Telegram chat instead of silently returning a success response.

## Important

The VPS worker must be configured before downloads will work from the Render controller. Do not put `API_ID`, `API_HASH`, or `BOT_TOKEN` into Git.

