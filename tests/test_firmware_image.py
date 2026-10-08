"""Behavioral tests for verified firmware extraction."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from subprocess import CalledProcessError

import pytest

import firmware_image


def _write_firmware_archive(
    path: Path,
    *,
    omitted_member: str | None = None,
    include_manifest: bool = True,
    corrupt_checksum: bool = False,
    kernel_content: bytes = b"Linux version 6.1.115+ (test) #1\nkernel image",
) -> None:
    """Create a small valid firmware archive with configurable integrity faults."""
    members = {
        firmware_image.KERNEL_MEMBER: kernel_content,
        firmware_image.DEVICE_TREE_MEMBER: b"device tree",
        firmware_image.SQUASHFS_MEMBER: b"squashfs image",
    }
    members.pop(omitted_member, None)
    checksums = [f"{hashlib.md5(content).hexdigest()} {name.removeprefix('./')}" for name, content in members.items()]
    if corrupt_checksum:
        first_member = next(iter(members))
        checksums[0] = f"{'0' * 32} {first_member.removeprefix('./')}"

    with tarfile.open(path, "w") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if include_manifest:
            manifest = "\n".join([*checksums, "malformed", ""]).encode()
            info = tarfile.TarInfo(firmware_image.CHECKSUM_MANIFEST_MEMBER)
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))


def _populate_module_tree(command: list[str], *, omit_modules: bool = False, omit_dep: bool = False) -> None:
    """Write a small module tree at the destination requested by unsquashfs."""
    module_root = Path(command[command.index("-d") + 1]) / firmware_image.MODULES_PATH
    module_root.mkdir(parents=True)
    if not omit_modules:
        module = module_root / "kernel/drivers/example.ko"
        module.parent.mkdir(parents=True)
        module.write_bytes(b"module")
    if not omit_dep:
        (module_root / "modules.dep").write_text("kernel/drivers/example.ko:\n")


def test_extract_firmware_image_verifies_and_assembles_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful extraction writes verified boot files and the requested module tree."""
    image_path = tmp_path / "firmware.img"
    output_dir = tmp_path / "extracted"
    output_dir.mkdir()
    (output_dir / "old-file").write_text("replace me")
    _write_firmware_archive(image_path)
    monkeypatch.setattr(firmware_image.shutil, "which", lambda _: "/usr/bin/unsquashfs")
    monkeypatch.setattr(
        firmware_image.subprocess,
        "run",
        lambda command, **_: _populate_module_tree(command),
    )

    summary = firmware_image.extract_firmware_image(image_path, output_dir)

    assert summary.module_count == 1
    assert summary.kernel_version == "6.1.115+"
    kernel_content = b"Linux version 6.1.115+ (test) #1\nkernel image"
    assert summary.kernel_md5 == hashlib.md5(kernel_content).hexdigest()
    assert summary.device_tree_md5 == hashlib.md5(b"device tree").hexdigest()
    assert (output_dir / "boot/ug_kernel").read_bytes() == kernel_content
    assert (output_dir / "boot/ug-rk3576-dh2300.dtb").read_bytes() == b"device tree"
    assert (output_dir / firmware_image.MODULES_PATH / "kernel/drivers/example.ko").read_bytes() == b"module"
    assert not (output_dir / "old-file").exists()


def test_extract_firmware_image_requires_unsquashfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Extraction reports a clear error when the external extractor is unavailable."""
    image_path = tmp_path / "firmware.img"
    _write_firmware_archive(image_path)
    monkeypatch.setattr(firmware_image.shutil, "which", lambda _: None)

    with pytest.raises(FileNotFoundError, match="unsquashfs is required"):
        firmware_image.extract_firmware_image(image_path, tmp_path / "output")


@pytest.mark.parametrize("output_kind", ["file", "symlink"])
def test_extract_firmware_image_rejects_unsafe_output_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_kind: str,
) -> None:
    """A file or symlink cannot be used as the extraction output directory."""
    image_path = tmp_path / "firmware.img"
    _write_firmware_archive(image_path)
    monkeypatch.setattr(firmware_image.shutil, "which", lambda _: "/usr/bin/unsquashfs")
    output_dir = tmp_path / "output"
    if output_kind == "file":
        output_dir.write_text("not a directory")
    else:
        output_dir.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(FileExistsError, match="not a safe directory"):
        firmware_image.extract_firmware_image(image_path, output_dir)


def test_extract_firmware_image_rejects_output_containing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction refuses to replace a directory containing the source image."""
    image_path = tmp_path / "firmware.img"
    _write_firmware_archive(image_path)
    monkeypatch.setattr(firmware_image.shutil, "which", lambda _: "/usr/bin/unsquashfs")

    with pytest.raises(FileExistsError, match="cannot contain the firmware image"):
        firmware_image.extract_firmware_image(image_path, tmp_path)


@pytest.mark.parametrize(
    ("archive_options", "message"),
    [
        ({"include_manifest": False}, "missing md5sum.txt"),
        ({"corrupt_checksum": True}, "Checksum mismatch"),
        ({"omitted_member": firmware_image.DEVICE_TREE_MEMBER}, "missing ug-rk3576-dh2300.dtb"),
        ({"kernel_content": b""}, "does not contain a Linux version banner"),
        ({"kernel_content": b"not a Linux kernel"}, "does not contain a Linux version banner"),
    ],
)
def test_extract_firmware_image_rejects_invalid_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive_options: dict[str, object],
    message: str,
) -> None:
    """Missing manifest data, members, or matching digests prevent extraction."""
    image_path = tmp_path / "firmware.img"
    _write_firmware_archive(image_path, **archive_options)
    monkeypatch.setattr(firmware_image.shutil, "which", lambda _: "/usr/bin/unsquashfs")

    with pytest.raises(RuntimeError, match=message):
        firmware_image.extract_firmware_image(image_path, tmp_path / "output")


def test_extract_firmware_image_reports_unsquashfs_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed external extraction is translated into a useful runtime error."""
    image_path = tmp_path / "firmware.img"
    _write_firmware_archive(image_path)
    monkeypatch.setattr(firmware_image.shutil, "which", lambda _: "/usr/bin/unsquashfs")

    def fail_extraction(*_: object, **__: object) -> None:
        raise CalledProcessError(1, "unsquashfs", stderr="bad SquashFS")

    monkeypatch.setattr(firmware_image.subprocess, "run", fail_extraction)

    with pytest.raises(RuntimeError, match="bad SquashFS"):
        firmware_image.extract_firmware_image(image_path, tmp_path / "output")


@pytest.mark.parametrize("omitted_entry", ["modules", "modules.dep"])
def test_extract_firmware_image_rejects_incomplete_module_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omitted_entry: str,
) -> None:
    """A module tree needs both at least one module and its dependency index."""
    image_path = tmp_path / "firmware.img"
    _write_firmware_archive(image_path)
    monkeypatch.setattr(firmware_image.shutil, "which", lambda _: "/usr/bin/unsquashfs")
    monkeypatch.setattr(
        firmware_image.subprocess,
        "run",
        lambda command, **_: _populate_module_tree(
            command,
            omit_modules=omitted_entry == "modules",
            omit_dep=omitted_entry == "modules.dep",
        ),
    )

    with pytest.raises(RuntimeError, match="no complete kernel module tree"):
        firmware_image.extract_firmware_image(image_path, tmp_path / "output")
