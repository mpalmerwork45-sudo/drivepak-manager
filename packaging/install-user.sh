#!/bin/sh
set -eu
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_PREFIX=${DRIVEPAK_PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}/drivepak-manager}
if [ "$(id -u)" = 0 ]; then
    echo 'Run this user installer without sudo. Use install-helper.sh separately for card access.' >&2
    exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11 or newer is required"'
python3 -m venv "$INSTALL_PREFIX/venv"
"$INSTALL_PREFIX/venv/bin/python" -m pip install -r "$PACKAGE_DIR/requirements-runtime.lock"
"$INSTALL_PREFIX/venv/bin/python" -m pip install --no-deps --force-reinstall "$PACKAGE_DIR/drivepak_manager-1.0.3-py3-none-any.whl"
"$INSTALL_PREFIX/venv/bin/python" - "$PACKAGE_DIR" "$INSTALL_PREFIX" <<'PY'
from pathlib import Path
import os, shlex, shutil, sys
package, prefix = map(Path, sys.argv[1:])
data = Path(os.environ.get('DRIVEPAK_DATA_DIR', os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share'))))
bin_dir = Path(os.environ.get('DRIVEPAK_BIN_DIR', str(Path.home()/'.local/bin')))
bin_dir.mkdir(parents=True, exist_ok=True)
for name in ('drivepak', 'drivepak-manager'):
    entry = bin_dir/name
    entry.write_text('#!/bin/sh\nexec ' + shlex.quote(str(prefix/'venv/bin'/name)) + ' "$@"\n')
    entry.chmod(0o755)
(data/'applications').mkdir(parents=True, exist_ok=True)
desktop = (package/'drivepak-manager.desktop').read_text()
executable = str(prefix/'venv/bin/drivepak-manager').replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
desktop = desktop.replace('Exec=drivepak-manager %f', 'Exec="' + executable + '" %f')
(data/'applications/drivepak-manager.desktop').write_text(desktop)
icons = data/'icons/hicolor/scalable/apps'
icons.mkdir(parents=True, exist_ok=True)
shutil.copyfile(package/'drivepak-manager.svg', icons/'drivepak-manager.svg')
shutil.copytree(package/'docs', prefix/'docs', dirs_exist_ok=True)
for name in ('LICENSE', 'THIRD_PARTY.md', 'README.md'):
    shutil.copyfile(package/name, prefix/name)
print('Installed:', prefix/'venv/bin/drivepak-manager')
print('Applications menu: DrivePak Manager')
PY
