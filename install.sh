#!/bin/bash
# Installs (or updates) Blah:
#   code + private Python environment  ->  ~/Library/Application Support/Blah
#   the app you open                   ->  /Applications/Blah.app (or ~/Applications if /Applications isn't writable)
# Your settings and history live in ~/.blah and are kept across updates.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/Blah"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "Blah runs on macOS only."; exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Blah needs Apple's free developer tools first. Run:"
  echo "    xcode-select --install"
  echo "then run ./install.sh again."
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install it with:  xcode-select --install"; exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is required (found $(python3 --version))."; exit 1
fi

if [[ -w /Applications ]]; then APPS="/Applications"; else APPS="$HOME/Applications"; fi
APP="$APPS/Blah.app"
mkdir -p "$APPS" "$SUPPORT"

echo "→ Closing Blah if it's running"
pkill -f "[Pp]ython[0-9.]* -m blah( |$)" 2>/dev/null || true
pkill -f "Blah.app/Contents/MacOS/Blah" 2>/dev/null || true
sleep 1

echo "→ Copying Blah into $SUPPORT"
[[ -d "$SUPPORT/src" ]] && chmod -R u+w "$SUPPORT/src"
rm -rf "$SUPPORT/src"
mkdir -p "$SUPPORT/src"
cp -R "$SRC/blah" "$SUPPORT/src/blah"
find "$SUPPORT/src" -name "__pycache__" -type d -prune -exec rm -rf {} +
chmod -R a-w "$SUPPORT/src"     # Blah's code is read-only once installed

if [[ ! -x "$SUPPORT/venv/bin/python" ]]; then
  echo "→ Creating a private Python environment"
  python3 -m venv "$SUPPORT/venv"
fi
echo "→ Installing dependencies (a few minutes the first time)"
"$SUPPORT/venv/bin/pip" install --quiet --upgrade pip
"$SUPPORT/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"

echo "→ Building $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# The bundle version stays fixed: macOS ties permissions to the exact app bundle, so changing it
# would make everyone re-allow Microphone, Input Monitoring and Accessibility on every update.
# Blah's real version is shown in Settings › Advanced.
VERSION="1.0.0"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Blah</string>
  <key>CFBundleDisplayName</key><string>Blah</string>
  <key>CFBundleIdentifier</key><string>app.blah.Blah</string>
  <key>CFBundleExecutable</key><string>Blah</string>
  <key>CFBundleIconFile</key><string>Blah</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Blah listens only while you hold your shortcut, and turns your voice into text on this Mac.</string>
</dict>
</plist>
PLIST

# ---- the app's main executable ------------------------------------------------
# A small compiled launcher runs Python inside Blah's own process, so macOS treats
# Blah as one app for the Dock and for permissions (Accessibility, Input Monitoring).
PY="$SUPPORT/venv/bin/python"
PY_HOME="$("$PY" -c 'import sys; print(sys.base_prefix)')"
PY_SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PY_CONFIG="$("$PY" -c 'import os, sysconfig as s; print(os.path.join(s.get_config_var("BINDIR") or "", "python" + s.get_config_var("VERSION") + "-config"))')"
PY_LIBDIR="$("$PY" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") or "")')"
# Extra link flags: Python's own, plus loading all of a static libpython so compiled
# packages (numpy and friends) find the functions they need.
PY_EXTRA="$("$PY" - <<'PYFLAGS'
import os, sysconfig as s
extra = s.get_config_var("LINKFORSHARED") or ""
lib = s.get_config_var("LDLIBRARY") or ""
if lib.endswith(".a"):
    for d in (s.get_config_var("LIBPL"), s.get_config_var("LIBDIR")):
        path = os.path.join(d or "", lib)
        if os.path.exists(path):
            extra += " -Wl,-force_load," + path
            break
print(extra)
PYFLAGS
)"
BUILD="$SUPPORT/build"          # fixed path: the same input builds the same app every time
rm -rf "$BUILD"; mkdir -p "$BUILD"
# Fill in the launcher's paths, escaped for C (works with spaces, quotes or any other character).
"$PY" - "$SRC/launcher/blah_launcher.c" "$BUILD/launcher.c" "$PY_HOME" "$PY_SITE" "$SUPPORT/src" "$PY" <<'FILL'
import json, sys
template, out, home, site, src, python = sys.argv[1:]
c = lambda value: json.dumps(value, ensure_ascii=False)[1:-1]
text = open(template, encoding="utf-8").read()
for key, value in (("@PYTHON_HOME@", home), ("@SITE_PACKAGES@", site), ("@SRC@", src), ("@PYTHON@", python)):
    text = text.replace(key, c(value))
open(out, "w", encoding="utf-8").write(text)
FILL

BUILT=0
if command -v clang >/dev/null 2>&1 && [[ -x "$PY_CONFIG" ]]; then
  # shellcheck disable=SC2046
  if clang -O2 "$BUILD/launcher.c" -o "$APP/Contents/MacOS/Blah" \
       $("$PY_CONFIG" --embed --cflags --ldflags) $PY_EXTRA -Wl,-rpath,"$PY_LIBDIR" -g0 2>"$BUILD/err.txt"; then
    BUILT=1
    rm -rf "$APP/Contents/MacOS/"*.dSYM
    strip -x "$APP/Contents/MacOS/Blah" 2>/dev/null || true
  fi
fi

if [[ "$BUILT" != 1 ]]; then
  echo "  ! Couldn't compile Blah's launcher, using a simple one instead."
  echo "    Pasting into other apps may not work. Install Apple's developer tools with:"
  echo "      xcode-select --install"
  echo "    then run this installer again."
  [[ -s "$BUILD/err.txt" ]] && sed 's/^/    /' "$BUILD/err.txt" | head -5
  cat > "$APP/Contents/MacOS/Blah" <<'LAUNCHER'
#!/bin/bash
SUPPORT="$HOME/Library/Application Support/Blah"
export BLAH_APP="$(cd "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="$SUPPORT/src"
mkdir -p "$HOME/.blah"
cd "$SUPPORT/src"
exec "$SUPPORT/venv/bin/python" -m blah "$@" >>"$HOME/.blah/blah.log" 2>&1
LAUNCHER
fi
chmod +x "$APP/Contents/MacOS/Blah"
rm -rf "$BUILD"

if command -v iconutil >/dev/null 2>&1; then
  iconutil -c icns "$SRC/assets/Blah.iconset" -o "$APP/Contents/Resources/Blah.icns" || true
fi

# A stable local signature: as long as the launcher doesn't change, macOS keeps
# remembering the permissions you gave Blah across updates.
codesign --force --sign - --identifier app.blah.Blah "$APP" >/dev/null 2>&1 || true

# If Blah's identity changed (first install of this launcher, or a new Python),
# clear old permission entries so the switches in System Settings aren't stale.
FINGERPRINT="$(cat "$APP/Contents/MacOS/Blah" "$APP/Contents/Info.plist" | shasum -a 256 | cut -d' ' -f1)"
if [[ "$(cat "$SUPPORT/fingerprint" 2>/dev/null)" != "$FINGERPRINT" ]]; then
  if [[ -f "$SUPPORT/fingerprint" || "$BUILT" == 1 ]]; then
    tccutil reset Accessibility app.blah.Blah >/dev/null 2>&1 || true
    tccutil reset ListenEvent app.blah.Blah >/dev/null 2>&1 || true
    tccutil reset Microphone app.blah.Blah >/dev/null 2>&1 || true
    echo "  Blah changed enough that macOS needs you to allow Microphone, Accessibility and Input Monitoring again."
    echo "  Use the Allow buttons in Blah's window."
  fi
  echo "$FINGERPRINT" > "$SUPPORT/fingerprint"
fi
touch "$APP"

echo
echo "✓ Blah is installed in $APPS. Opening it now…"
echo "  The first dictation downloads the speech model (about 500 MB)."
open "$APP"
