#!/bin/bash
# Install desktop file and icons for X11/Wayland desktop environments
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ASSETS="$SCRIPT_DIR/assets"

# Detect install prefix
if [ "$(id -u)" -eq 0 ]; then
    PREFIX="/usr/share"
else
    PREFIX="$HOME/.local/share"
fi

echo "Installing housekeeper desktop integration to $PREFIX ..."

# Install icons
for sz in 16 32 48 64 128 256; do
    ICON_DIR="$PREFIX/icons/hicolor/${sz}x${sz}/apps"
    mkdir -p "$ICON_DIR"
    cp "$ASSETS/housekeeper-${sz}.png" "$ICON_DIR/housekeeper.png"
    echo "  icon ${sz}x${sz} -> $ICON_DIR/housekeeper.png"
done

# Install SVG (scalable)
SVG_DIR="$PREFIX/icons/hicolor/scalable/apps"
mkdir -p "$SVG_DIR"
cp "$ASSETS/housekeeper.svg" "$SVG_DIR/housekeeper.svg"
echo "  icon scalable -> $SVG_DIR/housekeeper.svg"

# Install .desktop file
DESKTOP_DIR="$PREFIX/applications"
mkdir -p "$DESKTOP_DIR"
cp "$ASSETS/housekeeper.desktop" "$DESKTOP_DIR/housekeeper.desktop"
echo "  desktop -> $DESKTOP_DIR/housekeeper.desktop"

# Update icon cache
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f "$PREFIX/icons/hicolor/" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

echo "Done. You may need to log out and back in for the icon to appear."
