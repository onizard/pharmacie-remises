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

# Dépendances système de Firefox/camoufox (libgtk-3, NSS, dbus, X11, audio…)
# Liste explicite plus fiable que 'playwright install-deps firefox' sans binaire Firefox
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 \
    libdbus-1-3 libdbus-glib-1-2 \
    libnss3 libnspr4 \
    libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 libxcb-dri3-0 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libxrender1 \
    libxi6 libxtst6 libxt6 libxss1 libxcursor1 \
    libgbm1 libdrm2 \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libcairo-gobject2 \
    libatk1.0-0 libatk-bridge2.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcups2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# camoufox télécharge son propre binaire Firefox patchisé
RUN python -m camoufox fetch

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
