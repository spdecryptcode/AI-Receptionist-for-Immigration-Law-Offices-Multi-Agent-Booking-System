FROM python:3.12-slim

# audioop is removed in Python 3.13 — pinned to 3.12; audioop-lts in requirements.txt
# as belt-and-suspenders for future migration

WORKDIR /app

# System deps: ffmpeg (pydub), libpq (psycopg2), build tools, curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as non-root for container security hardening
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 3000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
