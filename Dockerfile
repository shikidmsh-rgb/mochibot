FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt sqlite-vec aiohttp

COPY . .

# Data directory for SQLite
VOLUME /app/data

CMD ["python", "-m", "mochi.main"]
