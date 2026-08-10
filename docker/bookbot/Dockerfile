FROM python:3.13-slim
WORKDIR /app
COPY docker/bookbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts /app/scripts
CMD ["python", "/app/scripts/processing/bookbot.py"]
