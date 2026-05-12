FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# playwright installe Chromium + toutes ses dépendances système
RUN playwright install chromium --with-deps

# camoufox télécharge Firefox (utilise les libs déjà présentes)
RUN python -m camoufox fetch

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
