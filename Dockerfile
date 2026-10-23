FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8088 \
    VTECH_DB_PATH=/app/publishing_app/data/publishing.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ ./tools/
COPY .agents/ ./.agents/
COPY publishing_app/ ./publishing_app/

RUN mkdir -p /app/publishing_app/data/media

EXPOSE 8088

CMD ["sh", "-c", "uvicorn publishing_app.server:app --host 0.0.0.0 --port ${PORT:-8088}"]
