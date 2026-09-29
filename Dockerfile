# Base image: only the tools that rarely change. The bot's own code is pulled
# from GitHub by entrypoint.sh every time the container starts.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates tzdata \
        tesseract-ocr tesseract-ocr-swe tesseract-ocr-spa tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Stockholm \
    DATA_DIR=/app/data \
    CONFIG_PATH=/app/config.yaml \
    CODE_ROOT=/app/code

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
