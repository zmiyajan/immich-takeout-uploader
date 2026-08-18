#!/bin/bash
# ينشر الأداة على سيرفر Immich عبر SSH.
# الاستخدام:  ./deploy.sh user@server-ip
set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "الاستخدام: $0 user@server-ip"
  echo "مثال   : $0 pi@192.168.4.30"
  exit 1
fi

SSH="ssh -o ConnectTimeout=10 $TARGET"
echo "→ الاتصال بـ $TARGET ..."

# ---------- ١. فحص السيرفر ----------
INFO=$($SSH 'echo "ARCH=$(uname -m)"; echo "PY=$(command -v python3 || echo none)"; echo "CURL=$(command -v curl || echo none)"; echo "HOST=$(hostname)"')
echo "$INFO" | sed 's/^/   /'
ARCH=$(echo "$INFO" | grep '^ARCH=' | cut -d= -f2)
PY=$(echo "$INFO"   | grep '^PY='   | cut -d= -f2)
CURL=$(echo "$INFO" | grep '^CURL=' | cut -d= -f2)

[ "$PY" = "none" ]   && { echo "✗ python3 مو مثبت على السيرفر. ثبّته: sudo apt install python3"; exit 1; }
[ "$CURL" = "none" ] && { echo "✗ curl مو مثبت على السيرفر. ثبّته: sudo apt install curl"; exit 1; }

case "$ARCH" in
  x86_64|amd64)   ASSET="immich-go_Linux_x86_64.tar.gz" ;;
  aarch64|arm64)  ASSET="immich-go_Linux_arm64.tar.gz" ;;
  *) echo "✗ معمارية غير مدعومة: $ARCH"; exit 1 ;;
esac
echo "   الحزمة المناسبة: $ASSET"

# ---------- ٢. المساحة الفاضية ----------
echo ""
echo "→ المساحة على السيرفر:"
$SSH 'df -h | grep -Ev "^(tmpfs|devtmpfs|udev|overlay|none)" | head -12' | sed 's/^/   /'

# ---------- ٣. تثبيت immich-go ----------
echo ""
echo "→ تثبيت immich-go ..."
$SSH "bash -s" <<REMOTE
set -e
mkdir -p ~/.local/bin ~/immich-uploader
if command -v immich-go >/dev/null 2>&1 || [ -x ~/.local/bin/immich-go ]; then
  echo "   موجود مسبقاً: \$(~/.local/bin/immich-go --version 2>/dev/null || immich-go --version)"
else
  URL=\$(curl -sL https://api.github.com/repos/simulot/immich-go/releases/latest \
        | grep -o "https://[^\"]*$ASSET" | head -1)
  echo "   تنزيل: \$URL"
  curl -sL "\$URL" -o /tmp/ig.tar.gz
  tar -xzf /tmp/ig.tar.gz -C /tmp immich-go
  mv /tmp/immich-go ~/.local/bin/immich-go
  chmod +x ~/.local/bin/immich-go
  rm -f /tmp/ig.tar.gz
  echo "   تم: \$(~/.local/bin/immich-go --version)"
fi
REMOTE

# ---------- ٤. نسخ الواجهة ----------
echo ""
echo "→ نسخ الواجهة ..."
scp -q "$(dirname "$0")/app.py" "$TARGET:~/immich-uploader/app.py"
echo "   تم النسخ إلى ~/immich-uploader/app.py"

# ---------- ٥. تشغيلها بحيث تستمر بعد قطع SSH ----------
echo ""
echo "→ تشغيل الواجهة (تستمر شغّالة حتى لو قفلت SSH) ..."
$SSH 'bash -s' <<'REMOTE'
cd ~/immich-uploader
# نوقف أي نسخة قديمة على نفس المنفذ
if command -v lsof >/dev/null 2>&1; then
  lsof -ti :8765 2>/dev/null | xargs -r kill -9 2>/dev/null || true
else
  pkill -9 -f "python3 app.py" 2>/dev/null || true
fi
sleep 1
setsid nohup python3 app.py > ~/immich-uploader/server.log 2>&1 < /dev/null &
sleep 2
if curl -s --max-time 5 -o /dev/null http://127.0.0.1:8765/env; then
  echo "   ✓ الواجهة شغّالة على المنفذ 8765"
else
  echo "   ✗ ما اشتغلت — شوف ~/immich-uploader/server.log"
  tail -20 ~/immich-uploader/server.log
  exit 1
fi
REMOTE

# ---------- ٦. تجهيز نفق SSH ----------
cat > "$(dirname "$0")/tunnel.command" <<TUNNEL
#!/bin/bash
# يفتح نفق SSH للسيرفر ويفتح الواجهة في المتصفح
echo "فتح النفق إلى $TARGET ..."
pkill -f "ssh -f -N -L 8765" 2>/dev/null
ssh -f -N -L 8765:127.0.0.1:8765 $TARGET
sleep 1
open http://127.0.0.1:8765/
echo "الواجهة مفتوحة. لإغلاق النفق:  pkill -f 'ssh -f -N -L 8765'"
TUNNEL
chmod +x "$(dirname "$0")/tunnel.command"

echo ""
echo "════════════════════════════════════════════════"
echo " تم النشر بنجاح"
echo "════════════════════════════════════════════════"
echo " افتح الواجهة بالضغط مرتين على:"
echo "   ~/immich-uploader/tunnel.command"
echo ""
echo " أو يدوياً:"
echo "   ssh -N -L 8765:127.0.0.1:8765 $TARGET"
echo "   ثم افتح http://127.0.0.1:8765"
echo "════════════════════════════════════════════════"
