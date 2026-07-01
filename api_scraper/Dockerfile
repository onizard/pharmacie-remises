FROM python:3.12-slim

# Dépendances système de base + Tesseract (OCR des PDF scannés, langue française)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates \
    tesseract-ocr tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# playwright installe Chromium + toutes ses dépendances système (pour OSPHARM)
RUN playwright install chromium --with-deps

# Dépendances système de Firefox/camoufox via playwright (liste maintenue automatiquement)
RUN playwright install-deps firefox

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
