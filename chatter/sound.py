"""Short audio cues for push-to-talk start/stop — reuses macOS's built-in
system sounds, no bundled audio files needed.
"""

import logging

from AppKit import NSSound

logger = logging.getLogger("chatter.sound")

_start_sound = NSSound.soundNamed_("Tink")
_stop_sound = NSSound.soundNamed_("Pop")


def play_start():
    try:
        if _start_sound is None:
            logger.warning("start sound Tink is not available")
            return
        _start_sound.stop()
        _start_sound.setVolume_(1.0)
        played = _start_sound.play()
        logger.info("start cue requested: played=%s", played)
    except Exception:
        logger.exception("failed to play start sound")


def play_stop():
    try:
        if _stop_sound is None:
            logger.warning("stop sound Pop is not available")
            return
        _stop_sound.stop()
        _stop_sound.setVolume_(1.0)
        played = _stop_sound.play()
        logger.info("stop cue requested: played=%s", played)
    except Exception:
        logger.exception("failed to play stop sound")
