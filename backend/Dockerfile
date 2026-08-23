FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build tooling for psycopg2 is needed only while installing, so it does not ship in the image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt \
    && apt-get purge -y build-essential && apt-get autoremove -y

COPY app ./app
COPY config ./config
COPY interop ./interop
COPY tools ./tools
COPY main.py pyproject.toml ./

# Merchant signing keys are generated on first run into this directory. Mount it as persistent
# storage in a deployment, otherwise records signed before a restart stop verifying.
RUN mkdir -p /app/secrets/merchant_keys /app/reports \
    && useradd --create-home --uid 10001 dwarpal \
    && chown -R dwarpal:dwarpal /app
USER dwarpal

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
