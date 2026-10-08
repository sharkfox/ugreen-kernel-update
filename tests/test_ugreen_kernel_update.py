"""Behavioral tests for the firmware command-line interface."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from click.testing import CliRunner

import firmware_image
import ugreen_kernel_update as updater


def _mock_sudo_copy(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Run sudo cp requests as unprivileged cp within the test's temporary rootfs."""
    commands: list[list[str]] = []
    native_run = updater.subprocess.run

    def copy_with_native_cp(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert command[0] == "sudo"
        if command[1] == "mkdir":
            return native_run(["mkdir", *command[2:]], **kwargs)
        assert command[1] == "cp"
        assert command[2:5] == ["-R", "-P", "--"]
        return native_run(["cp", *command[2:]], **kwargs)

    monkeypatch.setattr(updater.subprocess, "run", copy_with_native_cp)
    return commands


class FakeResponse:
    """HTTP response supporting JSON and streamed download payloads."""

    def __init__(self, payload: Any = None, chunks: list[bytes] | None = None) -> None:
        self.payload = payload
        self.chunks = chunks or []

    def raise_for_status(self) -> None:
        """Simulate a successful HTTP status check."""

    def json(self) -> Any:
        """Return the configured JSON payload."""
        return self.payload

    def iter_content(self, chunk_size: int):
        """Yield configured chunks using the requested chunk size."""
        assert chunk_size == updater.DOWNLOAD_CHUNK_SIZE
        yield from self.chunks

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeSession:
    """Requests session that returns queued responses and records requests."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return next(self.responses)


@pytest.fixture
def runner() -> CliRunner:
    """Provide an isolated Click command runner."""
    return CliRunner()


@pytest.fixture
def firmware_record() -> dict[str, str | None]:
    """Return representative metadata for a downloadable release."""
    return {
        "model": "DH2300",
        "version": "1.2.0",
        "date": "2026-01-01",
        "url": "https://api.example.test/firmware",
    }


def test_list_command_supports_text_and_json(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both list formats expose the same fetched firmware record."""
    records = [{"model": "DH2300", "version": "1.2.0", "date": "2026-01-01", "url": "https://example.test/fw"}]
    monkeypatch.setattr(updater, "fetch_links", lambda: records)

    text_result = runner.invoke(updater.cli, ["list"])
    json_result = runner.invoke(updater.cli, ["list", "--json"])

    assert text_result.exit_code == 0
    assert "1.2.0" in text_result.output
    assert "https://example.test/fw" in text_result.output
    assert json_result.exit_code == 0
    assert json.loads(json_result.output) == records


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (RuntimeError("API unavailable"), "API unavailable"),
        (requests.Timeout("request timed out"), "request timed out"),
    ],
)
def test_list_command_reports_catalog_failures(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    message: str,
) -> None:
    """Catalog request failures are reported as concise CLI errors."""

    def fail_fetch() -> None:
        raise failure

    monkeypatch.setattr(updater, "fetch_links", fail_fetch)

    result = runner.invoke(updater.cli, ["list"])

    assert result.exit_code != 0
    assert message in result.output


def test_list_command_reports_empty_catalog(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty catalog produces a clear CLI error."""
    monkeypatch.setattr(updater, "fetch_links", list)

    result = runner.invoke(updater.cli, ["list"])

    assert result.exit_code != 0
    assert "No firmware releases" in result.output


def test_legacy_command_prints_json(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy entry point continues to emit firmware JSON."""
    records = [{"model": "DH2300", "version": "1.2.0", "date": None, "url": "https://example.test/fw"}]
    monkeypatch.setattr(updater, "fetch_links", lambda: records)

    result = runner.invoke(updater.fetch_firmware_cli)

    assert result.exit_code == 0
    assert json.loads(result.output) == records


def test_extract_command_reports_artifact_metadata(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extraction command displays artifact locations and checksums."""
    image_path = tmp_path / "firmware.img"
    image_path.write_bytes(b"image")
    output_dir = tmp_path / "output"
    rootfs_dir = tmp_path / "rootfs"
    summary = firmware_image.ExtractionSummary(3, "6.1.115+", "kernel-md5", "dtb-md5")
    monkeypatch.setattr(updater, "extract_firmware_image", lambda *_: summary)
    monkeypatch.setattr(updater, "ROOTFS_DIRECTORY", rootfs_dir)
    monkeypatch.setattr(updater.platform, "uname", lambda: pytest.fail("default must not inspect the platform"))

    result = runner.invoke(updater.cli, ["extract", str(image_path), "--output-dir", str(output_dir)])

    assert result.exit_code == 0
    assert str(output_dir / "boot") in result.output
    assert "Kernel version: 6.1.115+" in result.output
    assert "Kernel MD5: kernel-md5" in result.output
    assert "DTB MD5: dtb-md5" in result.output
    assert f"3 kernel modules extracted to {output_dir / updater.MODULES_PATH}" in result.output
    assert not rootfs_dir.exists()


def test_extract_command_writes_to_rootfs_only_on_dh2300(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit write flag copies boot and module trees only on a DH2300 host."""
    image_path = tmp_path / "firmware.img"
    image_path.write_bytes(b"image")
    output_dir = tmp_path / "extracted"
    rootfs_dir = tmp_path / "rootfs"
    monkeypatch.setattr(updater, "ROOTFS_DIRECTORY", rootfs_dir)
    monkeypatch.setattr(updater.platform, "uname", lambda: ("Linux", "dh2300", "6.1.115+", "#1", "aarch64", ""))

    def extract_image(_: Path, destination: Path) -> firmware_image.ExtractionSummary:
        boot_dir = destination / "boot"
        module_dir = destination / updater.MODULES_PATH / "6.1.115+"
        boot_dir.mkdir(parents=True)
        module_dir.mkdir(parents=True)
        (boot_dir / "ug_kernel").write_bytes(b"kernel")
        (module_dir / "example.ko").write_bytes(b"module")
        (destination / updater.MODULES_PATH / "6.1.115+" / "build").symlink_to("/kernel/build")
        return firmware_image.ExtractionSummary(1, "6.1.115+", "kernel-md5", "dtb-md5")

    monkeypatch.setattr(updater, "extract_firmware_image", extract_image)
    (rootfs_dir / updater.MODULES_PATH / "6.1.115+").mkdir(parents=True)
    (rootfs_dir / updater.MODULES_PATH / "6.1.115+" / "build").symlink_to("/kernel/build")
    commands = _mock_sudo_copy(monkeypatch)

    result = runner.invoke(updater.cli, ["extract", str(image_path), "--output-dir", str(output_dir), "--write-rootfs"])

    assert result.exit_code == 0
    assert (rootfs_dir / "boot/ug_kernel").read_bytes() == b"kernel"
    assert (rootfs_dir / updater.MODULES_PATH / "6.1.115+" / "example.ko").read_bytes() == b"module"
    assert (rootfs_dir / updater.MODULES_PATH / "6.1.115+" / "build").is_symlink()
    assert f"Extracted files copied to {rootfs_dir}" in result.output
    assert [command[1] for command in commands] == ["mkdir", "cp", "mkdir", "cp"]


def test_extract_command_rejects_rootfs_write_on_non_target(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write flag refuses to copy files when uname does not identify a DH2300."""
    image_path = tmp_path / "firmware.img"
    image_path.write_bytes(b"image")
    rootfs_dir = tmp_path / "rootfs"
    monkeypatch.setattr(updater, "ROOTFS_DIRECTORY", rootfs_dir)
    monkeypatch.setattr(updater.platform, "uname", lambda: ("Linux", "workstation", "6.1", "#1", "x86_64", ""))
    monkeypatch.setattr(
        updater,
        "extract_firmware_image",
        lambda *_: firmware_image.ExtractionSummary(0, "6.1.115+", "kernel-md5", "dtb-md5"),
    )

    result = runner.invoke(updater.cli, ["extract", str(image_path), "--write-rootfs"])

    assert result.exit_code != 0
    assert "only available on a DH2300 target" in result.output
    assert not rootfs_dir.exists()


def test_extract_command_reports_rootfs_copy_failure(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rootfs permission failure is reported as a concise CLI error."""
    image_path = tmp_path / "firmware.img"
    image_path.write_bytes(b"image")
    monkeypatch.setattr(updater, "ROOTFS_DIRECTORY", tmp_path / "rootfs")
    monkeypatch.setattr(updater.platform, "uname", lambda: ("Linux", "dh2300", "6.1.115+", "#1", "aarch64", ""))
    monkeypatch.setattr(
        updater,
        "extract_firmware_image",
        lambda *_: firmware_image.ExtractionSummary(0, "6.1.115+", "kernel-md5", "dtb-md5"),
    )

    def fail_copy(command: list[str], **_: object) -> None:
        raise subprocess.CalledProcessError(1, command, stderr="read-only root filesystem")

    monkeypatch.setattr(updater.subprocess, "run", fail_copy)

    result = runner.invoke(updater.cli, ["extract", str(image_path), "--write-rootfs"])

    assert result.exit_code != 0
    assert "Could not copy extracted files" in result.output
    assert "read-only root filesystem" in result.output


def test_extract_command_translates_extraction_errors(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expected extraction errors are presented through Click's error path."""
    image_path = tmp_path / "firmware.img"
    image_path.write_bytes(b"image")

    def fail_extraction(*_: object) -> None:
        raise RuntimeError("bad firmware")

    monkeypatch.setattr(updater, "extract_firmware_image", fail_extraction)

    result = runner.invoke(updater.cli, ["extract", str(image_path)])

    assert result.exit_code != 0
    assert "bad firmware" in result.output


def test_update_dry_run_selects_latest_release(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """A dry run selects the newest release without making a download."""
    older = {**firmware_record, "version": "1.1.0", "date": "2025-12-01"}
    newer = {**firmware_record, "version": "1.2.0", "date": "2026-01-01"}
    monkeypatch.setattr(updater, "fetch_links", lambda: [older, newer])

    result = runner.invoke(updater.cli, ["update", "--dry-run", "--cache-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Selected DH2300 firmware 1.2.0" in result.output
    assert "Download endpoint: https://api.example.test/firmware" in result.output
    assert not list(tmp_path.iterdir())


def test_update_reports_unknown_version(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """An unknown requested release reports the available versions."""
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])

    result = runner.invoke(updater.cli, ["update", "missing", "--dry-run", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "version missing was not found" in result.output
    assert "1.2.0" in result.output


def test_update_reuses_a_cached_image(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """A nonempty cached image is extracted without downloading it again."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    rootfs_dir = tmp_path / "rootfs"
    monkeypatch.setattr(updater, "ROOTFS_DIRECTORY", rootfs_dir)
    monkeypatch.setattr(updater.platform, "uname", lambda: ("Linux", "dh2300", "6.1.115+", "#1", "aarch64", ""))
    cached_image = cache_dir / "ugreen-DH2300-1.2.0.img"
    cached_image.write_bytes(b"cached image")
    summary = firmware_image.ExtractionSummary(1, "6.1.115+", "kernel-md5", "dtb-md5")
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])

    def extract_image(_: Path, destination: Path) -> firmware_image.ExtractionSummary:
        boot_dir = destination / "boot"
        module_dir = destination / updater.MODULES_PATH
        boot_dir.mkdir(parents=True)
        module_dir.mkdir(parents=True)
        (boot_dir / "ug_kernel").write_bytes(b"kernel")
        (module_dir / "example.ko").write_bytes(b"module")
        (module_dir / "source").symlink_to("/kernel/source")
        return summary

    monkeypatch.setattr(updater, "extract_firmware_image", extract_image)
    commands = _mock_sudo_copy(monkeypatch)

    result = runner.invoke(updater.cli, ["update", "--write-rootfs", "--cache-dir", str(cache_dir)])

    assert result.exit_code == 0
    assert f"Using cached firmware at {cached_image}" in result.output
    assert "Kernel MD5: kernel-md5" in result.output
    assert (rootfs_dir / "boot/ug_kernel").read_bytes() == b"kernel"
    assert (rootfs_dir / updater.MODULES_PATH / "example.ko").read_bytes() == b"module"
    assert (rootfs_dir / updater.MODULES_PATH / "source").is_symlink()
    assert [command[1] for command in commands] == ["mkdir", "cp", "mkdir", "cp"]


def test_update_requires_a_download_url(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """A selected release without an endpoint cannot be downloaded."""
    monkeypatch.setattr(updater, "fetch_links", lambda: [{**firmware_record, "url": None}])

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "no download endpoint" in result.output


def test_update_downloads_and_extracts_a_release(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """The update workflow resolves a temporary URL, streams the image, and extracts it."""
    direct_link = FakeResponse({"code": 200, "data": {"linkData": {"tempUrl": "https://cdn.example.test/image"}}})
    download = FakeResponse(chunks=[b"firmware", b"", b" image"])
    session = FakeSession([direct_link, download])
    summary = firmware_image.ExtractionSummary(2, "6.1.115+", "kernel-md5", "dtb-md5")
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: session)
    monkeypatch.setattr(updater, "extract_firmware_image", lambda *_: summary)

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    cached_image = tmp_path / "ugreen-DH2300-1.2.0.img"
    assert result.exit_code == 0
    assert cached_image.read_bytes() == b"firmware image"
    assert len(session.calls) == 2
    assert "2 kernel modules extracted" in result.output


def test_update_removes_partial_download_after_network_failure(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """A failed stream leaves neither a partial nor a final cache file."""

    class FailedStream(FakeResponse):
        """Stream response that fails after the destination file is opened."""

        def iter_content(self, chunk_size: int):
            yield b"partial"
            raise requests.ConnectionError("connection lost")

    session = FakeSession(
        [
            FakeResponse({"code": 200, "data": {"linkData": {"tempUrl": "https://cdn.example.test/image"}}}),
            FailedStream(),
        ]
    )
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: session)

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "connection lost" in result.output
    assert not list(tmp_path.iterdir())


def test_update_cleans_up_captcha_image_when_terminal_is_noninteractive(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """A CAPTCHA image is removed when verification needs an interactive terminal."""
    session = FakeSession(
        [
            FakeResponse({"code": 403, "msg": "verification required"}),
            FakeResponse({"code": 200, "data": {"uuid": "captcha-1", "img": base64.b64encode(b"image").decode()}}),
        ]
    )
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: session)

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "rerun this command in an interactive terminal" in result.output
    assert not list(tmp_path.iterdir())


def test_update_rejects_non_object_api_response(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """Malformed API JSON is reported as a CLI error rather than a traceback."""
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: FakeSession([FakeResponse([])]))

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "returned an invalid API response" in result.output


def test_update_rejects_success_response_without_temporary_url(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """A successful temporary-link response must contain the actual download URL."""
    session = FakeSession([FakeResponse({"code": 200, "data": {}})])
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: session)

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "did not return a temporary download URL" in result.output


def test_update_reports_failed_captcha_api_response(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """A rejected CAPTCHA-image response stops the update before prompting."""
    session = FakeSession(
        [
            FakeResponse({"code": 403, "msg": "verification required"}),
            FakeResponse({"code": 500, "msg": "captcha unavailable"}),
        ]
    )
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: session)

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "captcha unavailable" in result.output
    assert not list(tmp_path.iterdir())


def test_update_reports_incomplete_captcha_payload(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
) -> None:
    """CAPTCHA data without its required UUID and image is rejected."""
    session = FakeSession(
        [
            FakeResponse({"code": 403, "msg": "verification required"}),
            FakeResponse({"code": 200, "data": {"uuid": "captcha-1"}}),
        ]
    )
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: session)

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "incomplete CAPTCHA data" in result.output


@pytest.mark.parametrize(
    ("verification_code", "verification_payload", "expected_message"),
    [
        (
            "1234",
            {"code": 200, "data": {"linkData": {"tempUrl": "https://cdn.example.test/image"}}},
            "Downloaded firmware",
        ),
        ("", None, "verification code cannot be empty"),
        ("1234", {"code": 403, "msg": "UGREEN rejected the verification code."}, "rejected the verification code"),
    ],
)
def test_update_handles_interactive_captcha_outcomes(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    firmware_record: dict[str, str | None],
    verification_code: str,
    verification_payload: dict[str, Any] | None,
    expected_message: str,
) -> None:
    """Interactive CAPTCHA success and rejection paths produce the expected CLI result."""

    class TTY:
        """Input stream marker for Click's interactive-terminal check."""

        def isatty(self) -> bool:
            """Report that CAPTCHA input is interactive."""
            return True

    responses = [
        FakeResponse({"code": 403, "msg": "verification required"}),
        FakeResponse({"code": 200, "data": {"uuid": "captcha-1", "img": base64.b64encode(b"image").decode()}}),
    ]
    if verification_payload is not None:
        responses.append(FakeResponse(verification_payload))
        if verification_payload.get("code") == 200:
            responses.append(FakeResponse(chunks=[b"firmware"]))
    session = FakeSession(responses)
    monkeypatch.setattr(updater, "fetch_links", lambda: [firmware_record])
    monkeypatch.setattr(updater.requests, "Session", lambda: session)
    monkeypatch.setattr(updater, "sys", SimpleNamespace(stdin=TTY()))
    monkeypatch.setattr(updater.click, "prompt", lambda *_args, **_kwargs: verification_code)
    monkeypatch.setattr(updater.click, "launch", lambda *_: (_ for _ in ()).throw(OSError("no image viewer")))
    monkeypatch.setattr(
        updater,
        "extract_firmware_image",
        lambda *_: firmware_image.ExtractionSummary(1, "6.1.115+", "k", "d"),
    )

    result = runner.invoke(updater.cli, ["update", "--cache-dir", str(tmp_path)])

    assert expected_message in result.output
    if verification_payload is not None and verification_payload.get("code") == 200:
        assert result.exit_code == 0
        assert (tmp_path / "ugreen-DH2300-1.2.0.img").read_bytes() == b"firmware"
    else:
        assert result.exit_code != 0
    assert not list(tmp_path.glob("ugreen-captcha-*.png"))
