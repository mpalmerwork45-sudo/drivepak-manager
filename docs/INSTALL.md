# Linux installation and package contents

The source installation in README.md is the development route. Pinned direct
and transitive dependencies are in `requirements.lock`; the runtime-only
resolution is `requirements-runtime.lock`. Python 3.11+ is required. Ordinary
image operations are portable Python; real card access is Linux-only.

## Build and install the Linux distribution

```bash
./tools/build_linux.sh
tar -xzf dist/drivepak-manager-1.0.4-linux.tar.gz -C /tmp
cd /tmp/drivepak-manager-1.0.4-linux
sha256sum -c SHA256SUMS
./install-user.sh
```

Run `~/.local/bin/drivepak-manager` or the **DrivePak Manager** Applications menu
entry. `~/.local/bin/drivepak` launches the CLI. The default application location
is `~/.local/share/drivepak-manager/venv`. `DRIVEPAK_PREFIX=/a/path` changes that
private virtualenv location; desktop/bin entries still belong to the current
user. Existing user launchers of those exact names are updated by this explicit
installation. No services or device permissions are changed by the user installer.

For an offline install, download the pinned runtime wheels on a matching online
machine and make them available locally:

```bash
python3 -m pip download -r requirements-runtime.lock -d wheelhouse
PIP_NO_INDEX=1 PIP_FIND_LINKS=/absolute/path/wheelhouse ./install-user.sh
```

The app wheel, source archive and Linux tarball are under `dist/`; all build
products are ignored by git. The tarball contains a wheel, desktop entry, SVG
icon, helper zipapp/wrapper/policy, installers, licenses and documentation. Qt is
not embedded in the application wheel/helper; runtime wheels are installed
separately. The helper zipapp contains six fixed, standard-library-only modules.
No research downloads, games, firmware, backups or user images are packaged.

The source checkout can also install its launcher manually using the template
in `packaging/drivepak-manager.desktop`; the distribution installer replaces
`Exec` with the absolute private virtualenv entrypoint and safely quotes paths.

## Install the card helper deliberately

Review `docs/SAFETY.md`, `drivepak-manager-helper`, the zipapp contents and
`org.drivepak.manager.policy`. Then, from the extracted distribution:

```bash
sudo ./install-helper.sh
```

Installed files:

| Destination | Purpose |
|---|---|
| /usr/local/libexec/drivepak-manager-helper | Fixed wrapper invoking `/usr/bin/python3 -I` |
| /usr/local/libexec/drivepak-manager-helper.pyz | Root-owned, stdlib-only validated device endpoint |
| /usr/share/polkit-1/actions/org.drivepak.manager.policy | Active-session administrator authentication |

Direct writes introduced in 1.0.3 require updating an older installed helper:
run the current package's `sudo ./install-helper.sh` after any active card
operation has finished. If the app says the helper needs updating, no card was
opened by that attempted direct operation. You do not need to reinstall the
helper after each write.

The system Python must be 3.11+ as well. The wrapper, zipapp and every ancestor
directory must be root-owned and not group/world writable. The GUI checks that
before invoking pkexec. No user-supplied Python path is imported by the helper.
Polkit uses `auth_admin`, not a persistent/passwordless authorization. A normal
Kubuntu session provides its authentication agent. UDisks handles deliberate
unmount requests through its existing service.

For package inspection without system changes:

```bash
DESTDIR=/tmp/drivepak-helper-stage ./install-helper.sh
```

That stages files only; it does not activate card access. Development used this
route and **did not install the helper or modify system configuration**.

## Removing the application

Remove the user virtualenv/application directory and the two installed launchers,
desktop entry and SVG icon using your file manager. Default paths:
`~/.local/share/drivepak-manager`, `~/.local/bin/drivepak-manager`,
`~/.local/bin/drivepak`, `~/.local/share/applications/drivepak-manager.desktop`,
`~/.local/share/icons/hicolor/scalable/apps/drivepak-manager.svg`.

For the helper, use an administrator file manager to remove the three exact
system files listed above. No permissions rules or daemon remain. Backups and
working images are user data and are never automatically removed by uninstall.

## Startup troubleshooting

* **Reader reports 0 bytes / No medium found:** Linux sees the USB reader but
  does not report readable card capacity. Empty slots remain listed as
  **No readable card detected**, and selection cannot start a backup or write.
  Reseat the microSD in the reader, reconnect the reader, then **Refresh**.
  Try another existing reader if available. Proceed only when the card reports
  a nonzero capacity. No formatting is needed to back up a proprietary DrivePak
  card; this check happens before any card layout is read.
* Qt `xcb` errors inside a restricted execution sandbox can mean denied display
  access. Launch from a normal Kubuntu terminal. Headless tests use
  `QT_QPA_PLATFORM=offscreen`.
* A minimal host may need `libxcb-cursor0` and normal XCB/Wayland libraries. Qt's
  error output identifies missing libraries; use Discover/apt deliberately.
* A missing helper or failed authentication affects real card access only; image
  editing remains available. Never run the entire GUI as root to bypass it.
* Device enumeration uses `lsblk`, `findmnt`, sysfs and `/proc/swaps`; unmount uses
  `udisksctl`. These standard Kubuntu tools must be present.
