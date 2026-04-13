FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt sqlite-vec

# Optional: admin portal dependencies
# Uncomment if you want the web UI
# RUN pip install --no-cache-dir fastapi uvicorn

COPY . .

# Data directory for SQLite
VOLUME /app/data

CMD ["python", "-m", "mochi.main"]
