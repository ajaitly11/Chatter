"""macOS Accessibility trust helpers.

Trust is tied to the *launching bundle identity*, not just the Python binary:
Chatter.app (launched via double-click/`open`) gets its own TCC entry,
separate from a bare `python main.py` run from Terminal. Checking with the
plain API never shows the OS prompt, so a never-granted app fails silently —
`request_trust()` uses the prompting variant so macOS actually surfaces the
dialog and adds the app to System Settings.
"""

import subprocess
import logging

import AVFoundation as AV

from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions
import Quartz

_PROMPT_KEY = "AXTrustedCheckOptionPrompt"
logger = logging.getLogger("chatter.permissions")

def microphone_authorization_status() -> str:
    """Return the current macOS microphone TCC state for this executable."""
    try:
        status = int(AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio))
    except Exception:
        logger.exception("couldn't query macOS microphone authorization")
        return "unknown"
    return {
        0: "not_determined",
        1: "restricted",
        2: "denied",
        3: "authorized",
    }.get(status, "unknown")


def is_microphone_authorized() -> bool:
    return microphone_authorization_status() == "authorized"


def request_microphone_access() -> bool:
    """Ask macOS to show its microphone prompt for the Chatter bundle.

    The completion callback is asynchronous. Callers should check
    ``microphone_authorization_status()`` again after the user responds.
    """
    status = microphone_authorization_status()
    if status == "authorized":
        return True
    if status in {"denied", "restricted"}:
        return False
    try:
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio,
            lambda granted: logger.info("macOS microphone permission response: %s", bool(granted)),
        )
    except Exception:
        logger.exception("couldn't request macOS microphone permission")
    return False


def accessibility_status() -> tuple[bool, bool]:
    """Return (AX trust, post-event trust) for this running bundle.

    Chatter uses Accessibility for the focused-field/correction APIs and
    Core Graphics to post the Cmd+V event. On recent macOS releases those
    checks can refresh at slightly different times, so onboarding must not
    rely on only one of them.
    """
    ax_trusted = False
    post_event = False
    try:
        ax_trusted = bool(AXIsProcessTrusted())
    except Exception:
        logger.exception("couldn't query macOS Accessibility trust")
    try:
        post_event = bool(Quartz.CGPreflightPostEventAccess())
    except Exception:
        logger.exception("couldn't query macOS post-event access")
    return ax_trusted, post_event


def is_trusted() -> bool:
    ax_trusted, post_event = accessibility_status()
    return ax_trusted or post_event


def request_trust() -> bool:
    """Triggers the OS prompt (if not already trusted/denied) and ensures
    this app appears in System Settings > Accessibility."""
    ax_result = bool(AXIsProcessTrustedWithOptions({_PROMPT_KEY: True}))
    post_result = False
    try:
        post_result = bool(Quartz.CGRequestPostEventAccess())
    except Exception:
        logger.exception("couldn't request macOS post-event access")
    ax_trusted, post_event = accessibility_status()
    result = ax_result or post_result or ax_trusted or post_event
    logger.info(
        "Accessibility request: ax_request=%s post_request=%s ax_trusted=%s post_event=%s available=%s",
        ax_result, post_result, ax_trusted, post_event, result,
    )
    return result


def open_accessibility_settings():
    subprocess.run([
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ])


def is_input_monitoring_trusted() -> bool:
    """Returns whether macOS allows this process to observe keyboard events."""
    try:
        return bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        return False


def _input_permission_probe_callback(proxy, event_type, event, refcon):
    return event


def can_create_input_monitoring_tap() -> bool:
    """Probe the same listen-only event tap used by the hotkey listener.

    CGPreflightListenEventAccess can lag behind the System Settings toggle,
    and CGRequestListenEventAccess does not reliably add an app to the list
    by itself. A real, short-lived tap is the useful readiness check and also
    gives macOS the event-tap registration it needs for this bundle.
    """
    try:
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            _input_permission_probe_callback,
            None,
        )
        if tap is None:
            return False
        Quartz.CFMachPortInvalidate(tap)
        return True
    except Exception:
        logger.exception("couldn't probe the input-monitoring event tap")
        return False


def input_monitoring_available() -> bool:
    return is_input_monitoring_trusted() or can_create_input_monitoring_tap()


def request_input_monitoring() -> bool:
    """Ask macOS to register this app for Input Monitoring.

    macOS may still require the user to enable the app manually in System
    Settings; the return value only indicates whether the request was
    accepted, not that the user has completed the toggle.
    """
    try:
        preflight_before = is_input_monitoring_trusted()
        requested = bool(Quartz.CGRequestListenEventAccess())
        probe = can_create_input_monitoring_tap()
        available = preflight_before or requested or probe
        logger.info(
            "Input Monitoring request: preflight_before=%s request_result=%s tap_probe=%s available=%s",
            preflight_before, requested, probe, available,
        )
        return available
    except Exception:
        logger.exception("couldn't request macOS Input Monitoring access")
        return False


def open_input_monitoring_settings():
    subprocess.run([
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    ])


def open_microphone_settings():
    subprocess.run([
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    ])
