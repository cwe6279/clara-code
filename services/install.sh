#!/bin/bash
# Install Clara's user services: the relay, the voice host and (with --speaker) the USB speaker player.
#   services/install.sh [--speaker]
# Then: systemctl --user status clara-host, journalctl --user -u clara-relay -f
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
UNITS=~/.config/systemd/user; mkdir -p "$UNITS" ~/.config/clara-code
names="clara-relay clara-host"; [ "$1" = "--speaker" ] && names="$names clara-speaker"
for n in $names; do sed "s#@REPO@#$REPO#g" "$REPO/services/$n.service" > "$UNITS/$n.service"; done
if [ ! -f ~/.config/clara-code/host.env ]; then
  cat > ~/.config/clara-code/host.env <<'ENV'
# Clara's voice host options (voice_loop.py flags). Restart after a change: systemctl --user restart clara-host
CLARA_ARGS=--face clara --satellite --no-window --stt whisper --tts piper --agent-url http://127.0.0.1:8032
ENV
fi
systemctl --user daemon-reload
systemctl --user enable $names >/dev/null
echo "Installed: $names. Start them with: systemctl --user start $names"
loginctl show-user "$USER" -p Linger | grep -q yes || echo "To keep them running after you log out: loginctl enable-linger $USER"
