"""Verified full-capacity snapshots and checked writes, independent of privilege/UI."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

from . import __version__
from .errors import DrivePakError, SafetyError
from .streams import (atomic_output, checkpoint, copy_stream, distinct, fingerprint,
                      hash_stream, need_space, report, utcnow, write_json)

WRITE_SYNC_BYTES = 64 * 1024**2


def require_capacity(capacity):
    if not isinstance(capacity, int) or capacity < 512 or capacity % 512:
        raise SafetyError(f"No valid readable card capacity ({capacity} bytes). Check the card and reader before starting a backup or write.")


def snapshot_stream(target, capacity, identity, output, readback, progress=None, cancel=None, guard=None):
    """Caller holds exclusive stable target access. readback is separately opened."""
    require_capacity(capacity)
    if guard:
        guard()
    digest = copy_stream(target, output, capacity, progress, cancel, guard)
    report(progress, 0, 0, "Flushing saved backup to storage")
    output.flush()
    os.fsync(output.fileno())
    if os.fstat(readback.fileno()).st_size != capacity:
        raise DrivePakError("Backup length differs from the complete device capacity.")
    saved_digest = hash_stream(readback, capacity, progress, cancel, "Verifying saved backup")
    if saved_digest != digest:
        raise DrivePakError("Backup readback checksum mismatch; output remains incomplete.")
    # A second complete device pass establishes snapshot correspondence and catches
    # changes while reading. Never assume one read is a stable source stream.
    if hasattr(target, "invalidate"):
        report(progress, 0, 0, "Preparing second full-card read — keep card connected")
        target.invalidate()
    current = hash_stream(target, capacity, progress, cancel, "Checking source stability", guard)
    if current != digest:
        raise SafetyError("Device changed during backup. Snapshot was not verified.")
    if guard:
        guard()
    return {"schema": 1, "kind": "complete-physical-backup", "status": "verified",
            "identity": identity, "capacity": capacity, "bytes": capacity,
            "sha256": digest, "created_utc": utcnow(), "application_version": __version__,
            "verification": "Durable output independently reopened and hashed; complete source read twice under exclusive access."}


def verify_backup(path, progress=None, cancel=None):
    path = Path(path)
    manifest_path = Path(str(path) + ".json")
    if not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise SafetyError("Missing or oversized backup manifest. A raw image alone is not a verified device snapshot.")
    try:
        m = json.loads(manifest_path.read_text())
        if m["schema"] != 1 or m["kind"] != "complete-physical-backup" or m["status"] != "verified" or m["bytes"] != m["capacity"]:
            raise ValueError("not a complete verified snapshot")
        require_capacity(m["capacity"])
        with path.open("rb", buffering=0) as f:
            before = fingerprint(f)
            if before[2] != m["capacity"] or hash_stream(f, before[2], progress, cancel) != m["sha256"]:
                raise SafetyError("Backup length/checksum does not match its manifest. Do not use it as a recovery snapshot.")
            if fingerprint(f) != before:
                raise SafetyError("Backup changed while being verified.")
        return m
    except (KeyError, TypeError, ValueError) as e:
        raise SafetyError(f"Invalid backup manifest: {e}") from e


def restore_stream(target, capacity, identity, source, backup, manifest, progress=None, cancel=None, guard=None):
    """Never writes until target's complete current contents match the backup."""
    require_capacity(capacity)
    if not isinstance(manifest, dict) or manifest.get("kind") != "complete-physical-backup" or manifest.get("status") != "verified" or manifest.get("schema") != 1:
        raise SafetyError("A verified complete backup manifest is required.")
    if manifest.get("identity") != identity or manifest.get("capacity") != capacity or manifest.get("bytes") != capacity:
        raise SafetyError("Backup provenance does not match this target's identity and capacity. Back up the selected target first.")
    sf, bf = fingerprint(source), fingerprint(backup)
    if sf[:2] == bf[:2] or sf[2] <= 0 or sf[2] > capacity or sf[2] % 512:
        raise SafetyError("Image must be a separate nonempty 512-byte-aligned file fitting the target. No truncation is allowed.")
    if bf[2] != capacity:
        raise SafetyError("Backup is not the target's complete physical capacity.")
    if hash_stream(backup, capacity, progress, cancel, "Verifying recovery backup") != manifest.get("sha256"):
        raise SafetyError("Recovery backup checksum is invalid.")
    if fingerprint(backup) != bf:
        raise SafetyError("Recovery backup changed during verification.")
    source_digest = hash_stream(source, sf[2], progress, cancel, "Checking source image")
    if fingerprint(source) != sf:
        raise SafetyError("Source image changed before writing.")
    if guard:
        guard()
    if hasattr(target, "invalidate"):
        report(progress, 0, 0, "Preparing current-card backup comparison")
        target.invalidate()
    if hash_stream(target, capacity, progress, cancel, "Checking backup against current target", guard) != manifest.get("sha256"):
        raise SafetyError("Target no longer matches the backup. Create and verify a fresh complete backup before writing.")
    if fingerprint(backup) != bf or fingerprint(source) != sf:
        raise SafetyError("Source or recovery backup changed before writing.")
    return _write_verified(target, capacity, source, sf, source_digest, progress, cancel, guard, backup, bf)


def direct_write_stream(target, capacity, source, progress=None, cancel=None, guard=None, *, acknowledge_no_backup=False):
    """Explicit opt-in: no snapshot, same protected write and independent readback."""
    if acknowledge_no_backup is not True:
        raise SafetyError("Direct writing requires explicit acknowledgement that no recovery backup will be created.")
    require_capacity(capacity)
    sf = fingerprint(source)
    tf = os.fstat(target.fileno())
    if sf[:2] == (tf.st_dev, tf.st_ino) or not 0 < sf[2] <= capacity or sf[2] % 512:
        raise SafetyError("Source must be a separate nonempty 512-byte-aligned image fitting the card. No truncation is allowed.")
    if guard:
        guard()
    digest = hash_stream(source, sf[2], progress, cancel, "Checking source image")
    if fingerprint(source) != sf:
        raise SafetyError("Source image changed before writing.")
    return _write_verified(target, capacity, source, sf, digest, progress, cancel, guard)


def _write_verified(target, capacity, source, sf, source_digest, progress, cancel, guard, backup=None, bf=None):
    checkpoint(cancel)
    if guard:
        guard()
    started = False
    synced = 0

    def sync_batch(done):
        nonlocal synced
        # Bound dirty writeback instead of queuing a whole card in Linux RAM.
        # The final partial batch is flushed before reporting writing at 100%.
        if done - synced >= WRITE_SYNC_BYTES or done == sf[2]:
            report(progress, 0, 0, "Flushing card — keep card connected; verification follows")
            target.flush()
            os.fsync(target.fileno())
            synced = done
            checkpoint(cancel)
            if guard:
                guard()

    try:
        # Setting this before copy is conservative: cancellation or a first-write
        # failure can still leave device/controller state uncertain.
        started = True
        copied = copy_stream(source, target, sf[2], progress, cancel, guard,
                             phase="Writing card", after_chunk=sync_batch)
        if hasattr(target, "invalidate"):
            report(progress, 0, 0, "Preparing independent card readback — keep card connected")
            target.invalidate()
        if copied != source_digest or fingerprint(source) != sf or (backup is not None and fingerprint(backup) != bf):
            raise SafetyError("Source image or recovery backup changed during the physical write.")
        if guard:
            guard()
        readback = hash_stream(target, sf[2], progress, cancel, "Reading back written card range", guard)
        if readback != source_digest:
            raise DrivePakError("Card readback checksum mismatch.")
        if fingerprint(source) != sf or (backup is not None and fingerprint(backup) != bf):
            raise SafetyError("Source image or recovery backup changed during verification.")
        if guard:
            guard()
        return {"status": "verified", "bytes_written": sf[2], "target_capacity": capacity,
                "sha256": source_digest, "verified_utc": utcnow(),
                "backup_policy": "verified-current-target" if backup is not None else "none-explicit-direct-write",
                "tail_policy": "Bytes beyond the image were preserved; partitions were not expanded."}
    except BaseException as e:
        if started:
            try:
                target.flush()
                os.fsync(target.fileno())
            except OSError:
                pass
            recovery = ("Keep the recovery backup. Re-select the card, take a fresh snapshot of its current state, then explicitly restore the earlier verified image."
                        if backup is not None else "No recovery backup was created for this direct write. Recovery requires an existing backup or another known-good image.")
            raise DrivePakError(f"PHYSICAL WRITE INCOMPLETE OR UNVERIFIED: {e} The card may contain partial data. {recovery} No automatic retry occurred.") from e
        raise


@dataclass
class SimulatedDevice:
    """Regular-file test backend; never accepts a real device."""
    path: Path

    def __post_init__(self):
        import stat
        self.path = Path(self.path).resolve()
        s = self.path.stat()
        if not stat.S_ISREG(s.st_mode):
            raise SafetyError("Simulation accepts only regular files, never devices.")
        self.capacity = s.st_size
        require_capacity(self.capacity)
        self.key = (s.st_dev, s.st_ino, s.st_size)
        self.identity = {"simulated": True, "path": str(self.path), "device": s.st_dev, "inode": s.st_ino, "capacity": s.st_size}

    def guard(self):
        s = self.path.stat()
        if (s.st_dev, s.st_ino, s.st_size) != self.key:
            raise SafetyError("Simulated device was replaced or resized.")

    def backup(self, destination, progress=None, cancel=None):
        destination = Path(destination)
        distinct(self.path, destination)
        need_space(destination.parent, self.capacity)
        if Path(str(destination) + ".json").exists():
            raise SafetyError("Backup manifest already exists; choose a new name.")
        with self.path.open("rb", buffering=0) as target, atomic_output(destination) as out:
            with open(out.partial_path, "rb", buffering=0) as check:
                m = snapshot_stream(target, self.capacity, self.identity, out, check, progress, cancel, self.guard)
        write_json(str(destination) + ".json", m)
        return m

    def write(self, source, recovery_backup, progress=None, cancel=None):
        distinct(self.path, source, recovery_backup)
        manifest = verify_backup(recovery_backup, progress, cancel)
        with self.path.open("r+b", buffering=0) as target, open(source, "rb", buffering=0) as src, open(recovery_backup, "rb", buffering=0) as back:
            return restore_stream(target, self.capacity, self.identity, src, back, manifest, progress, cancel, self.guard)

    def write_direct(self, source, progress=None, cancel=None, *, acknowledge_no_backup=False):
        distinct(self.path, source)
        with self.path.open("r+b", buffering=0) as target, open(source, "rb", buffering=0) as src:
            return direct_write_stream(target, self.capacity, src, progress, cancel, self.guard,
                                       acknowledge_no_backup=acknowledge_no_backup)

    def apply_changes(self, journal, receipt, progress=None, cancel=None, *, recovery=False):
        from .patches import apply_changes_stream
        distinct(self.path, journal, receipt)
        if not recovery and Path(receipt).absolute() != Path(str(journal) + ".json").absolute():
            raise SafetyError("Save the initial recovery receipt beside its journal as JOURNAL.dpp.json.")
        manifest = json.loads(Path(str(journal) + ".json").read_text()) if recovery else None
        with self.path.open("r+b", buffering=0) as target, open(journal, "rb", buffering=0) as source:
            return apply_changes_stream(target, self.capacity, self.identity, source,
                lambda m: write_json(receipt, m), progress, cancel, self.guard,
                acknowledge_in_place=True, recovery=recovery, manifest=manifest)
