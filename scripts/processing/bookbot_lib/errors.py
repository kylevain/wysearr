"""Domain exceptions used to classify retryable and permanent failures."""


class BookBotError(Exception):
    """Base class for BookBot failures."""


class ConfigurationError(BookBotError):
    """The service cannot operate safely with the current configuration."""


class QbittorrentError(BookBotError):
    """A qBittorrent API request failed."""


class QbittorrentAuthenticationError(QbittorrentError):
    """qBittorrent rejected the configured credentials."""


class QbittorrentUnavailableError(QbittorrentError):
    """qBittorrent is temporarily unreachable or unable to serve requests."""


class ImportErrorBase(BookBotError):
    """Base class for payload import failures."""


class UnsafeSourceError(ImportErrorBase):
    """The source or destination violates a filesystem safety invariant."""


class UnsupportedMediaError(ImportErrorBase):
    """The torrent contains no supported media or includes unsafe content."""


class MetadataCorrelationError(ImportErrorBase):
    """Trusted request metadata cannot be bound unambiguously to a torrent."""


class SourceChangedError(ImportErrorBase):
    """A supposedly completed payload changed while it was being copied."""
