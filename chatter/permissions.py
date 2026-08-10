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
    """Return whether this bundle currently has Accessibility trust.

    The plain checks in ``accessibility_status()`` can stay stale inside an
    already-running process even after the user flips the switch on in
    System Settings — macOS does not always push the update to a process
    that asked before the toggle changed. Falling back to the prompting
    variant forces a fresh read so a long-lived process (the main app, not
    just onboarding) notices the grant without requiring the user to quit
    and relaunch Chatter.
    """
    ax_trusted, post_event = accessibility_status()
    if ax_trusted or post_event:
        return True
    return request_trust()


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
    by itself. This is diagnostic only: a successful tap creation is not
    proof that macOS will deliver events from other applications, so
    readiness must use CGPreflightListenEventAccess().
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
    # CGEventTapCreate can succeed for a process that is still not authorized
    # to observe other applications. Treating that probe as authorization
    # made Chatter appear healthy while it only received its own key events.
    return is_input_monitoring_trusted()


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
        # The request result and tap probe only mean that macOS accepted the
        # request/created a local tap. Re-read the official authorization
        # state; only that state proves cross-application keyboard delivery.
        preflight_after = is_input_monitoring_trusted()
        available = preflight_after
        logger.info(
            "Input Monitoring request: preflight_before=%s request_result=%s tap_probe=%s preflight_after=%s available=%s",
            preflight_before, requested, probe, preflight_after, available,
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
