"""Extract kernel artifacts and module files from UGREEN firmware images."""

from __future__ import annotations

import hashlib
import mmap
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

COPY_CHUNK_SIZE: Final = 1024 * 1024
KERNEL_MEMBER: Final = "./ug_kernel"
DEVICE_TREE_MEMBER: Final = "./ug-rk3576-dh2300.dtb"
SQUASHFS_MEMBER: Final = "./kernel.squashfs"
CHECKSUM_MANIFEST_MEMBER: Final = "./md5sum.txt"
MODULES_PATH: Final = "usr/lib/modules"
KERNEL_VERSION_PATTERN: Final = re.compile(rb"Linux version ([^\s\x00]+)")


@dataclass(frozen=True)
class ExtractionSummary:
    """Verified metadata for artifacts extracted from a firmware image."""

    module_count: int
    kernel_version: str
    kernel_md5: str
    device_tree_md5: str


def extract_firmware_image(image_path: Path, output_dir: Path) -> ExtractionSummary:
    """Extract verified boot files and kernel modules from a firmware tar.

    Args:
        image_path: Path to the downloaded firmware image.
        output_dir: Directory to replace with the extracted root layout.

    Returns:
        Module count, kernel version, and verified MD5 digests for the kernel and device tree.

    Raises:
        FileExistsError: If the output path is unsafe or would contain the image.
        FileNotFoundError: If `unsquashfs` is unavailable.
        RuntimeError: If expected files, checksums, or module data are invalid.
        OSError: If archive data cannot be written to the destination.
        tarfile.TarError: If the firmware is not a readable tar archive.
    """
    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs is None:
        raise FileNotFoundError(
            "unsquashfs is required to extract modules; install squashfs-tools (macOS: brew install squashfs)."
        )
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise FileExistsError(f"Output path is not a safe directory: {output_dir}")
    if output_dir.resolve() in image_path.resolve().parents:
        raise FileExistsError("Output directory cannot contain the firmware image.")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}.", dir=output_dir.parent) as temporary_dir:
        staging_root = Path(temporary_dir)
        assembled_root, summary = _stage_firmware(image_path, staging_root, unsquashfs)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        assembled_root.replace(output_dir)

    return summary


def _stage_firmware(
    image_path: Path,
    staging_root: Path,
    unsquashfs: str,
) -> tuple[Path, ExtractionSummary]:
    """Verify image members and assemble the boot files and module tree.

    Args:
        image_path: Firmware archive to read.
        staging_root: Temporary directory for intermediate and assembled files.
        unsquashfs: Path to the SquashFS extraction executable.

    Returns:
        The assembled root directory and verified extraction summary.
    """
    assembled_root = staging_root / "root"
    boot_dir = assembled_root / "boot"
    boot_dir.mkdir(parents=True)

    with tarfile.open(image_path, "r:*") as archive:
        checksums = _read_checksum_manifest(archive)
        kernel_path = boot_dir / Path(KERNEL_MEMBER).name
        kernel_md5 = _copy_verified_member(archive, KERNEL_MEMBER, kernel_path, checksums)
        kernel_version = _read_kernel_version(kernel_path)
        device_tree_md5 = _copy_verified_member(
            archive,
            DEVICE_TREE_MEMBER,
            boot_dir / Path(DEVICE_TREE_MEMBER).name,
            checksums,
        )
        squashfs_image = staging_root / Path(SQUASHFS_MEMBER).name
        _copy_verified_member(archive, SQUASHFS_MEMBER, squashfs_image, checksums)

    summary = ExtractionSummary(
        module_count=_extract_module_tree(squashfs_image, staging_root, assembled_root, unsquashfs),
        kernel_version=kernel_version,
        kernel_md5=kernel_md5,
        device_tree_md5=device_tree_md5,
    )
    return assembled_root, summary


def _read_kernel_version(kernel_path: Path) -> str:
    """Read the Linux version string embedded in a kernel image.

    Args:
        kernel_path: Path to the extracted kernel image.

    Returns:
        The kernel release string from its Linux version banner.

    Raises:
        RuntimeError: If the kernel image does not contain a version banner.
        OSError: If the kernel image cannot be read.
    """
    with kernel_path.open("rb") as kernel_file:
        if kernel_file.seek(0, 2) == 0:
            raise RuntimeError("Firmware kernel does not contain a Linux version banner.")
        kernel_file.seek(0)
        with mmap.mmap(kernel_file.fileno(), length=0, access=mmap.ACCESS_READ) as kernel_data:
            match = KERNEL_VERSION_PATTERN.search(kernel_data)
            if match is None:
                raise RuntimeError("Firmware kernel does not contain a Linux version banner.")
            return kernel_data[match.start(1) : match.end(1)].decode("ascii")


def _extract_module_tree(
    squashfs_image: Path,
    staging_root: Path,
    assembled_root: Path,
    unsquashfs: str,
) -> int:
    """Extract only `/usr/lib/modules` and place it under the output root.

    Args:
        squashfs_image: Verified kernel SquashFS image.
        staging_root: Temporary directory for SquashFS output.
        assembled_root: Root directory being assembled for the user.
        unsquashfs: Path to the SquashFS extraction executable.

    Returns:
        Number of `.ko` module files extracted.

    Raises:
        RuntimeError: If extraction fails or the module tree is incomplete.
    """
    modules_stage = staging_root / "squashfs"
    modules_stage.mkdir()
    try:
        subprocess.run(
            [
                unsquashfs,
                "-f",
                "-d",
                str(modules_stage),
                str(squashfs_image),
                MODULES_PATH,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        details = error.stderr.strip() or "unknown SquashFS extraction error"
        raise RuntimeError(f"Could not extract kernel modules: {details}") from error

    modules_source = modules_stage / MODULES_PATH
    module_files = list(modules_source.rglob("*.ko"))
    if not module_files or not list(modules_source.rglob("modules.dep")):
        raise RuntimeError("The SquashFS image has no complete kernel module tree.")

    modules_destination = assembled_root / MODULES_PATH
    modules_destination.parent.mkdir(parents=True)
    shutil.move(str(modules_source), str(modules_destination))
    return len(module_files)


def _read_checksum_manifest(archive: tarfile.TarFile) -> dict[str, str]:
    """Read the image's MD5 manifest into a filename-to-digest mapping.

    Args:
        archive: Open firmware tar archive.

    Returns:
        Expected MD5 digests keyed by archive member name.

    Raises:
        RuntimeError: If the archive does not include its checksum manifest.
    """
    try:
        manifest = archive.extractfile(CHECKSUM_MANIFEST_MEMBER)
    except KeyError:
        manifest = None
    if manifest is None:
        manifest_name = CHECKSUM_MANIFEST_MEMBER.removeprefix("./")
        raise RuntimeError(f"Firmware archive is missing {manifest_name}.")

    checksums = {}
    for line in manifest.read().decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            checksums[parts[1].removeprefix("./")] = parts[0]
    return checksums


def _copy_verified_member(
    archive: tarfile.TarFile,
    member_name: str,
    destination: Path,
    checksums: dict[str, str],
) -> str:
    """Stream one tar member to disk and validate its manifest checksum.

    Args:
        archive: Open firmware tar archive.
        member_name: Member path inside the archive.
        destination: Output file path.
        checksums: Expected MD5 values from the archive manifest.

    Returns:
        The verified MD5 digest of the extracted member.

    Raises:
        RuntimeError: If the member is missing or its checksum does not match.
        OSError: If the member cannot be written to disk.
    """
    normalized_name = member_name.removeprefix("./")
    expected_digest = checksums.get(normalized_name)
    try:
        member = archive.extractfile(member_name)
    except KeyError:
        member = None
    if expected_digest is None or member is None:
        raise RuntimeError(f"Firmware archive is missing {normalized_name} or its checksum.")

    digest = hashlib.md5()
    with member, destination.open("wb") as output_file:
        while chunk := member.read(COPY_CHUNK_SIZE):
            digest.update(chunk)
            output_file.write(chunk)
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest:
        raise RuntimeError(f"Checksum mismatch for {normalized_name}.")
    return actual_digest
