FROM python:3.12-slim

WORKDIR /app

# curl нужен fetchers/wildberries.py — card.wb.ru блокирует httpx по TLS-фингерпринту, но пропускает curl
# остальное — системные зависимости headless-chromium для playwright (fetchers/dvgis.py: identify(),
# разовый вызов при добавлении ссылки) и patchright (fetchers/vseinstrumenti.py — используется на
# каждом скрапе, антибот площадки не проходится без него, см. докстринг модуля)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libxkbcommon0 \
        libatspi2.0-0 libwayland-client0 libasound2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN patchright install chromium

COPY app/ app/
COPY fetchers/ fetchers/
COPY scripts/ scripts/
COPY templates/ templates/
COPY static/ static/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8767"]
