# TeraBox Telegram Bot — Oracle VPS

The application now runs entirely on the Oracle VPS.

## Architecture

Telegram user
→ Telethon MTProto bot on Oracle
→ Playwright + Chromium on Oracle
→ TeraBox or Diskwala
→ temporary VPS disk
→ Telethon MTProto upload
→ Telegram

There is no Render runtime dependency.

## Oracle services

Everything is inside the vps/ stack:

- vps/worker.py — Telegram intake, queue, download, Telegram upload, health endpoint
- vps/browser_resolver.py — Playwright Chromium TeraBox resolver
- vps/Dockerfile — installs Playwright and Chromium
- vps/docker-compose.yml — always-on worker
- vps/.env.example — VPS configuration

Telegram updates are consumed directly by Telethon on the VPS. No Telegram webhook or Render controller is required.

Diskwala support uses the public share page in the existing Chromium instance. No Diskwala API key or paid third-party resolver is required.

The health endpoint is bound to the VPS loopback through Docker port mapping: 127.0.0.1:18080.

TeraBox metadata inspection can use two resolver slots. Diskwala uses one shared-browser slot to keep resource usage appropriate for the 2-core Oracle instance.

## Oracle deployment

On the VPS:

    cd ~/terabox-telegram-bot
    git pull origin main
    cp vps/.env.example vps/.env   # only on first setup
    nano vps/.env
    docker compose -f vps/docker-compose.yml up -d --build

Required values:

    BOT_TOKEN=...
    API_ID=...
    API_HASH=...

Optional:

    MAX_DOWNLOAD_BYTES=10737418240
    LOG_LEVEL=INFO

Do not commit vps/.env.

## Health and logs

Check health:

    curl http://127.0.0.1:18080/health

Check status:

    docker compose -f vps/docker-compose.yml ps

Check logs:

    docker compose -f vps/docker-compose.yml logs --tail=100

Follow live logs:

    docker compose -f vps/docker-compose.yml logs -f

## Automatic deployment

.github/workflows/deploy-oracle.yml deploys every push to main over SSH.

GitHub Actions:
- fetches and resets Oracle to origin/main
- rebuilds/restarts the worker
- checks /health
- prints the deployed commit
- prints recent worker logs
- fails the workflow if the worker does not become healthy

.github/workflows/oracle-diagnostics.yml can be run manually to inspect current VPS health and recent logs without deploying.

Required GitHub repository secrets:
- ORACLE_HOST
- ORACLE_USER
- ORACLE_SSH_KEY

## Telegram behavior

Send the bot /start, /help, a TeraBox share URL, or a public Diskwala share URL.

The bot queues the link, resolves it in Chromium, downloads the file to /tmp, uploads it to Telegram with MTProto, and removes the temporary file. Diskwala resolution reuses the same Chromium browser.

No long-lived TeraBox ndus cookie is required.

## Concurrency

The first deployment uses one resolver job at a time. Additional users wait in the in-memory queue. This avoids running multiple Chromium sessions simultaneously on the 2-core Oracle machine.
