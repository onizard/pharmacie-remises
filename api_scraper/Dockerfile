FROM python:3.12-slim

# Dépendances système de base
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# playwright installe Chromium + toutes ses dépendances système (pour OSPHARM)
RUN playwright install chromium --with-deps

# Dépendances système de Firefox/camoufox via playwright (liste maintenue automatiquement)
# Évite la dérive manuelle (ex: libgtk-3-0 → libgtk-3-0t64 sur Bookworm récent)
RUN playwright install-deps firefox

# camoufox télécharge son propre binaire Firefox patchisé
RUN python -m camoufox fetch

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
