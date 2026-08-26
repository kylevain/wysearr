#!/usr/bin/env python3
"""Louie: cached, read-only status joiner for WyseARR."""
from __future__ import annotations

import json
import http.cookiejar
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGGER = logging.getLogger("louie")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("LOUIE_DB_PATH", "/state/louie.db"))
HUEY_DB = Path(os.environ.get("HUEY_DB_PATH", "/huey-state/huey.db"))
POLL_SECONDS = max(15, int(os.environ.get("LOUIE_POLL_SECONDS", "60")))
GENERAL_ONLY = "content_class = 'general'"
STALLED_AFTER_SECONDS = os.environ.get("LOUIE_STALLED_AFTER_SECONDS")
UPSTREAM_ERRORS: dict[str, str] = {}
POLL_STATE = {
    "ever_completed": False,
    "last_successful_poll": None,
    "last_error": None,
    "upstreams": {},
}
POLL_STATE_LOCK = threading.Lock()

STATUS_ORDER = ("submitted", "searching", "downloading", "importing", "stalled", "seeding", "completed", "failed", "ambiguous", "unparsed", "manual_review")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_huey_readonly() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{HUEY_DB}?mode=ro", uri=True)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            request_id TEXT PRIMARY KEY, origin TEXT NOT NULL, content_class TEXT NOT NULL DEFAULT 'general', routing_state TEXT NOT NULL DEFAULT 'live',
            external_id TEXT, service TEXT, requested_by TEXT, requested_at TEXT, title TEXT, year INTEGER,
            media_type TEXT, status TEXT NOT NULL, progress_pct REAL, file_name TEXT, download_client TEXT,
            indexer TEXT, protocol TEXT, size INTEGER, eta INTEGER, searching_since TEXT, last_checked TEXT,
            download_state TEXT, source_updated_at TEXT NOT NULL, raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, status TEXT NOT NULL,
            observed_at TEXT NOT NULL, detail_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS items_status_idx ON items(status);
        CREATE INDEX IF NOT EXISTS history_request_idx ON item_history(request_id, id);
        CREATE TABLE IF NOT EXISTS shelfarr_correlations (
            shelfarr_id TEXT PRIMARY KEY, external_source TEXT NOT NULL, classification TEXT NOT NULL,
            huey_request_id TEXT, observed_at TEXT NOT NULL
        );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
        if "download_state" not in columns:
            db.execute("ALTER TABLE items ADD COLUMN download_state TEXT")
        if "routing_state" not in columns:
            db.execute("ALTER TABLE items ADD COLUMN routing_state TEXT NOT NULL DEFAULT 'live'")
        db.commit()

def huey_items() -> list[dict]:
    if not HUEY_DB.exists():
        return []
    with closing(open_huey_readonly()) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    result = []
    for row in rows:
        value = dict(row)
        status = value.get("status") or "queued"
        error = value.get("error")
        routing_state = "superseded" if value.get("media_type") == "ebooks" and not value.get("service") or value.get("service") == "prowlarr" else "live"
        if status in {"complete", "completed"}:
            status = "completed"
        elif status == "needs_selection":
            status = "unparsed" if error and "Start the request with" in error else "ambiguous"
        elif status in {"new", "processing"}:
            status = "submitted"
        elif status == "queued":
            status = "accepted" if value.get("external_id") else "submitted"
        result.append({
            "request_id": str(value["id"]), "origin": "discord", "content_class": "general",
            "routing_state": routing_state,
            "external_id": value.get("external_id"), "service": value.get("service"),
            "requested_by": value.get("discord_username") or value.get("discord_user_id"),
            "requested_at": value.get("created_at"), "title": value.get("title") or value.get("raw_request"),
            "year": None, "media_type": value.get("media_type"), "status": status,
            "progress_pct": None, "file_name": value.get("external_title"), "download_client": None,
            "indexer": None, "protocol": None, "size": None, "eta": None,
            "searching_since": value.get("dispatch_started_at"), "last_checked": None,
            "download_state": None,
            "source_updated_at": value.get("updated_at") or value.get("created_at"), "error": error,
        })
    return result


def disc_items() -> list[dict]:
    if not HUEY_DB.exists():
        return []
    with closing(open_huey_readonly()) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute("SELECT * FROM trusted_library_events ORDER BY id DESC").fetchall()
    result = []
    for row in rows:
        value = dict(row)
        status = "manual_review" if value.get("state") == "manual_review" else ("completed" if value.get("state") == "completed" else value.get("state", "submitted"))
        result.append({
            "request_id": f"physical-{value['id']}", "origin": "physical_disc", "content_class": "general",
            "routing_state": "live",
            "external_id": value.get("radarr_movie_id") or value.get("sonarr_series_id"), "service": "radarr" if value.get("radarr_movie_id") else "sonarr" if value.get("sonarr_series_id") else None,
            "requested_by": None, "requested_at": None, "title": value.get("title") or "Physical disc review", "year": value.get("year"),
            "media_type": value.get("media_type"), "status": status, "progress_pct": 100 if status == "completed" else None,
            "file_name": value.get("final_path"), "download_client": None, "indexer": None, "protocol": None,
            "size": value.get("size_bytes"), "eta": None, "searching_since": None, "last_checked": value.get("updated_at"),
            "download_state": None,
            "source_updated_at": value.get("updated_at") or value.get("created_at"), "error": value.get("error"),
        })
    return result


def get_json(url: str, headers: dict[str, str] | None = None) -> object:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def qbit_json(url: str) -> object:
    base = os.environ.get("QBITTORRENT_URL", "").rstrip("/")
    credentials = urllib.parse.urlencode({
        "username": os.environ.get("QBITTORRENT_USERNAME", ""),
        "password": os.environ.get("QBITTORRENT_PASSWORD", ""),
    }).encode()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    login = urllib.request.Request(base + "/api/v2/auth/login", data=credentials, method="POST")
    with opener.open(login, timeout=10) as response:
        if response.read().decode().strip() != "Ok.":
            raise RuntimeError("qBittorrent authentication rejected")
    with opener.open(urllib.request.Request(url, method="GET"), timeout=10) as response:
        body = response.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body


def arr_snapshot(service: str) -> dict[str, list[dict]]:
    version = "v1" if service == "lidarr" else "v3"
    noun = {"radarr": "movie", "sonarr": "series", "lidarr": "artist"}[service]
    base = os.environ.get(f"{service.upper()}_URL", "").rstrip("/")
    key = os.environ.get(f"{service.upper()}_API_KEY", "")
    if not base or not key:
        return {"library": [], "queue": [], "wanted": [], "history": []}
    headers = {"X-Api-Key": key}
    def fetch(path: str) -> list[dict]:
        try:
            value = get_json(f"{base}/api/{version}/{path}", headers)
        except Exception as error:
            LOGGER.error("upstream failure service=%s url=%s error=%s", service, f"{base}/api/{version}/{path}", error)
            return []
        if isinstance(value, dict):
            return value.get("records", [])
        return value if isinstance(value, list) else []
    return {"library": fetch(noun), "queue": fetch("queue?page=1&pageSize=1000"), "wanted": fetch("wanted/missing?page=1&pageSize=1000"), "history": fetch("history?page=1&pageSize=1000")}


def download_snapshot() -> tuple[dict[str, dict], dict[str, dict]]:
    torrents: dict[str, dict] = {}
    sab: dict[str, dict] = {}
    base = os.environ.get("QBITTORRENT_URL", "").rstrip("/")
    if base:
        try:
            data = qbit_json(base + "/api/v2/torrents/info")
            torrents = {str(item.get("hash", "")).lower(): item for item in data if item.get("hash")}
        except Exception as error:
            UPSTREAM_ERRORS["qBittorrent"] = str(error)
            LOGGER.error("upstream failure service=qBittorrent url=%s error=%s", base + "/api/v2/torrents/info", error)
    base = os.environ.get("SABNZBD_URL", "").rstrip("/")
    key = os.environ.get("SABNZBD_API_KEY", "")
    if base and key:
        for mode in ("queue", "history"):
            try:
                data = get_json(base + "/api?" + urllib.parse.urlencode({"mode": mode, "output": "json", "apikey": key}))
                rows = data.get("queue", {}).get("slots", []) if mode == "queue" else data.get("history", {}).get("slots", [])
                for row in rows:
                    identifier = str(row.get("nzo_id") or row.get("name") or "")
                    if identifier: sab[identifier] = {**row, "_mode": mode}
            except Exception as error:
                UPSTREAM_ERRORS["SABnzbd"] = str(error)
                LOGGER.error("upstream failure service=SABnzbd url=%s error=%s", base + "/api?mode=" + mode, error)
    return torrents, sab


def shelfarr_snapshot() -> tuple[dict[str, dict], dict[str, int]]:
    adapter = ShelfarrAdapter(os.environ.get("SHELFARR_URL", "http://shelfarr"), os.environ.get("SHELFARR_API_TOKEN", ""))
    payload = adapter.requests()
    records = payload.get("requests", []) if isinstance(payload, dict) else []
    result: dict[str, dict] = {}
    counts = {"huey_sequence": 0, "huey_external": 0, "unrecognized": 0}
    with closing(open_huey_readonly()) as source:
        max_id = source.execute("SELECT COALESCE(MAX(id), 0) FROM requests").fetchone()[0]
    with closing(sqlite3.connect(DB_PATH)) as db:
        for record in records:
            request = record.get("request", {}) if isinstance(record, dict) else {}
            source = str(request.get("external_source") or "")
            match = __import__("re").fullmatch(r"huey:(\d+)", source)
            if not match:
                classification = "unrecognized"
            elif int(match.group(1)) <= max_id:
                classification = "huey_sequence"
            else:
                classification = "huey_external"
            counts[classification] += 1
            shelf_id = str(record.get("id", ""))
            huey_id = match.group(1) if classification == "huey_sequence" else None
            db.execute("INSERT OR REPLACE INTO shelfarr_correlations VALUES (?, ?, ?, ?, ?)", (shelf_id, source, classification, huey_id, now()))
            if huey_id: result[huey_id] = record
        db.commit()

    return result, counts


def _ratio_pct(total: object, remaining: object) -> float | None:
    """Percent complete from a total/remaining byte pair, or None if unusable."""

    try:
        size = float(total)
        left = float(remaining)
    except (TypeError, ValueError):
        return None
    if size <= 0 or left < 0 or left > size:
        return None
    return round((size - left) / size * 100, 2)


def queue_progress(record: dict) -> float | None:
    """Progress from an ARR queue record's own byte counts.

    ARR already tracks size and sizeleft for every queued item, so progress
    does not depend on reaching the download client at all. Previously it did:
    a hash that did not match left progress permanently null.
    """

    return _ratio_pct(record.get("size"), record.get("sizeleft"))


def torrent_progress(torrent: dict) -> float | None:
    """Progress from a qBittorrent torrent's 0..1 fraction."""

    try:
        fraction = float(torrent.get("progress"))
    except (TypeError, ValueError):
        return None
    if not 0 <= fraction <= 1:
        return None
    return round(fraction * 100, 2)


def sab_progress(slot: dict) -> float | None:
    """Progress for one SABnzbd slot; history slots are finished by definition."""

    if str(slot.get("_mode")) == "history":
        return 100.0
    return _ratio_pct(slot.get("mb"), slot.get("mbleft"))


def enrich(items: list[dict]) -> dict[str, int]:
    arr = {service: arr_snapshot(service) for service in ("radarr", "sonarr", "lidarr")}
    torrents, sab = download_snapshot()
    shelfarr, correlation_counts = shelfarr_snapshot() if os.environ.get("SHELFARR_API_TOKEN") else ({}, {"huey_sequence": 0, "huey_external": 0, "unrecognized": 0})
    by_id = {str(item.get("request_id")): item for item in items}
    for item in items:
        if item["routing_state"] == "superseded":
            continue
        service = item.get("service")
        external_id = str(item.get("external_id") or "")
        data = arr.get(service, {})
        library = next((x for x in data.get("library", []) if str(x.get("id")) == external_id), None)
        queue = next((x for x in data.get("queue", []) if str(x.get("movieId") or x.get("seriesId") or x.get("artistId")) == external_id), None)
        history = next((x for x in data.get("history", []) if str(x.get("movieId") or x.get("seriesId") or x.get("artistId")) == external_id), None)
        wanted = next((x for x in data.get("wanted", []) if str(x.get("movieId") or x.get("seriesId") or x.get("artistId")) == external_id), None)
        if library and (library.get("hasFile") or library.get("statistics", {}).get("episodeFileCount", 0)):
            item["status"] = "completed"
        elif queue:
            item["status"] = "importing" if str(queue.get("status", "")).casefold() in {"importpending", "importing"} else "downloading"
            item["download_client"] = queue.get("downloadClient")
            item["protocol"] = queue.get("protocol")
            item["size"] = queue.get("size")
            item["eta"] = queue.get("timeleft")
            item["external_id"] = external_id
            # ARR's own byte counts are the baseline. The download client can
            # refine them, but must not be the only thing that can produce a
            # number -- an unreachable client or an unmatched ID used to leave
            # every card without any progress at all.
            item["progress_pct"] = queue_progress(queue)
            download_id = str(queue.get("downloadId") or "")
            torrent = torrents.get(download_id.lower())
            if torrent:
                progress = torrent_progress(torrent)
                if progress is not None:
                    item["progress_pct"] = progress
                item["file_name"] = torrent.get("name")
                item["eta"] = torrent.get("eta")
                item["download_state"] = torrent.get("state")
            elif queue.get("protocol") == "usenet":
                item["download_client"] = "sabnzbd"
                # nzo_id is case-sensitive, so this cannot reuse the lowercased
                # hash lookup above.
                slot = sab.get(download_id) or sab.get(str(queue.get("title") or ""))
                if slot:
                    progress = sab_progress(slot)
                    if progress is not None:
                        item["progress_pct"] = progress
        elif wanted:
            item["status"] = "searching"
        elif history and str(history.get("eventType", "")).casefold() in {"grabbed", "importfailed", "downloadfailed"}:
            item["status"] = "failed" if "failed" in str(history.get("eventType", "")).casefold() else "accepted"
        elif item["status"] == "accepted" and STALLED_AFTER_SECONDS:
            try:
                if time.time() - datetime.fromisoformat(str(item.get("searching_since")).replace("Z", "+00:00")).timestamp() > float(STALLED_AFTER_SECONDS): item["status"] = "stalled"
            except (TypeError, ValueError):
                pass
        shelf = shelfarr.get(str(item.get("request_id")))
        if shelf:
            item["external_id"] = str(shelf.get("id")); item["service"] = "shelfarr"; item["external_status"] = shelf.get("status")
    return correlation_counts


def poll() -> None:
    """Refresh the durable source projection and best-effort upstream fields."""
    items = huey_items() + disc_items()
    enrich(items)
    with closing(sqlite3.connect(DB_PATH)) as db:
        for item in items:
            item["last_checked"] = now()
            encoded = json.dumps(item, sort_keys=True, default=str)
            old = db.execute("SELECT status, raw_json FROM items WHERE request_id = ?", (item["request_id"],)).fetchone()
            db.execute("""INSERT INTO items (
                request_id, origin, content_class, routing_state, external_id, service, requested_by,
                requested_at, title, year, media_type, status, progress_pct, file_name, download_client,
                indexer, protocol, size, eta, searching_since, last_checked, download_state, source_updated_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET origin=excluded.origin, content_class=excluded.content_class,
                routing_state=excluded.routing_state,
                external_id=excluded.external_id, service=excluded.service, requested_by=excluded.requested_by,
                requested_at=excluded.requested_at, title=excluded.title, year=excluded.year, media_type=excluded.media_type,
                status=excluded.status, progress_pct=excluded.progress_pct, file_name=excluded.file_name,
                download_client=excluded.download_client, indexer=excluded.indexer, protocol=excluded.protocol,
                size=excluded.size, eta=excluded.eta, searching_since=excluded.searching_since,
                last_checked=excluded.last_checked, download_state=excluded.download_state,
                source_updated_at=excluded.source_updated_at, raw_json=excluded.raw_json""",
                (item["request_id"], item["origin"], item["content_class"], item["routing_state"], item["external_id"], item["service"], item["requested_by"], item["requested_at"], item["title"], item["year"], item["media_type"], item["status"], item["progress_pct"], item["file_name"], item["download_client"], item["indexer"], item["protocol"], item["size"], item["eta"], item["searching_since"], item["last_checked"], item.get("download_state"), item["source_updated_at"], encoded))
            if not old or old[0] != item["status"]:
                db.execute("INSERT INTO item_history(request_id, status, observed_at, detail_json) VALUES (?, ?, ?, ?)", (item["request_id"], item["status"], item["last_checked"], encoded))
        db.commit()

    upstreams = upstream_poll()
    with POLL_STATE_LOCK:
        POLL_STATE.update(
            ever_completed=True,
            last_successful_poll=now(),
            last_error=None,
            upstreams=upstreams,
        )


def poll_loop() -> None:
    while True:
        try:
            poll()
        except Exception as error:
            LOGGER.exception("poll failed: %s", error)
            with POLL_STATE_LOCK:
                POLL_STATE["last_error"] = str(error)
        time.sleep(POLL_SECONDS)


def upstream_poll() -> dict[str, object]:
    checks = {
        "radarr": ("RADARR_URL", "RADARR_API_KEY", "/api/v3/system/status"),
        "sonarr": ("SONARR_URL", "SONARR_API_KEY", "/api/v3/system/status"),
        "lidarr": ("LIDARR_URL", "LIDARR_API_KEY", "/api/v1/system/status"),
        "qBittorrent": ("QBITTORRENT_URL", None, "/api/v2/app/version"),
        "sabnzbd": ("SABNZBD_URL", "SABNZBD_API_KEY", "/api?mode=version&output=json"),
    }
    result: dict[str, object] = {}
    UPSTREAM_ERRORS.clear()
    for name, (base_key, api_key, path) in checks.items():
        base = os.environ.get(base_key, "")
        if not base:
            result[name] = {"status": "not_configured"}; continue
        try:
            headers = {"X-Api-Key": os.environ[api_key]} if api_key and os.environ.get(api_key) else {}
            if name == "qBittorrent":
                qbit_json(base.rstrip("/") + path)
            else:
                get_json(base.rstrip("/") + path, headers)
            result[name] = {"status": "ok"}
        except Exception as error:
            url = base.rstrip("/") + path
            UPSTREAM_ERRORS[name] = str(error)
            LOGGER.error("upstream failure service=%s url=%s error=%s", name, url, error)
            result[name] = {"status": "error", "url": url, "error": str(error)}
    shelfarr = ShelfarrAdapter(
        os.environ.get("SHELFARR_URL", "http://shelfarr"),
        os.environ.get("SHELFARR_API_TOKEN", ""),
    )
    try:
        shelfarr.requests(); result["shelfarr"] = {"status": "ok"}
    except Exception as error:
        url = shelfarr.base_url + "/api/v1/requests"
        UPSTREAM_ERRORS["shelfarr"] = str(error)
        LOGGER.error("upstream failure service=shelfarr url=%s error=%s", url, error)
        result["shelfarr"] = {"status": "error", "url": url, "error": str(error)}
    return result


class ShelfarrAdapter:
    """Read-only Shelfarr API client; mutation endpoints are intentionally absent."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def requests(self) -> object:
        if not self.token:
            raise RuntimeError("Shelfarr API token is not configured")
        return get_json(
            self.base_url + "/api/v1/requests",
            {"Authorization": f"Bearer {self.token}"},
        )


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, default=str).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = (STATIC / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if parsed.path == "/api/health":
            with POLL_STATE_LOCK:
                state = dict(POLL_STATE)
            last = state["last_successful_poll"]
            age = None
            if last:
                age = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()))
            self.send_json({**state, "poll_age_seconds": age}); return
        if parsed.path == "/api/requests" or parsed.path.startswith("/api/requests/"):
            params = urllib.parse.parse_qs(parsed.query); status = params.get("status", [None])[0]; origin = params.get("origin", [None])[0]; media_type = params.get("media_type", [None])[0]; requested_by = params.get("requested_by", [None])[0]; routing_state = params.get("routing_state", ["live"])[0]; include_non_general = params.get("content_class", ["general"])[0] == "all"
            with closing(sqlite3.connect(DB_PATH)) as db:
                db.row_factory = sqlite3.Row
                if parsed.path != "/api/requests":
                    item_id = parsed.path.rsplit("/", 1)[-1]; row = db.execute("SELECT * FROM items WHERE request_id = ?", (item_id,)).fetchone();
                    if not row: self.send_json({"error": "not found"}, 404); return
                    history = [dict(x) for x in db.execute("SELECT status, observed_at, detail_json FROM item_history WHERE request_id=? ORDER BY id", (item_id,))]; result = dict(row); result["history"] = history; self.send_json(result); return
                clauses = [] if include_non_general else [GENERAL_ONLY]; values = []
                if routing_state != "all": clauses.append("routing_state = ?"); values.append(routing_state)
                for key, value in (("status", status), ("origin", origin), ("media_type", media_type), ("requested_by", requested_by)):
                    if value: clauses.append(key + " = ?"); values.append(value)
                sql = "SELECT * FROM items" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY requested_at DESC, request_id DESC"
                self.send_json([dict(row) for row in db.execute(sql, values)])
            return
        self.send_json({"error": "not found"}, 404)

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    init_db()
    threading.Thread(target=poll_loop, daemon=True, name="poller").start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("LOUIE_PORT", "8090"))), Handler).serve_forever()

if __name__ == "__main__": main()
