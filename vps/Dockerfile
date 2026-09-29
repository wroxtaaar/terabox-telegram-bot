FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /worker

COPY requirements.txt ./requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY browser_resolver.py ./vps/browser_resolver.py
COPY stream_downloader.py ./vps/stream_downloader.py
COPY worker.py ./vps/worker.py
COPY __init__.py ./vps/__init__.py

EXPOSE 18080

CMD ["python", "-m", "hypercorn", "vps.worker:app", "--bind", "0.0.0.0:18080", "--access-logfile", "-", "--error-logfile", "-"]
