#!/bin/zsh
# Build "Flight Deck.saver" and the `flight-deck` command and install both.
# Run through the skill (`fd.py install`), which also installs and starts the feeder; `fd.py uninstall` removes it all.
# The saver draws natively (clock, agents, limits) from /lite.json; the full monitor page is
# web/index.html, opened with the `flight-deck` command. Page changes need no rebuild.
set -euo pipefail
cd "$(dirname "$0")"

NAME="Flight Deck"
DEST="$HOME/Library/Screen Savers/$NAME.saver"

B="build/$NAME.saver/Contents"
rm -rf build
mkdir -p "$B/MacOS" "$B/Resources"

echo "Compiling…"
swiftc -O -parse-as-library -module-name FlightDeck saver/FlightDeckView.swift \
    -emit-library -Xlinker -bundle -o "$B/MacOS/FlightDeck" \
    -framework ScreenSaver -framework Network
cp saver/Info.plist "$B/"
codesign --force --sign - "build/$NAME.saver"

# the `flight-deck` command (open / focus / close the page in its own window)
mkdir -p "$HOME/.local/bin"
swiftc -O launcher.swift -o "$HOME/.local/bin/flight-deck"

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
ditto "build/$NAME.saver" "$DEST"
echo "Installed: $DEST"

echo "Pick it in System Settings ▸ Wallpaper ▸ Screen Saver ▸ Other ▸ Flight Deck."
