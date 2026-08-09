"""Small, local-only hints about where a dictation will be inserted.

Chatter deliberately reads only the frontmost app identity and its window
title. It does not inspect page contents, selected text, document bodies, or
network state. The hint lets the optional cleanup model choose a sensible
format for an email, note, coding prompt, or social post without changing the
single streaming-ASR path.
"""

from dataclasses import dataclass, replace
import re

import AppKit
import Quartz


@dataclass(frozen=True)
class CaptureContext:
    app_name: str = ""
    bundle_id: str = ""
    window_title: str = ""
    mode: str = "general"

    @property
    def key(self) -> str:
        return "|".join((self.app_name, self.bundle_id, self.window_title, self.mode))

    def prompt_hint(self) -> str:
        title = self.window_title.replace("\n", " ").strip()[:140]
        app = self.app_name or "the foreground app"
        mode_rules = {
            "email": (
                "Treat this as an email or email reply: preserve greetings and sign-offs, "
                "use professional paragraphs and punctuation, and do not invent a subject, "
                "recipient, facts, or a signature."
            ),
            "notes": (
                "Treat this as notes or journaling: keep the speaker's first-person voice, "
                "use readable paragraphs, and create bullets only when the speaker clearly "
                "dictates a list."
            ),
            "coding": (
                "Treat this as a coding or AI-agent prompt: preserve technical names, "
                "commands, file paths, identifiers, and code-like punctuation; do not add "
                "email language or rewrite the request into a summary."
            ),
            "social": (
                "Treat this as a short social or chat post: keep the speaker's natural voice, "
                "make it readable, preserve hashtags and mentions, and do not add a formal "
                "email structure."
            ),
            "browser": (
                "This is a browser text field, but the exact page is uncertain. Use the "
                "window title only as a weak hint and otherwise preserve the speaker's wording."
            ),
            "general": (
                "Use neutral dictation formatting and preserve the speaker's wording."
            ),
        }
        title_hint = f" Window title hint: {title!r}." if title else ""
        return (
            f"Foreground app: {app!r}. Writing context: {self.mode}."
            f"{title_hint} {mode_rules.get(self.mode, mode_rules['general'])}"
        )

    def with_mode(self, mode: str) -> "CaptureContext":
        return replace(self, mode=mode)


def classify_mode(app_name: str = "", bundle_id: str = "", window_title: str = "") -> str:
    value = " ".join((app_name, bundle_id, window_title)).lower()
    if re.search(r"gmail|outlook|mail|inbox|compose|email|mail\.google", value):
        return "email"
    if re.search(r"notes|notion|obsidian|bear|day one|journal|drafts", value):
        return "notes"
    if re.search(
        r"claude|codex|chatgpt|terminal|iterm|warp|cursor|visual studio|vscode|"
        r"xcode|zed|pycharm|windsurf|codeium|developer",
        value,
    ):
        return "coding"
    if re.search(r"twitter|x\.com|linkedin|slack|discord|facebook|reddit|social", value):
        return "social"
    if re.search(r"safari|chrome|arc|firefox|brave|edge|browser", value):
        return "browser"
    return "general"


def _frontmost_window_title(pid: int) -> str:
    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        best_title = ""
        best_area = 0.0
        for window in windows:
            if window.get("kCGWindowOwnerPID") != pid:
                continue
            if window.get("kCGWindowLayer", 0) != 0:
                continue
            bounds = window.get("kCGWindowBounds") or {}
            area = float(bounds.get("Width", 0)) * float(bounds.get("Height", 0))
            if area > best_area:
                best_area = area
                best_title = str(window.get("kCGWindowName") or "")
        return best_title.strip()
    except Exception:
        return ""


def current_context() -> CaptureContext:
    """Return a best-effort context snapshot without reading document text."""
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return CaptureContext()
        app_name = str(app.localizedName() or "")
        bundle_id = str(app.bundleIdentifier() or "")
        title = _frontmost_window_title(int(app.processIdentifier()))
        return CaptureContext(
            app_name=app_name,
            bundle_id=bundle_id,
            window_title=title,
            mode=classify_mode(app_name, bundle_id, title),
        )
    except Exception:
        return CaptureContext()
