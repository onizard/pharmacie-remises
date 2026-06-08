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
RUN playwright install-deps firefox

COPY . .

# camoufox télécharge son binaire Firefox au démarrage (évite timeout build Render)
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
