#!/usr/bin/env python3
"""Build a minimal stdlib-only helper and a Linux installable tar distribution."""
from pathlib import Path
import hashlib
import shutil
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
NAME = "drivepak-manager-1.0.2-linux"
stage = DIST / NAME
stage.mkdir(parents=True, exist_ok=True)
# A fixed member list avoids accidentally packaging GUI libraries, caches or data.
modules = ("__init__.py", "errors.py", "streams.py", "backup.py", "devices.py", "helper.py")
with zipfile.ZipFile(stage / "drivepak-manager-helper.pyz", "w", compression=zipfile.ZIP_DEFLATED) as z:
    for module in modules:
        info = zipfile.ZipInfo("drivepak_manager/" + module, (2026, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        z.writestr(info, (ROOT / "src/drivepak_manager" / module).read_bytes())
    z.writestr(zipfile.ZipInfo("__main__.py", (2026, 1, 1, 0, 0, 0)), b"from drivepak_manager.helper import main\nraise SystemExit(main())\n")
for path in (ROOT / "packaging").iterdir():
    shutil.copyfile(path, stage / path.name)
    (stage / path.name).chmod(0o755 if path.name.endswith(".sh") or path.name == "drivepak-manager-helper" else 0o644)
for name in ("README.md", "LICENSE", "THIRD_PARTY.md", "requirements-runtime.lock"):
    shutil.copyfile(ROOT / name, stage / name)
shutil.copytree(ROOT / "docs", stage / "docs", dirs_exist_ok=True)
wheel = DIST / "drivepak_manager-1.0.2-py3-none-any.whl"
shutil.copyfile(wheel, stage / wheel.name)
checksums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(stage)}\n" for p in sorted(stage.rglob("*")) if p.is_file() and p.name != "SHA256SUMS")
(stage / "SHA256SUMS").write_text(checksums)
archive = DIST / (NAME + ".tar.gz")
with tarfile.open(archive, "w:gz") as t:
    t.add(stage, arcname=NAME)
print(archive)
print("Distribution contains no reference images, archived source, games or firmware.")
