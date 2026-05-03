FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
COPY web/requirements.txt ./web/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r web/requirements.txt

COPY config.py models.py main.py orchestrator.py ./
COPY agents/ ./agents/
COPY scripts/ ./scripts/
COPY web/ ./web/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "60", "web.app:app"]
