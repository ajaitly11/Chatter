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
    if download_count < 1 or note_count != 1:
        raise SystemExit(
            "expected at least one download link and exactly one version note in docs/index.html "
            f"(found {download_count} and {note_count})"
        )

    path.write_text(html, encoding="utf-8")
    print(f"Updated landing page to {tag}")


if __name__ == "__main__":
    main()
