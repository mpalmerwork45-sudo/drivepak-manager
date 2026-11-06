"""Automation interface; image edits always require a separate output path."""
import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import stat
import sys

from . import __version__
from .backup import SimulatedDevice, verify_backup
from .devices import discover
from .errors import DrivePakError, SafetyError, FormatError
from .filesystems.decb import DECB
from .filesystems.os9 import OS9
from .image import Image, RAW_DISK_SIZES, create_blank
from .linux import backup_card, write_card, write_card_direct, apply_card_changes
from .patches import prepare_changes
from .streams import copy_verified, hash_file


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def progress(done, total, phase):
    if sys.stderr.isatty():
        print(f"\r{phase}: {done:,}/{total:,}      ", end="", file=sys.stderr, flush=True)


def parser():
    p = argparse.ArgumentParser(prog="drivepak", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    x = sub.add_parser("inspect", help="layout and diagnostics, including unrecognised images")
    x.add_argument("image")
    x = sub.add_parser("new", help="create a blank DrivePak image with an empty DOS bank")
    x.add_argument("output")
    x.add_argument("--capacity-mib", type=int, default=64)
    x.add_argument("--disks", type=int, default=16)
    x.add_argument("--name", default="DRIVEPAK")
    x = sub.add_parser("copy", help="stream a byte-identical, durably verified image copy")
    x.add_argument("image")
    x.add_argument("output")

    def image_args(name, edit=False, disk=False):
        x = sub.add_parser(name)
        x.add_argument("image")
        if edit:
            x.add_argument("output", help="new image output; originals are never changed")
        x.add_argument("--partition", type=int, required=True, help="stable table index")
        if disk:
            x.add_argument("--disk", type=int, required=True, help="zero-based virtual floppy slot")
        return x

    x = image_args("disk-export", disk=True)
    x.add_argument("output")
    x.add_argument("--size", type=int, choices=RAW_DISK_SIZES, default=737280, help="explicit bytes, not inferred original geometry")
    x = sub.add_parser("disk-import", help="batch disk import with explicit mapping")
    x.add_argument("image")
    x.add_argument("output")
    x.add_argument("--map", action="append", required=True, metavar="PART:SLOT:PATH")
    x.add_argument("--tail", choices=("zero", "ff", "preserve"), default="zero")
    x = image_args("files")
    x.add_argument("--disk", type=int)
    x.add_argument("--path", default="/", help="OS-9 directory path")
    x = image_args("extract")
    x.add_argument("name", help="BASIC filename or absolute OS-9 path")
    x.add_argument("output")
    x.add_argument("--disk", type=int)
    x = image_args("insert", edit=True)
    x.add_argument("--disk", type=int, help="Disk BASIC slot; omit for an OS-9 partition")
    x.add_argument("source")
    x.add_argument("--name")
    x.add_argument("--type", type=int, choices=range(4), help="Disk BASIC type (default 2)")
    x.add_argument("--ascii", action="store_true")
    x.add_argument("--replace", action="store_true")
    x.add_argument("--path", default="/", help="OS-9 destination directory (default /)")
    x.add_argument("--attributes", type=lambda value: int(value, 0), help="OS-9 attributes as decimal or hex; default metadata is preserved")
    x.add_argument("--owner", type=lambda value: int(value, 0), help="OS-9 owner ID; default metadata is preserved")
    x = image_args("rename-file", edit=True)
    x.add_argument("--disk", type=int, help="Disk BASIC slot; omit for an OS-9 partition")
    x.add_argument("name")
    x.add_argument("new_name")
    x = image_args("delete-file", edit=True)
    x.add_argument("--disk", type=int, help="Disk BASIC slot; omit for an OS-9 partition")
    x.add_argument("name")
    x = sub.add_parser("partition-add")
    x.add_argument("image")
    x.add_argument("output")
    x.add_argument("--name", required=True)
    x.add_argument("--disks", type=int, default=1)
    x.add_argument("--os9", help="raw OS-9 volume instead of empty BASIC bank")
    x = image_args("partition-rename", edit=True)
    x.add_argument("name")
    x = image_args("partition-delete", edit=True)
    x.add_argument("--reassign", action="append", default=[], metavar="DEFAULT:INDEX", help="others become unassigned; disk numbers stay in place")
    x = image_args("set-default", edit=True)
    x.add_argument("drive", choices=("DOS", "DD", "U0", "U1", "U2", "U3"))
    x = image_args("volume-export")
    x.add_argument("output")
    x.add_argument("--diagnostic", action="store_true", help="explicitly export only the stored extent of an invalid OS-9 volume")
    x = image_args("volume-import", edit=True)
    x.add_argument("source")
    sub.add_parser("devices", help="read-only Linux device enumeration and protection reasons")
    x = sub.add_parser("verify-backup")
    x.add_argument("backup")
    x = sub.add_parser("backup-card")
    x.add_argument("device")
    x.add_argument("output")
    x = sub.add_parser("write-card", help="authenticated physical write with mandatory readback verification")
    x.add_argument("device")
    x.add_argument("image")
    policy = x.add_mutually_exclusive_group(required=True)
    policy.add_argument("--backup", help="complete verified snapshot of this target's current bytes")
    policy.add_argument("--no-backup", action="store_true", help="explicitly accept overwriting without creating a recovery backup")
    x.add_argument("--confirm-device", help="automation only: must exactly equal selected device path; otherwise interactive final review")
    x = sub.add_parser("simulate-backup", help="regular-file simulated device only")
    x.add_argument("device_file")
    x.add_argument("output")
    x = sub.add_parser("simulate-write", help="regular-file simulated device only")
    x.add_argument("device_file")
    x.add_argument("image")
    policy = x.add_mutually_exclusive_group(required=True)
    policy.add_argument("--backup")
    policy.add_argument("--no-backup", action="store_true")
    x = sub.add_parser("prepare-edits", help="create a before/after sector journal from original and modified images")
    x.add_argument("original")
    x.add_argument("modified")
    x.add_argument("journal")
    for command in ("apply-edits", "recover-edits"):
        x = sub.add_parser(command, help="authenticated changed-sector write and readback; preserves unrelated card regions")
        x.add_argument("device")
        x.add_argument("journal")
        x.add_argument("--receipt", required=True, help="new recovery receipt path; never overwrite the original journal's .json receipt")
        x.add_argument("--confirm-device")
    x = sub.add_parser("simulate-apply-edits", help="regular-file simulated card only")
    x.add_argument("device_file")
    x.add_argument("journal")
    x.add_argument("--receipt", required=True)
    x.add_argument("--recover", action="store_true")
    return p


def selected_device(path):
    matches = [d for d in discover() if d.path == path]
    if len(matches) != 1 or not matches[0].usable:
        raise SafetyError("Select an available external device from 'drivepak devices'; unmount deliberately through the desktop if necessary.")
    return matches[0]


def execute(a):
    cmd = a.command
    if cmd == "insert":
        if a.disk is None and (a.type is not None or a.ascii):
            raise FormatError("--type and --ascii describe Disk BASIC files. Omit them for OS-9; bytes are always copied unchanged.")
        if a.disk is not None and (a.attributes is not None or a.owner is not None or a.path != "/"):
            raise FormatError("--attributes, --owner and a destination --path apply to OS-9 files; omit --disk for OS-9.")
    if cmd == "devices":
        return discover()
    if cmd == "verify-backup":
        return verify_backup(a.backup, progress)
    if cmd == "prepare-edits":
        return prepare_changes(a.original, a.modified, a.journal, progress=progress)
    if cmd in ("apply-edits", "recover-edits"):
        d = selected_device(a.device)
        journal_digest = hash_file(a.journal, progress)
        if a.confirm_device is not None:
            if a.confirm_device != d.path:
                raise SafetyError("--confirm-device must exactly equal the selected destination.")
        else:
            if not sys.stdin.isatty():
                raise SafetyError("Noninteractive card edits require --confirm-device with the exact destination.")
            print(f"{cmd}: {d.path} · {d.model} · {d.capacity:,} bytes · {d.serial or d.diskseq}\nJournal: {a.journal}\nNon-atomic in-place update. Recovery covers journaled sectors only, not a complete physical backup.", file=sys.stderr)
            if input(f"Type {d.path} to write changed sectors and verify: ") != d.path:
                raise SafetyError("Card edit was not confirmed.")
        return apply_card_changes(d, a.journal, a.receipt, progress, recovery=cmd == "recover-edits",
                                  expected_journal=journal_digest)
    if cmd == "simulate-apply-edits":
        return SimulatedDevice(Path(a.device_file)).apply_changes(a.journal, a.receipt, progress, recovery=a.recover)
    if cmd == "backup-card":
        return backup_card(selected_device(a.device), a.output, progress=progress)
    if cmd == "write-card":
        d = selected_device(a.device)
        selected = Path(a.image).stat()
        selected_identity = (selected.st_dev, selected.st_ino, selected.st_size, selected.st_mtime_ns, selected.st_ctime_ns)
        if not stat.S_ISREG(selected.st_mode) or not 0 < selected.st_size <= d.capacity or selected.st_size % 512:
            raise SafetyError("Select a nonempty regular image with complete 512-byte sectors fitting the selected card. No truncation is allowed.")
        if a.confirm_device is not None:
            if a.confirm_device != d.path:
                raise SafetyError("--confirm-device must exactly equal the selected destination.")
        else:
            if not sys.stdin.isatty():
                raise SafetyError("Noninteractive physical writes require --confirm-device with the exact destination path.")
            policy = "DIRECT WRITE: no recovery backup will be created." if a.no_backup else f"Verified current-target backup: {a.backup}"
            print(f"READ image: {a.image}\nWRITE card: {d.path} · {d.model} · {d.capacity:,} bytes · {d.serial or d.diskseq}\n{policy}\nWrite range: {selected.st_size:,} bytes; larger-card tail: {d.capacity - selected.st_size:,} bytes, preserved. Interrupted writes can leave incomplete contents.", file=sys.stderr)
            if input(f"Type {d.path} to write and verify: ") != d.path:
                raise SafetyError("Write was not confirmed.")
        return (write_card_direct(d, a.image, progress, acknowledge_no_backup=True, expected_source=selected_identity)
                if a.no_backup else write_card(d, a.image, a.backup, progress, expected_source=selected_identity))
    if cmd == "simulate-backup":
        return SimulatedDevice(Path(a.device_file)).backup(a.output, progress)
    if cmd == "simulate-write":
        simulated = SimulatedDevice(Path(a.device_file))
        return simulated.write_direct(a.image, progress, acknowledge_no_backup=True) if a.no_backup else simulated.write(a.image, a.backup, progress)
    if cmd == "new":
        create_blank(a.output, a.capacity_mib * 1024**2, a.disks, a.name, progress)
        return {"created": a.output}
    if cmd == "copy":
        return {"output": a.output, "sha256": copy_verified(a.image, a.output, progress)}
    edits = {"disk-import", "insert", "rename-file", "delete-file", "partition-add", "partition-rename", "partition-delete", "set-default", "volume-import"}
    if cmd in edits:
        copy_verified(a.image, a.output, progress)
        path, writable = a.output, True
    else:
        path, writable = a.image, False
    with Image(path, writable) as image:
        if cmd == "inspect":
            return image.layout()
        if cmd == "disk-export":
            image.export_disk(a.partition, a.disk, a.output, a.size, progress)
        elif cmd == "disk-import":
            try:
                maps = [value.split(":", 2) for value in a.map]
                mapping = [(int(p), int(d), name) for p, d, name in maps]
            except ValueError as e:
                raise DrivePakError("Use --map PART:SLOT:PATH for every source file.") from e
            image.import_disks(mapping, a.tail, progress)
        elif cmd == "files":
            if a.disk is not None:
                fs = DECB(image.volume(a.partition, a.disk))
                return {"files": fs.files, "free_bytes": fs.free_bytes, "mutation_restrictions": fs.issues}
            return OS9(image.volume(a.partition)).list_dir(a.path)
        elif cmd == "extract":
            if a.disk is not None:
                DECB(image.volume(a.partition, a.disk)).extract(a.name, a.output)
            else:
                OS9(image.volume(a.partition)).extract(a.name, a.output, progress)
        elif cmd == "insert":
            if a.disk is not None:
                DECB(image.volume(a.partition, a.disk, editing=True)).insert(a.source, a.name, a.type if a.type is not None else 2, 255 if a.ascii else 0, a.replace)
            else:
                OS9(image.volume(a.partition, editing=True)).insert(a.source, directory=a.path, name=a.name,
                    replace=a.replace, attributes=a.attributes, owner=a.owner, progress=progress)
        elif cmd == "rename-file":
            if a.disk is not None:
                DECB(image.volume(a.partition, a.disk, editing=True)).rename(a.name, a.new_name)
            else:
                OS9(image.volume(a.partition, editing=True)).rename(a.name, a.new_name, progress=progress)
        elif cmd == "delete-file":
            if a.disk is not None:
                DECB(image.volume(a.partition, a.disk, editing=True)).delete(a.name)
            else:
                OS9(image.volume(a.partition, editing=True)).delete(a.name, progress=progress)
        elif cmd == "partition-add":
            return {"output": a.output, "partition_index": image.create_partition(a.name, a.disks, a.os9, progress)}
        elif cmd == "partition-rename":
            image.rename_partition(a.partition, a.name)
        elif cmd == "partition-delete":
            try:
                replacements = {key: int(idx) for key, idx in (v.split(":") for v in a.reassign)}
            except ValueError as e:
                raise DrivePakError("Use --reassign DEFAULT:INDEX.") from e
            image.delete_partition(a.partition, replacements)
        elif cmd == "set-default":
            image.set_default(a.drive, a.partition)
        elif cmd == "volume-export":
            p = image.layout().partition(a.partition)
            if p.kind not in (3, 4):
                raise FormatError("Select an OS-9 partition for whole-volume export.")
            warning = None
            try:
                OS9(image.volume(a.partition))
            except FormatError as e:
                if not a.diagnostic:
                    raise FormatError(f"{e} Use --diagnostic to explicitly export only the stored partition extent.") from e
                warning = str(e)
            image.volume(a.partition).export(a.output, progress=progress)
            return {"status": "complete", "output": a.output, "diagnostic_partition_only": warning is not None,
                    "representation": "raw 256-byte payload sectors", "warning": warning}
        elif cmd == "volume-import":
            image.import_os9(a.partition, a.source, progress)
        return {"status": "complete", "output": a.output}


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        result = execute(args)
        if sys.stderr.isatty():
            print(file=sys.stderr)
        print(json.dumps(result, default=encode, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Cancelled. Partial outputs are not verified. If a physical write began, preserve the recovery backup and treat the card as incomplete.", file=sys.stderr)
        return 130
    except (DrivePakError, OSError, ValueError) as e:
        print(f"drivepak: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
