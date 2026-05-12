FROM python:3.12-slim

# Dépendances système pour camoufox (Firefox)
# playwright --with-deps gère les dépendances Chromium lui-même
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libcups2 \
    libnss3 \
    libnspr4 \
    fonts-liberation \
    && (apt-get install -y --no-install-recommends libasound2t64 || apt-get install -y --no-install-recommends libasound2) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Firefox pour camoufox
RUN python -m camoufox fetch

# Chromium pour playwright (installe aussi ses dépendances système)
RUN playwright install chromium --with-deps

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
