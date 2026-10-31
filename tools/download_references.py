#!/usr/bin/env python3
"""Download research references, extract safely, record local provenance. stdlib only."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import urllib.request
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/reference"
URLS = {
    "manual.pdf": "https://colorcomputerarchive.com/repo/Documents/Manuals/Hardware/MicroSD%20Drive%20Pak%20Quick%20Docs%20%28Roger%20Taylor%29.pdf",
    "explorer.zip": "https://colorcomputerarchive.com/repo/Programming/Source/DrivePak%20Explorer/DrivePak%20Explorer%201.00%20Source%20Code%20%28Guillaume%20Major%29.zip",
    "drivepak-2gb.zip": "https://colorcomputerarchive.com/repo/Disks/DrivePak/Image/DrivePak%202GB%20Image%20%282022-02-11%29.zip",
}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cached", action="store_true", help="never access network; inspect existing downloads")
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest = {"notice": "Locally calculated SHA-256 establishes reproducibility, not independent publisher authentication.", "files": []}
    now = datetime.now(timezone.utc).isoformat()
    previous = ROOT / "docs/provenance.json"
    old = {x["path"]: x for x in json.loads(previous.read_text()).get("files", [])} if previous.exists() else {}
    for name, url in URLS.items():
        target = CACHE / name
        part = target.with_suffix(target.suffix + ".partial")
        if not target.exists():
            if args.cached:
                # A completed manually downloaded ZIP is still validated by ZipFile CRC below.
                if part.exists() and zipfile.is_zipfile(part):
                    part.rename(target)
                else:
                    raise SystemExit(f"Missing {target}; run without --cached")
            else:
                with urllib.request.urlopen(url, timeout=90) as source, part.open("xb") as dest:
                    shutil.copyfileobj(source, dest, 4 * 1024 * 1024)
                    dest.flush()
                    os.fsync(dest.fileno())
                part.rename(target)
        key = str(target.relative_to(ROOT))
        entry = {"path": key, "url": url, "retrieved_utc": old.get(key, {}).get("retrieved_utc", now), "bytes": target.stat().st_size, "sha256": digest(target)}
        manifest["files"].append(entry)
        if name.endswith(".zip"):
            dest = CACHE / ("explorer" if name == "explorer.zip" else "card-2gb")
            dest.mkdir(exist_ok=True)
            with zipfile.ZipFile(target) as archive:
                if shutil.disk_usage(dest).free < sum(m.file_size for m in archive.infolist()) + 64 * 1024**2:
                    raise SystemExit("Not enough space for extraction")
                if len(archive.infolist()) > 1000:
                    raise SystemExit("Unexpected archive member count")
                for member in archive.infolist():
                    p = PurePosixPath(member.filename)
                    if p.is_absolute() or ".." in p.parts or "\\" in member.filename or stat.S_ISLNK(member.external_attr >> 16):
                        raise SystemExit(f"Unsafe member: {member.filename}")
                    out = dest.joinpath(*p.parts)
                    if not out.resolve().is_relative_to(dest.resolve()) or member.file_size > 9 * 1024**3:
                        raise SystemExit("Unsafe member size/path")
                    if member.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if not out.exists():
                        partial = out.with_name(out.name + ".partial")
                        with archive.open(member) as source, partial.open("xb") as f:
                            shutil.copyfileobj(source, f, 4 * 1024 * 1024)
                            f.flush()
                            os.fsync(f.fileno())
                        partial.rename(out)
                    # Read through archive CRC even for cached members; hash decompressed source.
                    h = hashlib.sha256()
                    with archive.open(member) as f:
                        for b in iter(lambda: f.read(4 * 1024 * 1024), b""):
                            h.update(b)
                    if out.stat().st_size != member.file_size or digest(out) != h.hexdigest():
                        raise SystemExit(f"Cached extraction differs: {out}")
                    out.chmod(0o444)
                    manifest["files"].append({"path": str(out.relative_to(ROOT)), "archive": key, "member": member.filename, "retrieved_utc": entry["retrieved_utc"], "bytes": member.file_size, "sha256": h.hexdigest()})
        print(f"{name}: {entry['bytes']} bytes, SHA-256 {entry['sha256']}", flush=True)
    previous.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    run()
