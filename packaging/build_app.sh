#!/bin/bash
# Builds a real Chatter.app with a frozen Mach-O executable. The old bundle
# executed the development Python runtime, which made macOS privacy panes
# identify the app as a runtime instead of Chatter.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$PROJECT_DIR/Chatter.app"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"

# Releases pass the tag through CHATTER_VERSION. Local builds fall back to the
# nearest release tag so the app still has a useful version in Finder and
# System Settings. The internal build number is kept separate, as macOS uses
# it to distinguish two builds of the same public version.
VERSION="${CHATTER_VERSION:-}"
if [ -z "$VERSION" ] && [[ "${GITHUB_REF_NAME:-}" == v* ]]; then
    VERSION="${GITHUB_REF_NAME#v}"
fi
if [ -z "$VERSION" ]; then
    RELEASE_TAG="$(git -C "$PROJECT_DIR" describe --tags --match 'v[0-9]*' --exact-match 2>/dev/null || true)"
    if [ -z "$RELEASE_TAG" ]; then
        RELEASE_TAG="$(git -C "$PROJECT_DIR" describe --tags --match 'v[0-9]*' --abbrev=0 2>/dev/null || true)"
    fi
    VERSION="${RELEASE_TAG#v}"
fi
VERSION="${VERSION#v}"
if [ -z "$VERSION" ]; then
    VERSION="0.0.0"
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "error: release version must look like 1.2.3; got '$VERSION'" >&2
    exit 1
fi
BUILD_NUMBER="${CHATTER_BUILD_NUMBER:-${GITHUB_RUN_NUMBER:-1}}"
if [[ ! "$BUILD_NUMBER" =~ ^[0-9]+$ ]]; then
    echo "error: build number must be numeric; got '$BUILD_NUMBER'" >&2
    exit 1
fi

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
# bundle several gigabytes of GGUFs. Keep an empty, signed directory in the
# bundle for developer fallback; frozen builds use the writable per-user model
# directory under Application Support. An absolute symlink to the checkout
# would make strict macOS code-signature verification reject the bundle.
mkdir -p "$APP_DIR/Contents/Resources"
mkdir -p "$APP_DIR/Contents/Resources/models"

PLIST="$APP_DIR/Contents/Info.plist"
plutil -replace CFBundleName -string "Chatter" "$PLIST"
plutil -replace CFBundleDisplayName -string "Chatter" "$PLIST"
plutil -replace CFBundleIdentifier -string "com.chatter.app" "$PLIST"
plutil -replace CFBundleExecutable -string "Chatter" "$PLIST"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$BUILD_NUMBER" "$PLIST"
plutil -insert CFBundleSpokenName -string "Chatter" "$PLIST" 2>/dev/null || true
plutil -insert LSUIElement -bool false "$PLIST" 2>/dev/null || true
plutil -insert LSMultipleInstancesProhibited -bool true "$PLIST" 2>/dev/null || true
plutil -insert NSMicrophoneUsageDescription -string "Chatter needs microphone access for push-to-talk transcription. Audio and processing stay on this Mac." "$PLIST" 2>/dev/null || true
plutil -insert NSHighResolutionCapable -bool true "$PLIST" 2>/dev/null || true

# Give the TCC/privacy database a stable bundle identifier. PyInstaller's
# default ad-hoc identifier is the executable name, which makes permission
# entries less predictable after app moves or rebuilds.
#
# Sign with a local self-signed "Chatter Local Dev" certificate (see
# packaging/setup_dev_cert.sh) rather than ad-hoc (-s -). Ad-hoc signatures
# have no stable identity, so macOS ties Accessibility/Microphone/Input
# Monitoring grants to the exact binary hash — every rebuild silently
# invalidates permissions the user already granted, even though System
# Settings still shows the switch on. Signing with a persistent certificate
# gives TCC a stable designated requirement that survives rebuilds. Falls
# back to ad-hoc if the dev cert has not been created yet.
#
# CHATTER_CODESIGN_IDENTITY lets a caller (CI) point this at a different
# imported identity — e.g. "Chatter Release" — instead of the local dev one.
#
# Deliberately not `-v` here: that flag means "only show identities with a
# trusted chain", which a self-signed certificate never has regardless of
# whether it's perfectly usable for signing — trust only matters for
# verifying a signature later, not for creating one. `-v` made this check
# report "not found" even when the identity was imported and working fine.
CODESIGN_IDENTITY="${CHATTER_CODESIGN_IDENTITY:-Chatter Local Dev}"
if ! security find-identity -p codesigning | grep -q "$CODESIGN_IDENTITY"; then
    echo "warning: '$CODESIGN_IDENTITY' certificate not found; falling back to ad-hoc signing." >&2
    echo "         Run packaging/setup_dev_cert.sh once to make permission grants survive rebuilds." >&2
    CODESIGN_IDENTITY="-"
fi
codesign --force --deep --sign "$CODESIGN_IDENTITY" --identifier com.chatter.app "$APP_DIR"

echo "Built $APP_DIR"
echo "Version: $VERSION (build $BUILD_NUMBER)"
echo "Executable: $APP_DIR/Contents/MacOS/Chatter"
