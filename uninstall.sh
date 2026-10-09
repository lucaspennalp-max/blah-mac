#!/bin/bash
# Removes Blah. Add --everything to also delete your settings, API key and history (~/.blah).
set -uo pipefail

pkill -f "[Pp]ython[0-9.]* -m blah( |$)" 2>/dev/null
pkill -f "Blah.app/Contents/MacOS/Blah" 2>/dev/null
rm -rf "/Applications/Blah.app" "$HOME/Applications/Blah.app"
[[ -d "$HOME/Library/Application Support/Blah" ]] && chmod -R u+w "$HOME/Library/Application Support/Blah"
rm -rf "$HOME/Library/Application Support/Blah"
rm -f "$HOME/Library/LaunchAgents/app.blah.login.plist"

if [[ "${1:-}" == "--everything" ]]; then
  rm -rf "$HOME/.blah"
  echo "✓ Blah and all of its data are removed."
else
  echo "✓ Blah is removed. Your settings and history are still in ~/.blah (run with --everything to delete them)."
fi
echo "  You can also remove Blah from System Settings › Privacy & Security (Microphone, Input Monitoring, Accessibility)."
