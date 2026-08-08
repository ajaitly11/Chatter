"""Locates or fetches the llama-server binary chatter/formatter.py needs for
the text-cleanup pass. transcribe.cpp ships its own binary through its
Python package, but llama.cpp's server is a separate native binary this
app doesn't bundle — without it, "Clean up with AI" just silently no-ops
(formatter.py falls back to the raw transcript). For that to work on a
machine other than the one this was built on, this discovers an existing
install or downloads the official prebuilt binary straight from
llama.cpp's GitHub releases.

macOS-only, matching the rest of this app (hotkey.py/overlay.py/
main_window.py are already all AppKit/Quartz-specific) — only macOS
arm64/x64 release assets are handled here.
"""

import json
import logging
import platform
import shutil
import stat
import tarfile
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from . import config

logger = logging.getLogger("chatter.llama_runtime")

RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "Chatter" / "runtime"
_RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
# Locations `brew install llama.cpp` uses on Apple Silicon / Intel.
_COMMON_PATHS = ["/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server"]


def find_installed() -> str | None:
    """Checked in order: the configured path, this app's own prior
    download, PATH, then the Homebrew locations."""
    configured = config.load().get("llama_server_bin")
    if configured and Path(configured).exists():
        return configured
    own = RUNTIME_DIR / "llama-server"
    if own.exists():
        return str(own)
    found = shutil.which("llama-server")
    if found:
        return found
    for p in _COMMON_PATHS:
        if Path(p).exists():
            return p
    return None


def _mac_asset_suffix() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"macos-{arch}"


def latest_release_asset() -> tuple[str, str, int]:
    """(asset_name, download_url, size) for the plain macOS build in the
    latest release — picked from that release's actual published asset
    list, not a hardcoded filename, so a naming change upstream doesn't
    silently break this."""
    req = urllib.request.Request(_RELEASES_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        release = json.loads(resp.read())
    suffix = _mac_asset_suffix()
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.startswith("llama-") and f"bin-{suffix}." in name:
            return name, asset["browser_download_url"], asset.get("size", 0)
    raise RuntimeError(f"no {suffix} build found in the latest llama.cpp release ({release.get('tag_name')})")


class LlamaServerDownloadWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)  # path to the extracted llama-server binary
    failed = pyqtSignal(str)

    def run(self):
        extract_dir = RUNTIME_DIR / "extracted"
        try:
            name, url, size_hint = latest_release_asset()
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            archive_path = RUNTIME_DIR / name
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp, open(archive_path, "wb") as f:
                total = int(resp.headers.get("Content-Length") or size_hint or 0)
                downloaded = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    self.progress.emit(downloaded, total)

            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir(parents=True)
            with tarfile.open(archive_path) as tar:
                tar.extractall(extract_dir)
            archive_path.unlink()

            binary = next(extract_dir.rglob("llama-server"), None)
            if binary is None:
                raise RuntimeError("llama-server binary not found inside the downloaded archive")
            dest = RUNTIME_DIR / "llama-server"
            shutil.copy2(binary, dest)
            mode = dest.stat().st_mode
            dest.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            config.update(llama_server_bin=str(dest))
            self.finished_ok.emit(str(dest))
        except Exception as e:
            logger.exception("llama-server download failed")
            self.failed.emit(str(e))
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
