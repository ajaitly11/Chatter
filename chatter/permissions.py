"""macOS Accessibility trust helpers.

Trust is tied to the *launching bundle identity*, not just the Python binary:
Chatter.app (launched via double-click/`open`) gets its own TCC entry,
separate from a bare `python main.py` run from Terminal. Checking with the
plain API never shows the OS prompt, so a never-granted app fails silently —
`request_trust()` uses the prompting variant so macOS actually surfaces the
dialog and adds the app to System Settings.
"""

import subprocess

from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions

_PROMPT_KEY = "AXTrustedCheckOptionPrompt"


def is_trusted() -> bool:
    return bool(AXIsProcessTrusted())


def request_trust() -> bool:
    """Triggers the OS prompt (if not already trusted/denied) and ensures
    this app appears in System Settings > Accessibility."""
    return bool(AXIsProcessTrustedWithOptions({_PROMPT_KEY: True}))


def open_accessibility_settings():
    subprocess.run([
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ])
