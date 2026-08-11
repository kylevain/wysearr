# Decisions

2026-08-09: Clean Debian rebuild chosen because legacy automation was
undocumented.

Naming: wyseadmin@wysearr.

2026-08-11: Discord request architecture chosen.

Discord channels define media intent. The request listener does not infer media type from message content.

Media-specific channels route requests to deterministic handlers:
- movies/tv
- ebooks
- audiobooks
- manga/comics
- roms
- sheet music
- future media types

Pilot may generate structured requests but does not directly control acquisition services.

SQLite approved as a local service-state pathway for request tracking, history, recovery, and auditability. Databases remain service-owned and are not centralized by default.

Naming context:
- Dewey references the Dewey Decimal System and represents the media library conversational interface.
- Huey and Louie follow the Donald Duck nephews naming pattern. Huey is assigned to Discord request intake. Louie is reserved for future media-library automation.

2026-08-11: SQLite backup strategy identified as future architecture work.

VainLab services may use service-owned SQLite databases. A future backup strategy should evaluate consistent SQLite-safe backup methods across services.

This is intentionally outside Huey/Dewey/Louie implementation scope.
