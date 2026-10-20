# Immagine del server di assistenza. Viene pubblicata già pronta su ghcr.io dal workflow
# .github/workflows/server-image.yml per amd64, arm64 (Raspberry Pi 4/5, mini PC ARM) e armv7.
FROM python:3.13-slim

ARG VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    SUITE_VERSION=${VERSION} \
    SECURE_COOKIES=1

LABEL org.opencontainers.image.title="Server di assistenza Home Assistant" \
      org.opencontainers.image.description="Dashboard per installatori: clienti, impianti, diagnostica e ticket" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="NOASSERTION"

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 1000 --home-dir /srv suite \
 && mkdir -p /data && chown suite:suite /data

COPY app ./app
USER suite

# I dati (database, chiave di sessione, backup) stanno tutti qui: è l'unica cosa da salvare.
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

# Un solo processo: il long-polling dei comandi tiene lo stato in memoria.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
