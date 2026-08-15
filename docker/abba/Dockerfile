FROM ghcr.io/jamesry96/audiobookbay-automated@sha256:be58c8a0c2ef4ec4c1a1cc6714791b5b72c8bf62a24774ee8c784257c87a2678

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5078 \
    ABBA_DB_PATH=/config/abba.db

USER root
WORKDIR /app
COPY --chown=10001:10001 docker/abba/app.py /app/app.py
COPY --chown=10001:10001 docker/abba/wsgi.py /app/wsgi.py
RUN pip install --no-cache-dir gunicorn==23.0.0
RUN mkdir -p /config
RUN chown 10001:10001 /config
RUN chmod 0700 /config

USER 10001:10001
EXPOSE 5078
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '5078') + '/health', timeout=4).read()"]

CMD ["gunicorn", "--bind", "0.0.0.0:5078", "--workers", "1", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "wsgi:application"]
