FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY docker/huey/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/huey /app/scripts/huey
COPY docs/huey-channels.yml /app/config/channels.yml

CMD ["python", "/app/scripts/huey/huey.py"]
