# Container image for the bot, used by deploy/docker-compose.yml.
# Kept under deploy/ on purpose: Railway builds this repo from the Procfile,
# so a root-level Dockerfile would change its build behavior.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY twitter_telegram_bot.py database.py ./
COPY templates/ ./templates/
COPY manifest.json sw.js ./

EXPOSE 8080

CMD ["python", "twitter_telegram_bot.py"]
