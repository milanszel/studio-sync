#!/bin/bash
# Double click to build Studio Sync.app, install it in Applications,
# install the FL Studio script, and make a .dmg to give to other Macs.
cd "$(dirname "$0")" || exit 1

# On GitHub Actions (CI is set) nothing is installed on the build machine
# and there is nobody to press Return.
pause_end() { [ -n "$CI" ] || read -r -p 'Press Return to close.'; }

fail() {
  echo
  echo "Build failed: $1"
  pause_end
  exit 1
}

if ! command -v python3.12 >/dev/null; then
  echo 'Building needs Python 3.12. Install the macOS universal2 installer from:'
  echo 'https://www.python.org/downloads/release/python-31210/'
  fail 'Python 3.12 not found.'
fi

echo '1/6  Preparing the build environment (the first build downloads about 150 MB)…'
[ -x .venv-build/bin/python ] || python3.12 -m venv .venv-build || fail 'could not create the build environment.'
.venv-build/bin/python -m pip install --quiet --only-binary=:all: --no-binary=proxy-tools \
  -r requirements.txt -r packaging/build-requirements.txt || fail 'could not install dependencies (see above).'
VERSION=$(.venv-build/bin/python packaging/version.py) || fail 'could not read the version.'

echo "2/6  Making the app icon…"
rm -rf build/icon.iconset && mkdir -p build/icon.iconset
for size in 16 32 128 256 512; do
  sips -z $size $size packaging/icon.png --out "build/icon.iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z $double $double packaging/icon.png --out "build/icon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns build/icon.iconset -o build/icon.icns || fail 'could not make the icon.'

echo "3/6  Building Studio Sync $VERSION…"
rm -rf "dist/Studio Sync.app" "dist/Studio Sync"
.venv-build/bin/python -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath dist --workpath build/pyinstaller packaging/studio_sync.spec || fail 'PyInstaller failed (see above).'
APP="dist/Studio Sync.app"

echo '4/6  Checking the built app…'
rm -f build/self-test.txt
"$APP/Contents/MacOS/Studio Sync" --self-test "$PWD/build/self-test.txt"
if ! grep -q '^OK' build/self-test.txt 2>/dev/null; then
  cat build/self-test.txt 2>/dev/null
  fail 'the built app is missing something (listed above).'
fi
# Ad hoc signature so Apple Silicon runs it and permissions stick to the app.
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || fail 'could not sign the app.'

if [ -n "$CI" ]; then
  echo '5/6  Skipping install on the build server.'
else
echo '5/6  Installing…'
osascript -e 'tell application "Studio Sync" to quit' >/dev/null 2>&1
pkill -f '/Studio Sync.app/Contents/MacOS/Studio Sync' >/dev/null 2>&1
sleep 1
rm -rf "/Applications/Studio Sync.app"
ditto "$APP" "/Applications/Studio Sync.app" || fail 'could not copy the app to Applications.'
FL_USER="$HOME/Documents/Image-Line/FL Studio"
if [ -d "$FL_USER" ]; then
  mkdir -p "$FL_USER/Settings/Hardware/Studio-Sync"
  cp FL-Script/Studio-Sync/device_studio_sync.py "$FL_USER/Settings/Hardware/Studio-Sync/" \
    && echo "     FL script installed in $FL_USER/Settings/Hardware/Studio-Sync"
else
  echo '     FL user data folder not found here. Use Settings > Install in the app.'
fi
fi

echo '6/6  Making the installer disk image…'
ARCH=$(uname -m)
DMG="dist/Studio-Sync-$VERSION-Mac-$ARCH.dmg"
rm -rf build/dmg "$DMG" && mkdir -p build/dmg
ditto "$APP" "build/dmg/Studio Sync.app"
ln -s /Applications build/dmg/Applications
made=''
for attempt in 1 2 3; do
  # hdiutil is sometimes briefly busy; try again before giving up.
  if hdiutil create -quiet -volname "Studio Sync $VERSION" -srcfolder build/dmg -ov -format UDZO "$DMG"; then
    made=1
    break
  fi
  sleep 3
done
[ -n "$made" ] || fail 'could not make the disk image.'

echo
echo "Installer for Macs ($ARCH): $DMG"
if [ -z "$CI" ]; then
  echo "Studio Sync $VERSION is in Applications."
  echo 'In FL, click Update MIDI scripts (Options > MIDI settings) if FL was open.'
  open -R "$DMG"
  open "/Applications/Studio Sync.app"
fi
pause_end
