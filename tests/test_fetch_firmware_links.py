"""Behavioral tests for the firmware catalog client."""

from __future__ import annotations

from typing import Any

import pytest

import fetch_firmware_links as firmware_links


class FakeResponse:
    """Minimal HTTP response with a JSON payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        """Simulate a successful HTTP status check."""

    def json(self) -> Any:
        """Return the configured JSON payload."""
        return self.payload


class FakeSession:
    """Requests session that returns queued responses and records requests."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, int]] = []

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, url: str, timeout: int) -> FakeResponse:
        self.calls.append((url, timeout))
        return next(self.responses)


def test_fetch_links_returns_only_downloadable_firmware(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "modelDTOList": [
                            {"id": 7, "model": "OTHER"},
                            {"id": "42", "model": firmware_links.MODEL_NAME},
                        ]
                    },
                }
            ),
            FakeResponse(
                {
                    "code": "200",
                    "data": [
                        {"versionName": "1.2.0", "pubDate": "2026-01-01", "otaUrl": "https://example.test/fw"},
                        {"versionName": "unavailable"},
                        None,
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr(firmware_links.requests, "Session", lambda: session)

    result = firmware_links.fetch_links()

    assert result == [
        {
            "model": "DH2300",
            "version": "1.2.0",
            "date": "2026-01-01",
            "url": "https://example.test/fw",
        }
    ]
    assert session.headers == {"Accept-Language": "de-DE"}
    assert session.calls == [
        (firmware_links.MODEL_LIST_URL, 30),
        (f"{firmware_links.API_BASE}/getFirmwareList?modelId=42", 30),
    ]


@pytest.mark.parametrize(
    ("payload", "error_type", "message"),
    [
        ([], TypeError, "invalid response"),
        ({"code": "500", "msg": "API unavailable"}, RuntimeError, "API unavailable"),
    ],
)
def test_get_json_rejects_invalid_or_failed_responses(
    payload: Any,
    error_type: type[Exception],
    message: str,
) -> None:
    session = FakeSession([FakeResponse(payload)])

    with pytest.raises(error_type, match=message):
        firmware_links.get_json(session, "https://example.test/api")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"data": None}, "invalid model list"),
        ({"data": {"modelDTOList": "not-a-list"}}, "invalid model list"),
        ({"data": {"modelDTOList": [{"model": "DH2300", "id": "bad"}]}}, "invalid ID"),
        ({"data": {"modelDTOList": []}}, "model not found"),
    ],
)
def test_get_model_id_rejects_invalid_catalog_entries(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    message: str,
) -> None:
    monkeypatch.setattr(firmware_links, "get_json", lambda *_: payload)

    with pytest.raises((TypeError, RuntimeError), match=message):
        firmware_links.get_model_id(FakeSession([]), firmware_links.MODEL_NAME)


def test_fetch_links_rejects_non_list_firmware_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(
        [
            FakeResponse({"code": 200, "data": {"modelDTOList": [{"id": 42, "model": "DH2300"}]}}),
            FakeResponse({"code": 200, "data": {"unexpected": "shape"}}),
        ]
    )
    monkeypatch.setattr(firmware_links.requests, "Session", lambda: session)

    with pytest.raises(TypeError, match="invalid firmware list"):
        firmware_links.fetch_links()
