"""Small, privacy-respecting GitHub release checker for Chatter.

The checker only asks GitHub for the public latest-release record. It sends
no transcript, device identifier, or account information. The network call is
kept off the Qt thread so launch and dictation stay responsive.
"""

from __future__ import annotations

import json
import plistlib
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from . import config


REPOSITORY = "ajaitly11/Chatter"
RELEASES_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{REPOSITORY}/releases/latest"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(latest: str, current: str) -> bool:
    latest_version = parse_version(latest)
    current_version = parse_version(current)
    return latest_version is not None and current_version is not None and latest_version > current_version


def installed_version() -> str:
    """Read the version stamped into the app bundle, with a dev fallback."""
    candidates: list[Path] = []
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        candidates.append(executable.parents[1] / "Info.plist")
    candidates.append(Path(__file__).resolve().parents[1] / "Chatter.app" / "Contents" / "Info.plist")
    for path in candidates:
        try:
            with path.open("rb") as handle:
                value = plistlib.load(handle).get("CFBundleShortVersionString")
            if value:
                return str(value)
        except (OSError, plistlib.InvalidFileException, KeyError, TypeError, ValueError):
            continue
    return "0.0.0"


class UpdateChecker(QObject):
    available = pyqtSignal(str, str, str)  # version, release page, DMG URL
    current = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_version = installed_version()
        self._lock = threading.Lock()
        self._checking = False

    def check(self, force: bool = False):
        if not force and not config.load().get("updates_enabled", True):
            return
        with self._lock:
            if self._checking:
                return
            self._checking = True
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            request = urllib.request.Request(
                RELEASES_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Chatter-update-checker",
                },
            )
            with urllib.request.urlopen(request, timeout=6) as response:
                release = json.loads(response.read().decode("utf-8"))
            tag = str(release.get("tag_name", ""))
            version = tag.removeprefix("v")
            if not parse_version(version):
                raise RuntimeError("GitHub returned a release without a stable semantic version")
            page_url = str(release.get("html_url") or RELEASES_PAGE_URL)
            dmg_url = page_url
            for asset in release.get("assets", []):
                name = str(asset.get("name", "")).lower()
                if name.endswith(".dmg"):
                    dmg_url = str(asset.get("browser_download_url") or page_url)
                    break
            config.update(last_update_check=time.time())
            if is_newer(version, self.current_version):
                self.available.emit(version, page_url, dmg_url)
            else:
                self.current.emit(self.current_version)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            with self._lock:
                self._checking = False
