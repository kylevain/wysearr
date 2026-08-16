"""BookBot's production import and retention components.

Keep package initialization lightweight.  In particular, the container health
probe imports :mod:`bookbot_lib.health`; eagerly importing the worker service
here would also load the HTTP and storage stacks before the probe could read a
small JSON marker.
"""

from __future__ import annotations


__all__ = ["BookBotConfig", "BookBotService", "CategorySpec"]


def __getattr__(name: str) -> object:
    """Lazily preserve the package's public convenience exports."""
    if name in {"BookBotConfig", "CategorySpec"}:
        from .config import BookBotConfig, CategorySpec

        value = {"BookBotConfig": BookBotConfig, "CategorySpec": CategorySpec}[name]
    elif name == "BookBotService":
        from .service import BookBotService

        value = BookBotService
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
