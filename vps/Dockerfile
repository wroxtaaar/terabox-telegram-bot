FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /worker

COPY vps/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY vps ./vps

EXPOSE 18080

CMD ["python", "-m", "hypercorn", "vps.worker:app", "--bind", "0.0.0.0:18080", "--access-logfile", "-", "--error-logfile", "-"]
