FROM python:3.13-slim

WORKDIR /app
COPY louie /app/louie
RUN useradd --uid 1000 --create-home louie && mkdir -p /state /huey-state && chown -R louie:louie /app /state
USER louie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-m", "louie.server"]
