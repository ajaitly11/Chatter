#!/usr/bin/env python3
"""Keep the static landing page's download link on the tagged release."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: update_release_page.py v1.2.3")

    version = sys.argv[1].removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"invalid release version: {version!r}")

    path = Path(__file__).resolve().parents[1] / "docs" / "index.html"
    html = path.read_text(encoding="utf-8")
    tag = f"v{version}"

    html, download_count = re.subn(
        r"(releases/download/)v[0-9]+\.[0-9]+\.[0-9]+(/Chatter-macOS-arm64\.dmg)",
        rf"\g<1>{tag}\g<2>",
        html,
    )
    html, note_count = re.subn(
        r"(Chatter )v[0-9]+\.[0-9]+\.[0-9]+( · macOS arm64)",
        rf"\g<1>{tag}\g<2>",
        html,
    )

    # The landing page may expose the same download in several intentional
    # places (navigation, hero, and final CTA). Keep every one of those links
    # on the tagged release instead of forcing the design to have a single
    # button just to satisfy the release automation.
    # The current landing page intentionally keeps the download surface
    # minimal and does not show a version note. Older layouts did show one,
    # so update it when present, but do not make the release pipeline depend
    # on a piece of copy that the design may legitimately omit.
    if download_count < 1:
        raise SystemExit(
            "expected at least one download link in docs/index.html "
            f"(found {download_count}; version notes updated: {note_count})"
        )

    path.write_text(html, encoding="utf-8")
    print(f"Updated landing page to {tag}")


if __name__ == "__main__":
    main()
