# Legacy BookBot migration

The recovered shell implementation remains outside this repository under
`/home/wyseadmin/legacy/bookbot` for historical reference only. It is not run by
the production stack and its Pi-specific paths and secret handling must not be
restored.

Its useful behavior has been ported to the Python BookBot service:

- media-specific destination and extension rules
- normalized filenames/folders
- duplicate preservation rather than overwrite
- direct-media imports onto the DAS
- terminal Huey state updates
- post-import retention cleanup

The production source is `scripts/processing/`; its persisted ledger is under
`config/bookbot`. Discord delivery is centralized in Huey, so the legacy direct
Discord shell functions and daily digest are intentionally retired.
