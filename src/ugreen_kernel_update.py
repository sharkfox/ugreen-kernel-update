"""Click command-line interface for downloading UGREEN NAS firmware."""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Final

import click
import requests

from fetch_firmware_links import API_BASE, fetch_links
from firmware_image import MODULES_PATH, extract_firmware_image

DOWNLOAD_CHUNK_SIZE: Final = 1024 * 1024
CACHE_ROOT: Final = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
DEFAULT_CACHE_DIR: Final = CACHE_ROOT / "ugreen-kernel-update"
CAPTCHA_IMAGE_URL: Final = f"{API_BASE}/captchaImage"
ROOTFS_DIRECTORY: Final = Path("/")


def _load_links() -> list[dict[str, str | None]]:
    """Fetch firmware records and translate API failures into CLI errors.

    Returns:
        Firmware records containing model, version, date, and download URL.

    Raises:
        click.ClickException: If the API request fails or returns no records.
    """
    try:
        links = fetch_links()
    except (requests.RequestException, RuntimeError, TypeError) as error:
        raise click.ClickException(str(error)) from error
    if not links:
        raise click.ClickException("No firmware releases were returned by UGREEN.")
    return links


@click.group()
def cli() -> None:
    """Fetch or download firmware for a UGREEN DH2300 NAS."""


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print releases as JSON.")
def list_firmware(as_json: bool) -> None:
    """List available DH2300 firmware releases."""
    links = _load_links()
    if as_json:
        click.echo(json.dumps(links, indent=2))
        return

    click.echo(f"{'VERSION':<16} {'DATE':<12} DOWNLOAD ENDPOINT")
    for firmware in links:
        click.echo(f"{firmware['version'] or '-':<16} {firmware['date'] or '-':<12} {firmware['url']}")


@cli.command("extract")
@click.argument(
    "image_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_CACHE_DIR / "extracted",
    show_default=True,
    help="Directory for the boot files and module tree.",
)
@click.option(
    "--write-rootfs",
    is_flag=True,
    help="Copy extracted files into / on a DH2300 target. Disabled by default.",
)
def extract_firmware(image_path: Path, output_dir: Path, write_rootfs: bool) -> None:
    """Extract firmware artifacts and optionally copy them into the target rootfs.

    Args:
        image_path: Firmware image to extract.
        output_dir: Directory for the extracted boot files and module tree.
        write_rootfs: Whether to copy extracted files into `/` on a DH2300.
    """
    _extract_and_report(image_path, output_dir, write_rootfs)


def _extract_and_report(image_path: Path, output_dir: Path, write_rootfs: bool = False) -> None:
    """Extract firmware artifacts, report metadata, and optionally write to `/`."""
    try:
        summary = extract_firmware_image(image_path, output_dir)
    except (FileExistsError, OSError, RuntimeError, tarfile.TarError) as error:
        raise click.ClickException(str(error)) from error
    if write_rootfs:
        _copy_extracted_files_to_rootfs(output_dir)
    click.echo(f"Boot files extracted to {output_dir / 'boot'}")
    click.echo(f"Kernel version: {summary.kernel_version}")
    click.echo(f"Kernel MD5: {summary.kernel_md5}")
    click.echo(f"DTB MD5: {summary.device_tree_md5}")
    click.echo(f"{summary.module_count} kernel modules extracted to {output_dir / MODULES_PATH}")
    if write_rootfs:
        click.echo(f"Extracted files copied to {ROOTFS_DIRECTORY}")


def _copy_extracted_files_to_rootfs(output_dir: Path) -> None:
    """Copy boot files and kernel modules to `/` on a DH2300 target only.

    Args:
        output_dir: Root directory containing extracted `boot` and `usr/lib/modules` trees.

    Raises:
        click.ClickException: If the host is not a DH2300 or the files cannot be copied.
    """
    uname_output = " ".join(platform.uname()).casefold()
    if "dh2300" not in uname_output:
        raise click.ClickException("--write-rootfs is only available on a DH2300 target.")

    try:
        for relative_path in (Path("boot"), Path(MODULES_PATH)):
            source = output_dir / relative_path
            destination = ROOTFS_DIRECTORY / relative_path
            subprocess.run(["sudo", "mkdir", "-p", str(destination)], check=True, capture_output=True, text=True)
            subprocess.run(
                ["sudo", "cp", "-R", "-P", "--", f"{source}/.", str(destination)],
                check=True,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.CalledProcessError) as error:
        details = (
            error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        )
        raise click.ClickException(f"Could not copy extracted files to {ROOTFS_DIRECTORY}: {details}") from error


@click.command()
def fetch_firmware_cli() -> None:
    """Print available firmware releases as JSON (legacy command)."""
    click.echo(json.dumps(_load_links(), indent=2))


@cli.command("update")
@click.argument("version", required=False)
@click.option(
    "--cache-dir",
    "--output-dir",
    "cache_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_CACHE_DIR,
    show_default=True,
    help="Directory for cached firmware images.",
)
@click.option("--force", is_flag=True, help="Replace an existing image file.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the selected release without downloading it.",
)
@click.option(
    "--write-rootfs",
    is_flag=True,
    help="Copy extracted files into / on a DH2300 target. Disabled by default.",
)
def update_firmware(
    version: str | None,
    cache_dir: Path,
    force: bool,
    dry_run: bool,
    write_rootfs: bool,
) -> None:
    """Download a release and optionally copy extracted files into the target rootfs.

    Args:
        version: Optional firmware version to select.
        cache_dir: Directory for cached images and extracted files.
        force: Whether to replace an existing cached image.
        dry_run: Whether to display the selected release without downloading it.
        write_rootfs: Whether to copy extracted files into `/` on a DH2300.
    """
    links = _load_links()
    if version is None:
        firmware = max(links, key=lambda item: item.get("date") or "")
    else:
        firmware = next((item for item in links if item["version"] == version), None)
        if firmware is None:
            available = ", ".join(item["version"] or "unknown" for item in links)
            raise click.ClickException(f"Firmware version {version} was not found. Available: {available}")

    click.echo(f"Selected DH2300 firmware {firmware['version']} ({firmware['date'] or 'date unknown'}).")
    if dry_run:
        click.echo(f"Download endpoint: {firmware['url']}")
        return
    destination = _cache_path(firmware, cache_dir)
    if destination.is_file() and destination.stat().st_size > 0 and not force:
        click.echo(f"Using cached firmware at {destination}")
        _extract_and_report(destination, cache_dir / "extracted", write_rootfs)
        return
    if not firmware["url"]:
        raise click.ClickException("The selected release has no download endpoint.")

    try:
        with requests.Session() as session:
            session.headers.update({"Accept-Language": "de-DE"})
            direct_url = _resolve_download_url(session, firmware["url"], cache_dir)
            _download_image(session, direct_url, destination)
    except (
        requests.RequestException,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        raise click.ClickException(str(error)) from error
    _extract_and_report(destination, cache_dir / "extracted", write_rootfs)


def _cache_path(firmware: dict[str, str | None], cache_dir: Path) -> Path:
    """Build the stable cache path for one model and firmware version.

    Args:
        firmware: Firmware metadata used to identify the image.
        cache_dir: Directory in which cached images are stored.

    Returns:
        The path where the firmware image is cached.
    """
    model = re.sub(r"[^A-Za-z0-9._-]", "_", firmware["model"] or "unknown-model")
    version = re.sub(r"[^A-Za-z0-9._-]", "_", firmware["version"] or "unknown-version")
    return cache_dir / f"ugreen-{model}-{version}.img"


def _request_api_payload(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Request and validate a JSON payload from UGREEN's API.

    Args:
        session: Session used to make the request.
        url: API endpoint to request.
        params: Optional query parameters for the endpoint.

    Returns:
        The decoded API response object.

    Raises:
        requests.RequestException: If the request or HTTP response fails.
        TypeError: If the response JSON is not an object.
        ValueError: If the response body is not valid JSON.
    """
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("UGREEN returned an invalid API response.")
    return payload


def _resolve_download_url(
    session: requests.Session,
    endpoint: str,
    cache_dir: Path,
) -> str:
    """Resolve a firmware endpoint, prompting for CAPTCHA when required.

    Args:
        session: Session used for API requests.
        endpoint: Firmware-specific temporary-link endpoint.
        cache_dir: Directory used to store the CAPTCHA image.

    Returns:
        The temporary direct-download URL.

    Raises:
        click.ClickException: If verification needs a terminal or the code is empty.
        RuntimeError: If UGREEN rejects a request or returns no download URL.
        TypeError: If UGREEN returns malformed CAPTCHA data.
    """
    payload = _request_api_payload(session, endpoint)
    if str(payload.get("code")) == "200":
        return _temporary_url(payload)

    captcha_payload = _request_api_payload(session, CAPTCHA_IMAGE_URL)
    captcha_data = captcha_payload.get("data")
    if str(captcha_payload.get("code")) != "200" or not isinstance(captcha_data, dict):
        raise RuntimeError(captcha_payload.get("msg", "Could not load UGREEN CAPTCHA."))

    captcha_uuid = captcha_data.get("uuid")
    captcha_image = captcha_data.get("img")
    if not isinstance(captcha_uuid, str) or not isinstance(captcha_image, str):
        raise TypeError("UGREEN returned incomplete CAPTCHA data.")

    image_path = cache_dir / f"ugreen-captcha-{re.sub(r'[^A-Za-z0-9-]', '_', captcha_uuid)}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        image_path.write_bytes(base64.b64decode(captcha_image, validate=True))
        click.echo(f"UGREEN verification image: {image_path}")

        if not sys.stdin.isatty():
            raise click.ClickException(
                "UGREEN requires CAPTCHA verification; rerun this command in an interactive terminal."
            )
        try:
            click.launch(str(image_path))
        except OSError:
            click.echo("Open the verification image above to read the code.")
        verification_code = click.prompt("UGREEN verification code").strip()
        if not verification_code:
            raise click.ClickException("The verification code cannot be empty.")

        verified_payload = _request_api_payload(
            session,
            endpoint,
            params={"code": verification_code, "uuid": captcha_uuid},
        )
        if str(verified_payload.get("code")) != "200":
            raise RuntimeError(verified_payload.get("msg", "UGREEN rejected the verification code."))
        return _temporary_url(verified_payload)
    finally:
        image_path.unlink(missing_ok=True)


def _temporary_url(payload: dict[str, Any]) -> str:
    """Extract the direct temporary download URL from an API response.

    Args:
        payload: Successful UGREEN API response.

    Returns:
        The temporary URL for the firmware image.

    Raises:
        RuntimeError: If the response does not contain a temporary URL.
    """
    data = payload.get("data")
    link_data = data.get("linkData") if isinstance(data, dict) else None
    temporary_url = link_data.get("tempUrl") if isinstance(link_data, dict) else None
    if not isinstance(temporary_url, str) or not temporary_url:
        raise RuntimeError("UGREEN did not return a temporary download URL.")
    return temporary_url


def _download_image(
    session: requests.Session,
    url: str,
    destination: Path,
) -> None:
    """Stream a firmware image to its cache path using an atomic rename.

    Args:
        session: Session used to download the image.
        url: Temporary direct-download URL.
        destination: Final path for the cached image.

    Returns:
        None.

    Raises:
        requests.RequestException: If the download request fails.
        OSError: If the image cannot be written to the cache.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.part")

    try:
        with session.get(url, stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with temporary.open("wb") as firmware_file:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        firmware_file.write(chunk)
        temporary.replace(destination)
    except (requests.RequestException, OSError):
        temporary.unlink(missing_ok=True)
        raise

    click.echo(f"Downloaded firmware to {destination}")
    click.echo("Install the image from the NAS administration interface.")
