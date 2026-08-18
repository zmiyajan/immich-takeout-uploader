#!/bin/sh
# Installer for the Immich Takeout Uploader.
#
#   curl -fsSL https://raw.githubusercontent.com/zmiyajan/immich-takeout-uploader/main/install.sh | sh
#
# It downloads one Python file, drops a launcher beside it, and starts the
# interface. immich-go itself is fetched later from inside the app, which picks
# the build matching this machine.
#
# Piping a script from the internet into a shell means trusting it sight
# unseen. The two-line alternative in the README does the same thing and lets
# you read the file first.

set -eu

REPO="zmiyajan/immich-takeout-uploader"
DIR="${IG_DIR:-$HOME/immich-takeout-uploader}"
SRC="https://raw.githubusercontent.com/$REPO/main/app.py"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required and was not found."
  echo "  macOS : xcode-select --install"
  echo "  Debian: sudo apt install python3"
  echo "  Fedora: sudo dnf install python3"
  exit 1
fi

echo "-> installing into $DIR"
mkdir -p "$DIR"
curl -fsSL "$SRC" -o "$DIR/app.py"

# Downloading with curl avoids the quarantine flag a browser would attach on
# macOS, which is what makes Gatekeeper kill an unsigned binary on launch.
cat > "$DIR/start.command" <<'LAUNCHER'
#!/bin/bash
cd "$(dirname "$0")"
exec python3 app.py
LAUNCHER
chmod +x "$DIR/start.command"

echo "-> installed"
echo "   run again later with:  python3 $DIR/app.py"
if [ -z "${IG_NO_START:-}" ]; then
  echo "-> starting"
  cd "$DIR"
  exec python3 app.py
fi
