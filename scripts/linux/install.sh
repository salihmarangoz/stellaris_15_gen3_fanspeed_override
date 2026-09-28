#!/usr/bin/env bash
# Install or update Stellaris Fan Control on Ubuntu with one command:
#   sudo ./scripts/linux/install.sh [--yes]
# Run it from your normal user account; that user becomes the only non-root
# user allowed to command the root service. The installer offers to disable
# TUXEDO Control Center's daemon and then starts fan control in Automatic mode.
set -euo pipefail

ASSUME_YES=0
[ "${1:-}" = --yes ] && ASSUME_YES=1

APP_DIR=/opt/stellaris-fan-control
CONFIG_DIR=/etc/stellaris-fan-control
SERVICE=stellaris-fan-control
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi
TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
    echo "Run this installer via sudo from your normal user account." >&2
    exit 1
fi
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

BOARD="$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)"
SKU="$(cat /sys/class/dmi/id/product_sku 2>/dev/null || true)"
if [ "$BOARD" != GMxZGxx ] || [ "$SKU" != STELLARIS1XA03 ]; then
    echo "This laptop ($BOARD/$SKU) is not the validated Stellaris 15 Gen3; refusing." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "Python 3.11 or newer is required." >&2
    exit 1
fi
if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "Install python3-venv first: sudo apt install python3-venv" >&2
    exit 1
fi
command -v nvidia-smi >/dev/null || echo "Warning: nvidia-smi was not found; Automatic mode needs the NVIDIA driver."

if systemctl is-active --quiet "$SERVICE"; then
    echo "Stopping the running service for the update (both fans go to 100% meanwhile)..."
    systemctl stop "$SERVICE"
fi

echo "Copying the application to $APP_DIR..."
install -d -m 755 "$APP_DIR"
for item in backend frontend shared; do
    rm -rf "${APP_DIR:?}/$item"
    cp -r "$SOURCE_DIR/$item" "$APP_DIR/$item"
done
install -d -m 755 "$APP_DIR/assets"
install -m 644 "$SOURCE_DIR/assets/stellaris-fan-control.png" "$APP_DIR/assets/"
for file in stellaris15gen3_linux_service.py stellaris15gen3_frontend.py README.md THIRD_PARTY_NOTICES.md; do
    install -m 644 "$SOURCE_DIR/$file" "$APP_DIR/$file"
done
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR"
chmod -R u=rwX,go=rX "$APP_DIR"

echo "Preparing the GUI environment (PySide6)..."
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$APP_DIR/frontend/requirements.txt"
python3 -m compileall -q "$APP_DIR/backend" "$APP_DIR/shared" "$APP_DIR/frontend" "$APP_DIR"/*.py

echo "Allowing only $TARGET_USER (uid $TARGET_UID) and root to control the service..."
install -d -m 755 "$CONFIG_DIR"
printf '{\n  "allowed_uids": [%s]\n}\n' "$TARGET_UID" > "$CONFIG_DIR/config.json"
chmod 644 "$CONFIG_DIR/config.json"

install -m 644 "$SOURCE_DIR/scripts/linux/stellaris-fan-control.service" "/etc/systemd/system/$SERVICE.service"
install -m 755 "$SOURCE_DIR/scripts/linux/stellaris-fan-control-sleep" "/usr/lib/systemd/system-sleep/$SERVICE"
install -m 644 "$SOURCE_DIR/scripts/linux/stellaris-fan-control.desktop" "/usr/share/applications/$SERVICE.desktop"
# Create the user's autostart directory as that user so no parent becomes root-owned.
sudo -u "$TARGET_USER" mkdir -p "$TARGET_HOME/.config/autostart"
install -m 644 -o "$TARGET_USER" -g "$(id -g "$TARGET_USER")" \
    "$SOURCE_DIR/scripts/linux/stellaris-fan-control.desktop" "$TARGET_HOME/.config/autostart/$SERVICE.desktop"

systemctl daemon-reload
systemctl enable "$SERVICE.service" >/dev/null

# NVIDIA applies the cTGP offset and Dynamic Boost only while nvidia-powerd runs.
# Ubuntu's driver packages ship its unit only as documentation and no D-Bus
# policy, so set it up the way TUXEDO's driver packages do.
if [ -x /usr/bin/nvidia-powerd ] && ! systemctl is-active --quiet nvidia-powerd.service; then
    echo "Enabling nvidia-powerd so the GPU power limit and Dynamic Boost take effect..."
    systemctl unmask nvidia-powerd.service >/dev/null 2>&1 || true
    if ! systemctl cat nvidia-powerd.service >/dev/null 2>&1; then
        install -m 644 "$SOURCE_DIR/scripts/linux/nvidia-powerd.service" /etc/systemd/system/nvidia-powerd.service
    fi
    if ! grep -rqs 'nvidia.powerd.server' /etc/dbus-1/system.d /usr/share/dbus-1/system.d; then
        install -m 644 "$SOURCE_DIR/scripts/linux/nvidia-dbus.conf" /etc/dbus-1/system.d/nvidia-dbus.conf
        systemctl reload dbus || true
    fi
    systemctl daemon-reload
    systemctl enable --now nvidia-powerd.service || \
        echo "Warning: nvidia-powerd did not start; the GPU power limit will not take effect."
elif [ ! -x /usr/bin/nvidia-powerd ]; then
    echo "Warning: nvidia-powerd is not installed; the GPU power limit will not take effect."
fi

start_service=1
if systemctl is-active --quiet tccd 2>/dev/null || systemctl is-enabled --quiet tccd 2>/dev/null; then
    echo
    echo "TUXEDO Control Center's daemon (tccd) also controls the fans. The service"
    echo "refuses every fan write while tccd runs, so tccd must be disabled."
    answer=y
    if [ "$ASSUME_YES" -eq 0 ]; then
        read -r -p "Disable tccd now? [Y/n] " answer </dev/tty || answer=n
    fi
    case "${answer:-y}" in
        [Yy]*) systemctl disable --now tccd.service ;;
        *)
            start_service=0
            echo "tccd left running; fan control is installed but not started."
            ;;
    esac
fi

echo
if [ "$start_service" -eq 1 ]; then
    systemctl restart "$SERVICE"
    echo "Fan control is running in Automatic mode."
else
    echo "After removing or disabling tccd, start fan control with: sudo systemctl start $SERVICE"
fi
echo "Open 'Stellaris Fan Control' from the application menu; it also starts at every login."
