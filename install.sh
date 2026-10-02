#!/bin/bash
# Installation script for Easier SSH Tunnel Manager
# Supports Debian/Ubuntu (apt), Fedora/RHEL (dnf), Arch (pacman) and openSUSE (zypper)

echo "Easier SSH Tunnel Manager - Installation Script"
echo "=============================================="
echo

# Check if running with sudo
if [ "$EUID" -ne 0 ]; then
    echo "This script needs to be run with sudo to install system-wide."
    echo "Usage: sudo ./install.sh"
    exit 1
fi

cd "$(dirname "$0")" || exit 1

# Install dependencies with the distribution's package manager
echo "Installing dependencies..."
if command -v apt-get &> /dev/null; then
    apt-get update
    apt-get install -y python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 iproute2 openssh-client
    # Debian 12+ and Ubuntu 24.04+ ship the Ayatana indicator; older releases only the original
    apt-get install -y gir1.2-ayatanaappindicator3-0.1 || apt-get install -y gir1.2-appindicator3-0.1
    TRAY_EXTENSION_PACKAGE="gnome-shell-extension-appindicator"
    PM_INSTALL="apt-get install -y"
elif command -v dnf &> /dev/null; then
    dnf install -y python3 python3-gobject python3-cairo gobject-introspection gtk3 \
        libayatana-appindicator-gtk3 iproute openssh-clients
    TRAY_EXTENSION_PACKAGE="gnome-shell-extension-appindicator"
    PM_INSTALL="dnf install -y"
elif command -v pacman &> /dev/null; then
    pacman -S --needed --noconfirm python python-gobject python-cairo gtk3 \
        libayatana-appindicator iproute2 openssh
    TRAY_EXTENSION_PACKAGE="gnome-shell-extension-appindicator"
    PM_INSTALL="pacman -S --needed --noconfirm"
elif command -v zypper &> /dev/null; then
    zypper --non-interactive install python3 python3-gobject python3-gobject-cairo \
        python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-AyatanaAppIndicator3-0_1 \
        iproute2 openssh-clients
    TRAY_EXTENSION_PACKAGE=""
    PM_INSTALL="zypper --non-interactive install"
else
    echo "No supported package manager found (apt, dnf, pacman, zypper)."
    echo "Install these yourself: Python 3, PyGObject with cairo support, GTK 3 introspection,"
    echo "AyatanaAppIndicator3 or AppIndicator3 introspection, iproute2 (ss) and the OpenSSH client."
    TRAY_EXTENSION_PACKAGE=""
    PM_INSTALL=""
fi

# Check that the Python bindings load
echo "Checking Python bindings..."
if python3 - <<'EOF'
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
import cairo
gi.require_foreign('cairo')
for name in ('AyatanaAppIndicator3', 'AppIndicator3'):
    try:
        gi.require_version(name, '0.1')
        break
    except ValueError:
        pass
else:
    print("No AppIndicator library found: the app will run without a tray icon.")
EOF
then
    echo "Python bindings OK."
else
    echo "WARNING: GTK 3 Python bindings do not load; the app will not start until they are installed."
fi

# Copy script to /usr/local/bin
echo "Installing application..."
install -m 755 easier_ssh_tunnel.py /usr/local/bin/easier_ssh_tunnel.py

# Copy icons
echo "Installing icons..."
mkdir -p /usr/local/share/easier-ssh-tunnel/icons
cp icons/*.png /usr/local/share/easier-ssh-tunnel/icons/

# Copy desktop entry
echo "Installing desktop entry..."
install -m 644 easier-ssh-tunnel.desktop /usr/share/applications/easier-ssh-tunnel.desktop

# Update desktop database
if command -v update-desktop-database &> /dev/null; then
    update-desktop-database /usr/share/applications/
fi

# GNOME shows tray icons only with an AppIndicator extension (Ubuntu ships one)
if command -v gnome-shell &> /dev/null \
    && [ ! -d /usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com ] \
    && [ ! -d /usr/share/gnome-shell/extensions/appindicatorsupport@rgcjonas.gmail.com ]; then
    echo
    echo "GNOME needs the 'AppIndicator and KStatusNotifierItem Support' extension for the tray icon."
    if [ -n "$TRAY_EXTENSION_PACKAGE" ]; then
        $PM_INSTALL "$TRAY_EXTENSION_PACKAGE" || echo "Could not install $TRAY_EXTENSION_PACKAGE."
    else
        echo "Install it from https://extensions.gnome.org/extension/615/appindicator-support/"
    fi
    echo "Enable it as your user, then log out and back in:"
    echo "  gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com"
    echo "Without a tray the app opens its window instead."
fi

# Optional autostart for the user who ran sudo
TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ] && [ -t 0 ]; then
    read -r -p "Start Easier SSH Tunnel Manager at login for $TARGET_USER? [y/N] " AUTOSTART
    if [[ "$AUTOSTART" =~ ^[Yy]$ ]]; then
        TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
        AUTOSTART_DIR="$TARGET_HOME/.config/autostart"
        install -d -o "$TARGET_USER" -g "$(id -gn "$TARGET_USER")" "$AUTOSTART_DIR"
        install -m 644 -o "$TARGET_USER" -g "$(id -gn "$TARGET_USER")" \
            easier-ssh-tunnel.desktop "$AUTOSTART_DIR/easier-ssh-tunnel.desktop"
        echo "Autostart entry installed in $AUTOSTART_DIR"
        echo "Tunnels are not started automatically; switch them on from the tray menu."
    fi
fi

echo
echo "Installation complete!"
echo "You can now launch 'Easier SSH Tunnel Manager' from your applications menu,"
echo "or run it from the terminal with: easier_ssh_tunnel.py"
echo
echo "The application will run in system tray mode by default."
echo "To run without the system tray indicator, use: easier_ssh_tunnel.py --no-indicator"
