#!/bin/sh
# Install only when deliberately invoked by the administrator. DESTDIR is for
# nonprivileged packaging validation; it never enables the installed endpoint.
set -eu
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGING_ROOT=${DESTDIR:-}
if [ -z "$STAGING_ROOT" ] && [ "$(id -u)" != 0 ]; then
    echo 'Install using sudo ./install-helper.sh after reviewing docs/SAFETY.md.' >&2
    exit 1
fi
if [ ! -f "$PACKAGE_DIR/drivepak-manager-helper.pyz" ]; then
    echo 'Missing helper zipapp. Use the built Linux distribution.' >&2
    exit 1
fi
install -d -m 755 "$STAGING_ROOT/usr/local/libexec" "$STAGING_ROOT/usr/share/polkit-1/actions"
install -m 755 "$PACKAGE_DIR/drivepak-manager-helper" "$STAGING_ROOT/usr/local/libexec/drivepak-manager-helper"
install -m 644 "$PACKAGE_DIR/drivepak-manager-helper.pyz" "$STAGING_ROOT/usr/local/libexec/drivepak-manager-helper.pyz"
install -m 644 "$PACKAGE_DIR/org.drivepak.manager.policy" "$STAGING_ROOT/usr/share/polkit-1/actions/org.drivepak.manager.policy"
if [ -z "$STAGING_ROOT" ]; then
    chown root:root /usr/local/libexec/drivepak-manager-helper /usr/local/libexec/drivepak-manager-helper.pyz /usr/share/polkit-1/actions/org.drivepak.manager.policy
    echo 'Installed constrained helper. Launch the desktop as your normal user; polkit authenticates each operation.'
else
    echo "Staged helper under $STAGING_ROOT; no system configuration was changed."
fi
