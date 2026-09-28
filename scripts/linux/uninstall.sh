#!/usr/bin/env bash
# Remove Stellaris Fan Control. Stopping the service leaves both fans at 100%
# until the EC firmware or another fan controller takes over (e.g. a reboot).
# Fan backups and service settings in /var/lib/stellaris-fan-control are kept
# unless --purge is given.
set -euo pipefail

SERVICE=stellaris-fan-control
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

systemctl disable --now "$SERVICE.service" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE.service" \
      "/usr/lib/systemd/system-sleep/$SERVICE" \
      "/usr/share/applications/$SERVICE.desktop"
systemctl daemon-reload
rm -rf /opt/stellaris-fan-control /etc/stellaris-fan-control
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    TARGET_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    rm -f "$TARGET_HOME/.config/autostart/$SERVICE.desktop"
fi
if [ "${1:-}" = --purge ]; then
    rm -rf /var/lib/stellaris-fan-control
fi
echo "Removed $SERVICE. Both fans stay at 100% until the next reboot or another controller."
echo "nvidia-powerd is part of the NVIDIA driver and stays enabled; disable it with: sudo systemctl disable --now nvidia-powerd"
