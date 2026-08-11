FROM python:3.13-slim

WORKDIR /app

COPY docker/huey/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/huey /app/scripts/huey
COPY docs/huey-channels.yml /app/config/channels.yml

CMD ["python", "/app/scripts/huey/huey.py"]
