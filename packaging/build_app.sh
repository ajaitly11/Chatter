#!/bin/bash
# Builds Chatter.app: a thin wrapper that execs this project's venv Python.
# Not a frozen/self-contained bundle — it references this checkout's venv
# and main.py by absolute path, so it only runs on this machine/checkout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$PROJECT_DIR/Chatter.app"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python3"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "error: $VENV_PYTHON not found. Run: python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

echo "Generating icon..."
"$VENV_PYTHON" "$SCRIPT_DIR/make_icon.py"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cp "$SCRIPT_DIR/icon.icns" "$APP_DIR/Contents/Resources/icon.icns"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Chatter</string>
    <key>CFBundleDisplayName</key>
    <string>Chatter</string>
    <key>CFBundleIdentifier</key>
    <string>com.chatter.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>Chatter</string>
    <key>CFBundleIconFile</key>
    <string>icon.icns</string>
    <key>LSUIElement</key>
    <false/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Chatter needs microphone access for push-to-talk transcription.</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cat > "$APP_DIR/Contents/MacOS/Chatter" <<LAUNCHER
#!/bin/bash
cd "$PROJECT_DIR"
exec "$VENV_PYTHON" "$PROJECT_DIR/main.py"
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/Chatter"

echo "Built $APP_DIR"
