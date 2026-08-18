#!/bin/bash
# Install this tool on an Immich server over SSH.
#
# Uploading over localhost instead of the network is dramatically faster, so if
# the archives can reach the server, running the importer there is worth it.
#
#   ./deploy.sh user@host
#
# The app is started detached so it keeps running after the SSH session ends,
# and stays bound to loopback; reach it through the tunnel this script writes.

set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: $0 user@host"
  echo "example: $0 pi@192.0.2.10"
  exit 1
fi

SSH="ssh -o ConnectTimeout=10 $TARGET"
echo "-> connecting to $TARGET"

# --- 1. inspect the server -------------------------------------------------
INFO=$($SSH 'echo "ARCH=$(uname -m)"; echo "PY=$(command -v python3 || echo none)"; echo "CURL=$(command -v curl || echo none)"')
echo "$INFO" | sed 's/^/   /'
ARCH=$(echo "$INFO" | grep '^ARCH=' | cut -d= -f2)
PY=$(echo   "$INFO" | grep '^PY='   | cut -d= -f2)
CURL=$(echo "$INFO" | grep '^CURL=' | cut -d= -f2)

[ "$PY"   = "none" ] && { echo "error: python3 is not installed on the server"; exit 1; }
[ "$CURL" = "none" ] && { echo "error: curl is not installed on the server"; exit 1; }

case "$ARCH" in
  x86_64|amd64)  ASSET="immich-go_Linux_x86_64.tar.gz" ;;
  aarch64|arm64) ASSET="immich-go_Linux_arm64.tar.gz" ;;
  *) echo "error: unsupported architecture: $ARCH"; exit 1 ;;
esac
echo "   release asset: $ASSET"

# --- 2. show free space, since the archives have to fit ---------------------
echo ""
echo "-> disk space on the server"
$SSH 'df -h | grep -Ev "^(tmpfs|devtmpfs|udev|overlay|none)" | head -12' | sed 's/^/   /'

# --- 3. install immich-go ---------------------------------------------------
echo ""
echo "-> installing immich-go"
$SSH "bash -s" <<REMOTE
set -e
mkdir -p ~/.local/bin ~/immich-uploader
if [ -x ~/.local/bin/immich-go ] || command -v immich-go >/dev/null 2>&1; then
  echo "   already installed: \$(~/.local/bin/immich-go --version 2>/dev/null || immich-go --version)"
else
  URL=\$(curl -sL https://api.github.com/repos/simulot/immich-go/releases/latest \
        | grep -o "https://[^\"]*$ASSET" | head -1)
  echo "   downloading \$URL"
  curl -sL "\$URL" -o /tmp/immich-go.tar.gz
  tar -xzf /tmp/immich-go.tar.gz -C /tmp immich-go
  mv /tmp/immich-go ~/.local/bin/immich-go
  chmod +x ~/.local/bin/immich-go
  rm -f /tmp/immich-go.tar.gz
  echo "   installed: \$(~/.local/bin/immich-go --version)"
fi
REMOTE

# --- 4. copy the app --------------------------------------------------------
echo ""
echo "-> copying app.py"
scp -q "$(dirname "$0")/app.py" "$TARGET:~/immich-uploader/app.py"
echo "   copied to ~/immich-uploader/app.py"

# --- 5. start it so it survives the SSH session ending ----------------------
echo ""
echo "-> starting the interface"
$SSH 'bash -s' <<'REMOTE'
cd ~/immich-uploader
if command -v lsof >/dev/null 2>&1; then
  lsof -ti :8765 2>/dev/null | xargs -r kill -9 2>/dev/null || true
else
  pkill -9 -f "python3 app.py" 2>/dev/null || true
fi
sleep 1
setsid nohup python3 app.py > ~/immich-uploader/server.log 2>&1 < /dev/null &
sleep 2
if curl -s --max-time 5 -o /dev/null http://127.0.0.1:8765/env; then
  echo "   running on port 8765"
else
  echo "   failed to start; last lines of ~/immich-uploader/server.log:"
  tail -20 ~/immich-uploader/server.log
  exit 1
fi
REMOTE

# --- 6. write a tunnel launcher --------------------------------------------
cat > "$(dirname "$0")/tunnel.command" <<TUNNEL
#!/bin/bash
# Opens an SSH tunnel to the server and launches the interface locally.
# The server stays bound to loopback, so the API key never crosses the network.
echo "opening tunnel to $TARGET"
pkill -f "ssh -f -N -L 8765" 2>/dev/null
ssh -f -N -L 8765:127.0.0.1:8765 $TARGET
sleep 1
command -v open >/dev/null && open http://127.0.0.1:8765/ || echo "open http://127.0.0.1:8765/"
echo "to close the tunnel:  pkill -f 'ssh -f -N -L 8765'"
TUNNEL
chmod +x "$(dirname "$0")/tunnel.command"

echo ""
echo "deployed."
echo "  open the interface:  ./tunnel.command"
echo "  or manually:         ssh -N -L 8765:127.0.0.1:8765 $TARGET"
echo "                       then browse to http://127.0.0.1:8765"
