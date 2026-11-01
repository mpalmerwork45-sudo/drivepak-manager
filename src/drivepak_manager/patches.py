"""Durable image edit journals and verified, bounded in-place card updates.

A journal stores each changed physical sector's before/after bytes. It is a
partial recovery file, never a complete physical backup. No format is guessed.
"""
import hashlib
import os
from pathlib import Path
import struct

from .errors import DrivePakError, SafetyError
from . import __version__
from .streams import (CHUNK, atomic_output, checkpoint, distinct, fingerprint,
                      hash_stream, need_space, read_exact, report, utcnow, write_all)

BLOCK = 512
MAGIC = b"DPEDIT01"
HEADER = struct.Struct(">8sQQ32s")
RECORD_SIZE = 8 + 2 * BLOCK


def prepare_changes(original, working, destination, expected_original=None, progress=None, cancel=None):
    """Compare regular image files, stream a new journal, preserve both inputs."""
    import stat
    distinct(original, working, destination)
    for path in (original, working):
        if not stat.S_ISREG(Path(path).stat().st_mode):
            raise SafetyError("Edit journals require regular image files.")
    with open(original, "rb", buffering=0) as old, open(working, "rb", buffering=0) as new:
        of, nf = fingerprint(old), fingerprint(new)
        size = of[2]
        if not size or size % BLOCK or nf[2] != size:
            raise SafetyError("In-place edits require equal, nonempty, 512-byte-aligned image lengths.")
        before_hash, after_hash, record_hash = hashlib.sha256(), hashlib.sha256(), hashlib.sha256()
        count = 0
        with atomic_output(destination) as out:
            write_all(out, bytes(HEADER.size))
            for offset in range(0, size, CHUNK):
                checkpoint(cancel)
                length = min(CHUNK, size - offset)
                a, b = read_exact(old, length), read_exact(new, length)
                before_hash.update(a)
                after_hash.update(b)
                if a != b:
                    need_space(Path(destination).parent, (length // BLOCK) * RECORD_SIZE)
                    for pos in range(0, length, BLOCK):
                        if a[pos:pos+BLOCK] != b[pos:pos+BLOCK]:
                            record = struct.pack(">Q", offset + pos) + a[pos:pos+BLOCK] + b[pos:pos+BLOCK]
                            record_hash.update(record)
                            write_all(out, record)
                            count += 1
                report(progress, offset + length, size, "Finding changed physical sectors")
            digest = before_hash.hexdigest()
            if expected_original and digest != expected_original:
                raise SafetyError("The original image changed outside this application. Reopen a verified original before preparing edits.")
            if fingerprint(old) != of or fingerprint(new) != nf:
                raise SafetyError("An image changed while preparing its edit journal.")
            out.seek(0)
            header = HEADER.pack(MAGIC, size, count, before_hash.digest())
            write_all(out, header)
            report(progress, 0, 0, "Flushing edit recovery journal")
            out.flush()
            os.fsync(out.fileno())
            # Check the bytes actually saved against the bytes derived from the
            # images, before publishing a usable recovery file.
            with open(out.partial_path, "rb", buffering=0) as saved:
                if os.fstat(saved.fileno()).st_size != HEADER.size + count * RECORD_SIZE or read_exact(saved, HEADER.size) != header:
                    raise DrivePakError("Edit journal header/length verification failed.")
                body = hashlib.sha256()
                saved_hash = hashlib.sha256(header)
                length = count * RECORD_SIZE
                report(progress, 0, length, "Verifying saved edit recovery journal")
                for offset in range(0, length, CHUNK):
                    checkpoint(cancel)
                    chunk = read_exact(saved, min(CHUNK, length - offset))
                    body.update(chunk)
                    saved_hash.update(chunk)
                    report(progress, min(offset + CHUNK, length), length, "Verifying saved edit recovery journal")
                if body.digest() != record_hash.digest():
                    raise DrivePakError("Edit journal record verification failed.")
            if fingerprint(old) != of or fingerprint(new) != nf:
                raise SafetyError("An image changed while verifying its edit journal.")
        # Accidental edits through another application should fail immediately.
        Path(destination).chmod(0o400)
        return {"sectors": count, "bytes_changed": count * BLOCK, "image_bytes": size,
                "original_sha256": digest, "modified_sha256": after_hash.hexdigest(),
                "journal_sha256": saved_hash.hexdigest(),
                "journal": str(Path(destination).resolve())}


def journal_header(source, capacity):
    source.seek(0)
    magic, length, count, baseline = HEADER.unpack(read_exact(source, HEADER.size))
    if magic != MAGIC or not length or length % BLOCK or length > capacity or count > length // BLOCK:
        raise SafetyError("Invalid edit journal header or image length exceeds the selected target.")
    if os.fstat(source.fileno()).st_size != HEADER.size + count * RECORD_SIZE:
        raise SafetyError("Edit journal is truncated or has trailing data.")
    return length, count, baseline.hex()


def records(source, length, count):
    source.seek(HEADER.size)
    previous = -1
    for _ in range(count):
        offset = struct.unpack(">Q", read_exact(source, 8))[0]
        before, after = read_exact(source, BLOCK), read_exact(source, BLOCK)
        if offset % BLOCK or offset <= previous or offset + BLOCK > length or before == after:
            raise SafetyError("Edit journal has overlapping, unordered, unchanged or out-of-range records.")
        previous = offset
        yield offset, before, after


def _same_target(previous, current):
    # Kernel diskseq/path can change after unplugging; stable reader identity and
    # capacity plus the complete image-sized content comparison must still agree.
    if not isinstance(previous, dict):
        return False
    keys = ("model", "serial", "wwn", "capacity", "transport")
    if not all(previous.get(k) == current.get(k) for k in keys):
        return False
    return bool(previous.get("serial") or previous.get("wwn")) or previous == current


def compare_original(target, source, length, count, baseline, recovery, progress, cancel, guard):
    """Prove the entire prefix is original except known journal after sectors.

    A USB serial may identify only a reader. Matching just affected sectors is
    insufficient for recovery: later edits elsewhere can share filesystem state.
    Normalize known after sectors to before bytes only in this bounded RAM buffer.
    """
    phase = "Checking complete card state for recovery" if recovery else "Comparing card with editor original"
    report(progress, 0, length, phase)
    entries = records(source, length, count)
    entry = next(entries, None)
    digest = hashlib.sha256()
    target.seek(0)
    for offset in range(0, length, CHUNK):
        checkpoint(cancel)
        if guard:
            guard()
        data = bytearray(read_exact(target, min(CHUNK, length - offset)))
        end = offset + len(data)
        while entry is not None and entry[0] < end:
            position, before, after = entry
            pos = position - offset
            current = data[pos:pos + BLOCK]
            if current != before and (not recovery or current != after):
                raise SafetyError(f"Card sector {position // BLOCK} differs from the journal. No edits were written.")
            data[pos:pos + BLOCK] = before
            entry = next(entries, None)
        digest.update(data)
        report(progress, end, length, phase)
    if digest.hexdigest() != baseline:
        if recovery:
            raise SafetyError("Card has changes outside this recovery journal, or is a different card. No recovery edits were written. Use a complete verified backup for broader recovery.")
        raise SafetyError("The card differs from the editor's original image. No edits were written. Back up/open the current card before editing, or select the card matching this original.")


def apply_changes_stream(target, capacity, identity, source, recovery_ready, progress=None,
                         cancel=None, guard=None, *, acknowledge_in_place=False,
                         recovery=False, manifest=None, expected_journal=None):
    """Caller holds protected exclusive target access; durable journal already exists.

    recovery_ready must durably save the supplied manifest before returning.
    No card write occurs until it has done so. Recovery accepts a mixture of
    before/after sectors from an interrupted attempt and rejects other contents.
    """
    if acknowledge_in_place is not True:
        raise SafetyError("Applying edits requires explicit acknowledgement of non-atomic in-place card changes.")
    if type(recovery) is not bool:
        raise SafetyError("Invalid recovery mode.")
    if not os.fstat(source.fileno()).st_nlink:
        raise SafetyError("The recovery journal has been deleted. No card edits were written.")
    report(progress, 0, 0, "Flushing edit recovery journal before card access")
    os.fsync(source.fileno())
    sf = fingerprint(source)
    length, count, baseline = journal_header(source, capacity)
    if not count:
        raise SafetyError("The edit journal contains no changes.")
    digest = hash_stream(source, sf[2], progress, cancel, "Verifying edit recovery journal")
    if expected_journal is not None and digest != expected_journal:
        raise SafetyError("The edit journal changed after preparation or review. No card edits were written.")
    if fingerprint(source) != sf:
        raise SafetyError("Edit journal changed during verification.")
    if recovery:
        if not isinstance(manifest, dict) or manifest.get("kind") != "changed-sector-recovery" or manifest.get("status") != "verified" or manifest.get("journal_sha256") != digest or not _same_target(manifest.get("identity", {}), identity):
            raise SafetyError("Recovery manifest, journal digest or target identity does not match.")
    if guard:
        guard()
    if hasattr(target, "invalidate"):
        report(progress, 0, 0, "Preparing independent current-card comparison")
        target.invalidate()
    compare_original(target, source, length, count, baseline, recovery, progress, cancel, guard)
    if fingerprint(source) != sf:
        raise SafetyError("Edit journal changed before writing.")
    receipt = {"schema": 1, "kind": "changed-sector-recovery", "status": "verified",
               "identity": identity, "capacity": capacity, "image_bytes": length,
               "sectors": count, "journal_sha256": digest, "original_sha256": baseline,
               "created_utc": utcnow(), "application_version": __version__,
               "scope": "Only journaled sectors; NOT a complete physical backup."}
    recovery_ready(receipt)
    checkpoint(cancel)
    if guard:
        guard()
    if fingerprint(source) != sf:
        raise SafetyError("Edit journal changed after saving its manifest.")
    started, written, queued = False, 0, 0
    try:
        # Bounded contiguous runs reduce system calls without widening writes.
        buffer, start = bytearray(), None
        def flush_run():
            nonlocal started, written, queued, buffer, start
            if not buffer:
                return
            checkpoint(cancel)
            if guard:
                guard()
            if fingerprint(source) != sf:
                raise SafetyError("Edit journal changed during writing.")
            started = True
            target.seek(start)
            write_all(target, buffer)
            written += len(buffer)
            queued += len(buffer)
            buffer, start = bytearray(), None
            if queued >= 64 * 1024**2:
                report(progress, 0, 0, "Flushing card edits — keep card connected")
                target.flush()
                os.fsync(target.fileno())
                queued = 0
            report(progress, written, count * BLOCK, "Writing changed card sectors")
        for offset, before, after in records(source, length, count):
            if buffer and (offset != start + len(buffer) or len(buffer) >= CHUNK):
                flush_run()
            if start is None:
                start = offset
            buffer.extend(before if recovery else after)
        flush_run()
        report(progress, 0, 0, "Flushing card edits — keep card connected")
        target.flush()
        os.fsync(target.fileno())
        checkpoint(cancel)
        if hash_stream(source, sf[2], progress, cancel, "Rechecking edit recovery journal") != digest or fingerprint(source) != sf:
            raise SafetyError("Edit journal checksum changed during card writing.")
        if hasattr(target, "invalidate"):
            report(progress, 0, 0, "Preparing independent edited-sector readback")
            target.invalidate()
        result_hash = hashlib.sha256()
        for number, (offset, before, after) in enumerate(records(source, length, count), 1):
            checkpoint(cancel)
            if guard:
                guard()
            target.seek(offset)
            b = read_exact(target, BLOCK)
            if b != (before if recovery else after):
                raise DrivePakError(f"Card readback mismatch at sector {offset // BLOCK}.")
            result_hash.update(b)
            if number % 256 == 0 or number == count:
                report(progress, number * BLOCK, count * BLOCK, "Reading back edited card sectors")
        if fingerprint(source) != sf:
            raise SafetyError("Edit journal changed during card verification.")
        if guard:
            guard()
        return {"status": "verified", "sectors_written": count, "bytes_written": written,
                "sha256_written_sectors": result_hash.hexdigest(), "verified_utc": utcnow(),
                "operation": "recover-edits" if recovery else "apply-edits",
                "scope": "Only changed sectors were written; all other regions retained."}
    except BaseException as e:
        if started:
            try:
                target.flush()
                os.fsync(target.fileno())
            except OSError:
                pass
            raise DrivePakError(f"PHYSICAL EDITS INCOMPLETE OR UNVERIFIED: {e} Keep the edit journal and its verified recovery manifest. Use Recover card edits to restore its before sectors. No automatic retry occurred.") from e
        raise
