#!/usr/bin/env python3
"""Inspect package inventory, helper entrypoint and authentication policy offline."""
from pathlib import Path
import tarfile
import xml.etree.ElementTree as ET
import zipfile

root = Path(__file__).resolve().parents[1]
dist = root / "dist"
stage = dist / "drivepak-manager-1.0.2-linux"
for name in ("drivepak-manager-1.0.2-linux.tar.gz", "drivepak_manager-1.0.2.tar.gz"):
    with tarfile.open(dist / name) as t:
        for item in t.getmembers():
            p = Path(item.name)
            assert not p.is_absolute() and ".." not in p.parts and not item.issym()
            assert not {"data", "backups", "exports", ".git", ".venv", "__pycache__"}.intersection(p.parts), item.name
            assert p.suffix not in (".img", ".dsk", ".bin", ".pdf", ".log"), item.name
with zipfile.ZipFile(stage / "drivepak-manager-helper.pyz") as z:
    assert set(z.namelist()) == {"__main__.py", *("drivepak_manager/" + x for x in ("__init__.py", "errors.py", "streams.py", "backup.py", "devices.py", "helper.py"))}
    for name in z.namelist():
        compile(z.read(name), name, "exec")
with zipfile.ZipFile(dist / "drivepak_manager-1.0.2-py3-none-any.whl") as z:
    assert any(x.endswith("licenses/THIRD_PARTY.md") for x in z.namelist())
policy = ET.parse(stage / "org.drivepak.manager.policy")
assert policy.findtext("action/defaults/allow_active") == "auth_admin"
assert policy.findtext("action/defaults/allow_any") == "no"
assert policy.findtext("action/defaults/allow_inactive") == "no"
assert policy.find("action/annotate").text == "/usr/local/libexec/drivepak-manager-helper"
print("Package inventories, embedded helper syntax, licensing and explicit authentication policy verified.")
