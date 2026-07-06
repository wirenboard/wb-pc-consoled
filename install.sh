#!/usr/bin/env bash
# Install wb-pc-console daemon + client as a user systemd service.
# Usage: ./install.sh [INSTANCE ...]   e.g. ./install.sh ALEPBSP3 AADU6HJN
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
bin="$HOME/.local/bin"
unitdir="$HOME/.config/systemd/user"

mkdir -p "$bin" "$unitdir"
chmod +x "$here/wb-pc-consoled" "$here/wb-pc-console"
ln -sf "$here/wb-pc-consoled" "$bin/wb-pc-consoled"
ln -sf "$here/wb-pc-console" "$bin/wb-pc-console"
cp "$here/wb-pc-console@.service" "$unitdir/"
systemctl --user daemon-reload

# survive logout
if [ "$(loginctl show-user "$USER" -P Linger 2>/dev/null)" != "yes" ]; then
    loginctl enable-linger "$USER" \
        || echo "WARN: enable-linger failed; run manually: loginctl enable-linger $USER"
fi

# ModemManager ignore rule needs root; install if passwordless sudo is around
if [ ! -f /etc/udev/rules.d/99-wb-pc-console.rules ]; then
    if sudo -n true 2>/dev/null; then
        sudo -n cp "$here/99-wb-pc-console.rules" /etc/udev/rules.d/ \
            && sudo -n udevadm control --reload || true
    else
        echo "NOTE: to keep ModemManager off the consoles, run once:"
        echo "  sudo cp $here/99-wb-pc-console.rules /etc/udev/rules.d/ && sudo udevadm control --reload"
    fi
fi

for inst in "$@"; do
    systemctl --user enable --now "wb-pc-console@$inst"
    echo "enabled wb-pc-console@$inst"
done

echo "OK. Try: wb-pc-console list"
