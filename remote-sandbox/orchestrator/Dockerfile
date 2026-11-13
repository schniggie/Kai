FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends e2fsprogs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV TOKENS_FILE=/data/tokens.txt \
    SANDBOX_IMAGE=kai-sandbox:local \
    IDLE_TIMEOUT_SECONDS=3600 \
    NETWORK_NAME=kai_sandbox_net \
    SANDBOX_VOLUME_ROOT=/var/lib/kai-sandboxes

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
