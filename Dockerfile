FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# UID/GID du user runtime — paramétrables au build pour matcher l'utilisateur
# de l'hôte qui possède le bind-mount data/. Par défaut 1000/1000 (= ubuntu
# sur la plupart des images Ubuntu cloud).
# Override : `docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)`
ARG UID=1000
ARG GID=1000

WORKDIR /app

# System deps for pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# User non-root + ownership de /app (cf. HANDOFF : data/ doit être writable
# par le user du conteneur, sinon les fichiers créés par git pull / le runtime
# se retrouvent en root sur l'hôte et bloquent les déploiements suivants).
RUN groupadd --gid ${GID} app \
    && useradd --uid ${UID} --gid ${GID} --home /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.core.main:app", "--host", "0.0.0.0", "--port", "8000"]
