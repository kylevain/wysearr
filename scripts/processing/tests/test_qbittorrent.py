from __future__ import annotations

import unittest
from typing import Any

import requests

from bookbot_lib.errors import (
    ConfigurationError,
    QbittorrentAuthenticationError,
    QbittorrentError,
    QbittorrentUnavailableError,
)
from bookbot_lib.qbittorrent import QbittorrentClient


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.payload = payload

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.trust_env = True
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.login_responses: list[FakeResponse] = [FakeResponse(text="Ok.")]
        self.responses: list[FakeResponse] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append((url, kwargs))
        return self.login_responses.pop(0)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class QbittorrentClientTests(unittest.TestCase):
    def make_client(self, session: FakeSession) -> QbittorrentClient:
        return QbittorrentClient(
            "http://qbittorrent:8080",
            "operator",
            "secret",
            session=session,  # type: ignore[arg-type]
        )

    def test_cookie_login_precedes_api_request(self) -> None:
        session = FakeSession()
        session.responses.append(FakeResponse(text="v5.1.4"))
        client = self.make_client(session)
        self.assertEqual("v5.1.4", client.application_version())
        self.assertEqual(1, len(session.posts))
        self.assertEqual("operator", session.posts[0][1]["data"]["username"])
        self.assertEqual("secret", session.posts[0][1]["data"]["password"])
        self.assertFalse(session.trust_env)
        self.assertEqual(
            "http://qbittorrent:8080/", session.headers.get("Referer")
        )

    def test_failed_login_does_not_echo_credentials(self) -> None:
        session = FakeSession()
        session.login_responses = [FakeResponse(status_code=403, text="Fails.")]
        client = self.make_client(session)
        with self.assertRaises(QbittorrentAuthenticationError) as caught:
            client.application_version()
        self.assertNotIn("secret", str(caught.exception))

    def test_login_connection_failure_is_retryable_unavailability(self) -> None:
        class UnavailableSession(FakeSession):
            def post(self, url: str, **kwargs: Any) -> FakeResponse:
                raise requests.exceptions.ConnectionError("connection refused")

        client = self.make_client(UnavailableSession())
        with self.assertRaises(QbittorrentUnavailableError):
            client.application_version()

    def test_server_error_is_retryable_unavailability(self) -> None:
        session = FakeSession()
        session.login_responses = [FakeResponse(status_code=503)]
        client = self.make_client(session)
        with self.assertRaises(QbittorrentUnavailableError):
            client.application_version()

    def test_tls_configuration_failure_is_not_retryable_unavailability(self) -> None:
        class InvalidTlsSession(FakeSession):
            def post(self, url: str, **kwargs: Any) -> FakeResponse:
                raise requests.exceptions.SSLError("certificate verify failed")

        client = self.make_client(InvalidTlsSession())
        with self.assertRaises(QbittorrentError) as caught:
            client.application_version()
        self.assertNotIsInstance(caught.exception, QbittorrentUnavailableError)

    def test_403_reauthenticates_once(self) -> None:
        session = FakeSession()
        session.login_responses = [
            FakeResponse(text="Ok."),
            FakeResponse(text="Ok."),
        ]
        session.responses = [
            FakeResponse(status_code=403),
            FakeResponse(text="v5.1.4"),
        ]
        client = self.make_client(session)
        self.assertEqual("v5.1.4", client.application_version())
        self.assertEqual(2, len(session.posts))
        self.assertEqual(2, len(session.requests))

    def test_invalid_torrent_json_is_rejected(self) -> None:
        session = FakeSession()
        session.responses = [FakeResponse(payload={"not": "a list"})]
        client = self.make_client(session)
        with self.assertRaises(QbittorrentError):
            client.torrents()

    def test_imported_category_is_created_with_source_save_path(self) -> None:
        session = FakeSession()
        session.responses = [
            FakeResponse(payload={"ebooks": {"savePath": "/downloads/ebooks"}}),
            FakeResponse(text="Ok."),
        ]
        client = self.make_client(session)
        client.ensure_imported_category(
            "ebooks", "ebooks-imported", "/downloads/ebooks"
        )
        method, url, kwargs = session.requests[-1]
        self.assertEqual("POST", method)
        self.assertTrue(url.endswith("/api/v2/torrents/createCategory"))
        self.assertEqual("/downloads/ebooks", kwargs["data"]["savePath"])

    def test_existing_imported_category_path_mismatch_is_rejected(self) -> None:
        session = FakeSession()
        session.responses = [
            FakeResponse(
                payload={
                    "ebooks": {"savePath": "/downloads/ebooks"},
                    "ebooks-imported": {"savePath": "/downloads/elsewhere"},
                }
            )
        ]
        client = self.make_client(session)
        with self.assertRaises(ConfigurationError):
            client.ensure_imported_category(
                "ebooks", "ebooks-imported", "/downloads/ebooks"
            )

    def test_delete_with_files_uses_explicit_true(self) -> None:
        session = FakeSession()
        session.responses = [FakeResponse(text="Ok.")]
        client = self.make_client(session)
        client.delete_with_files("a" * 40)
        self.assertEqual("true", session.requests[-1][2]["data"]["deleteFiles"])


if __name__ == "__main__":
    unittest.main()
