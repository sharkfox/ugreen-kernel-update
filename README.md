# UGREEN Kernel Update

Download and extract UGREEN DH2300 firmware, including the kernel, device tree, and kernel modules.

## Requirements

- Python 3.9+
- [uv](https://docs.astral.sh/uv/)
- `unsquashfs` for extracting firmware modules

Install `unsquashfs` for your platform:

```sh
# Debian
sudo apt update
sudo apt install squashfs-tools

# macOS
brew install squashfs
```

On the DH2300, `/boot` is read-only by default. Remount it before writing firmware files:

```sh
sudo mount -o remount,rw /boot
```

Install the project environment:

```sh
uv sync
```

## Usage

Update to the latest firmware, extract it, and write the files to the DH2300 root filesystem:

```sh
uv run ugreen-kernel-update update --cache-dir /tmp/ugreen-kernel-update --write-rootfs
```

Other commands:

```sh
uv run ugreen-kernel-update list
uv run ugreen-kernel-update list --json
uv run ugreen-kernel-update check
uv run ugreen-kernel-update update --dry-run
uv run ugreen-kernel-update update [VERSION]
uv run ugreen-kernel-update extract firmware.img --output-dir extracted
```

Root filesystem writes are disabled by default. Add `--write-rootfs` to `extract` or `update` to copy the extracted boot files and modules to `/`. This option only runs on a DH2300 and uses `sudo` for the copy.

`update` writes the firmware release fields and extracted kernel release to `boot/ug_kernel.json`. `check` fetches only the available release list and compares it with that metadata. On a DH2300 it checks `/boot` and verifies the running kernel release; on other systems it checks the cached `extracted/boot` directory. It does not download firmware, and returns a nonzero status when no local extracted kernel is available or when a newer package exists.
