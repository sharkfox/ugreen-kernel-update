"""Fetch DH2300 firmware download endpoints from UGREEN's catalog API."""

from __future__ import annotations

from typing import Any, Final, TypedDict

import requests

API_BASE: Final = "https://api-eur.ugnas.com/api/system/v2/sa/official"
MODEL_LIST_URL: Final = f"{API_BASE}/query/model?type=ugos-pro"
MODEL_NAME: Final = "DH2300"


class FirmwareRecord(TypedDict):
    """Normalized release fields used by the CLI and kernel sidecar."""

    model: str
    version: str | None
    date: str | None
    url: str


def get_json(session: requests.Session, url: str) -> dict[str, Any]:
    """Request and validate a successful UGREEN JSON response."""
    response = session.get(url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError(f"UGREEN returned an invalid response for {url}")
    if str(payload.get("code")) != "200":
        raise RuntimeError(payload.get("msg", f"UGREEN API error for {url}"))
    return payload


def get_model_id(session: requests.Session, model_name: str) -> int:
    """Look up a model ID by its exact catalog name."""
    payload = get_json(session, MODEL_LIST_URL)
    data = payload.get("data")
    models = data.get("modelDTOList") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise TypeError("UGREEN returned an invalid model list")

    for model in models:
        if isinstance(model, dict) and model.get("model") == model_name:
            try:
                return int(model["id"])
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(f"UGREEN returned an invalid ID for {model_name}") from error
    raise RuntimeError(f"UGREEN model not found: {model_name}")


def fetch_links() -> list[FirmwareRecord]:
    """Fetch firmware download endpoints for the DH2300."""
    with requests.Session() as session:
        session.headers.update({"Accept-Language": "de-DE"})
        model_id = get_model_id(session, MODEL_NAME)
        firmware_url = f"{API_BASE}/getFirmwareList?modelId={model_id}"
        payload = get_json(session, firmware_url)
        firmware_list = payload.get("data")

    if not isinstance(firmware_list, list):
        raise TypeError(f"UGREEN returned an invalid firmware list for {MODEL_NAME}")

    records: list[FirmwareRecord] = []
    for firmware in firmware_list:
        if not isinstance(firmware, dict):
            continue
        url = firmware.get("otaUrl")
        if url is None or url == "":
            continue
        version = firmware.get("versionName")
        date = firmware.get("pubDate")
        if not isinstance(url, str):
            raise TypeError("UGREEN returned an invalid firmware URL")
        if version is not None and not isinstance(version, str):
            raise TypeError("UGREEN returned an invalid firmware version")
        if date is not None and not isinstance(date, str):
            raise TypeError("UGREEN returned an invalid firmware publication date")
        records.append({"model": MODEL_NAME, "version": version, "date": date, "url": url})
    return records
