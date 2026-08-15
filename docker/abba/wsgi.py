"""Gunicorn entry point for the production ABBA adapter."""

from app import create_app_from_env


application = create_app_from_env()
