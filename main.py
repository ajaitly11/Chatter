"""
Chatter — local transcription + push-to-talk, built on transcribe.cpp.

Run with:
    python main.py

Requires ffmpeg on PATH and at least one .gguf model in ./models.
"""

from chatter.app import run

if __name__ == "__main__":
    run()
