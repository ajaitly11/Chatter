#!/bin/bash
# Builds a real Chatter.app with a frozen Mach-O executable. The old bundle
# exec'd the venv's Python, which made macOS privacy panes identify the app as
# Python 3 instead of Chatter.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$PROJECT_DIR/Chatter.app"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "error: $VENV_PYTHON not found. Run: python3 -m venv venv && venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
fi

echo "Generating icon..."
QT_QPA_PLATFORM=offscreen "$VENV_PYTHON" "$SCRIPT_DIR/make_icon.py"

echo "Freezing Chatter executable..."
cd "$PROJECT_DIR"
"$VENV_PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name Chatter \
    --icon "$SCRIPT_DIR/icon.icns" \
    --add-data "chatter/style.qss:chatter" \
    --collect-all transcribe_cpp_native \
    --copy-metadata transcribe-cpp \
    --copy-metadata transcribe-cpp-native \
    main.py

rm -rf "$APP_DIR"
mv "$PROJECT_DIR/dist/Chatter.app" "$APP_DIR"

# Models are intentionally not copied into the app: a release should not
# bundle several gigabytes of GGUFs. A bundle-local symlink keeps the runtime
# lookup stable for a local checkout; CI builds get an empty models folder and
# can guide the user to download the recommended model after installation.
mkdir -p "$APP_DIR/Contents/Resources"
if [ -d "$PROJECT_DIR/models" ]; then
    ln -s "$PROJECT_DIR/models" "$APP_DIR/Contents/Resources/models"
else
    mkdir -p "$APP_DIR/Contents/Resources/models"
fi

PLIST="$APP_DIR/Contents/Info.plist"
plutil -replace CFBundleName -string "Chatter" "$PLIST"
plutil -replace CFBundleDisplayName -string "Chatter" "$PLIST"
plutil -replace CFBundleIdentifier -string "com.chatter.app" "$PLIST"
plutil -replace CFBundleExecutable -string "Chatter" "$PLIST"
plutil -replace CFBundleShortVersionString -string "1.0" "$PLIST"
plutil -replace CFBundleVersion -string "1" "$PLIST"
plutil -insert CFBundleSpokenName -string "Chatter" "$PLIST" 2>/dev/null || true
plutil -insert LSUIElement -bool false "$PLIST" 2>/dev/null || true
plutil -insert LSMultipleInstancesProhibited -bool true "$PLIST" 2>/dev/null || true
plutil -insert NSMicrophoneUsageDescription -string "Chatter needs microphone access for push-to-talk transcription. Audio and processing stay on this Mac." "$PLIST" 2>/dev/null || true
plutil -insert NSHighResolutionCapable -bool true "$PLIST" 2>/dev/null || true

# Give the TCC/privacy database a stable bundle identifier. PyInstaller's
# default ad-hoc identifier is the executable name, which makes permission
# entries less predictable after app moves or rebuilds.
codesign --force --deep --sign - --identifier com.chatter.app "$APP_DIR"

echo "Built $APP_DIR"
echo "Executable: $APP_DIR/Contents/MacOS/Chatter"
