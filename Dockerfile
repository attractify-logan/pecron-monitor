FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PECRON_STATE_PATH=/data/pecron-state.json

WORKDIR /app

COPY pyproject.toml README.md ./
COPY *.py ./

RUN pip install --upgrade pip \
    && pip install ".[ble]" \
    && mkdir -p /config /data

CMD ["pecron-monitor", "--config", "/config/config.yaml"]
