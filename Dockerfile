FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run unprivileged; the bot role writes its heartbeat to /tmp.
RUN useradd --system --uid 10001 --no-create-home app && chown -R app /app
USER app

# APP_ROLE selects bot | api | all (see bot/main.py)
CMD ["python", "-m", "bot.main"]
