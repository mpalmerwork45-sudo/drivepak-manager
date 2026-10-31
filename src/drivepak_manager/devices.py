"""Read-only Linux discovery and fail-closed protection of real card devices."""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import stat
import subprocess

from .errors import SafetyError


def command(args):
    try:
        r = subprocess.run(args, check=True, capture_output=True, text=True, timeout=20,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        return r.stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise SafetyError(f"System device inspection failed: {e}") from e


@dataclass(frozen=True)
class Device:
    path: str
    capacity: int
    model: str
    serial: str
    wwn: str
    transport: str
    major_minor: str
    sys_path: str
    diskseq: str
    readonly: bool
    external: bool
    mounts: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    children: tuple[str, ...] = ()

    @property
    def identity(self):
        # Mount status deliberately excluded: it is separately revalidated.
        return {k: v for k, v in asdict(self).items() if k not in ("mounts", "reasons", "children")}

    @property
    def usable(self):
        return not self.reasons and not self.mounts


class Topology:
    def __init__(self, rows, sysroot=Path("/sys")):
        self.rows, self.edges, self.sysroot = {}, {}, sysroot

        def visit(row, parent=None):
            key = row["maj:min"]
            self.rows[key] = row
            self.edges.setdefault(key, set())
            if parent:
                self.connect(key, parent)
            for child in row.get("children", []):
                visit(child, key)
        for row in rows:
            visit(row)
        for key, row in self.rows.items():
            sys = sysroot / "dev/block" / key
            for group in ("slaves", "holders"):
                d = sys / group
                if d.exists():
                    for child in d.iterdir():
                        dev = child / "dev"
                        if dev.exists():
                            self.connect(key, dev.read_text().strip())

    def connect(self, a, b):
        self.edges.setdefault(a, set()).add(b)
        self.edges.setdefault(b, set()).add(a)

    def family(self, key):
        found, todo = set(), [key]
        while todo:
            node = todo.pop()
            if node not in found:
                found.add(node)
                todo.extend(self.edges.get(node, ()))
        return found

    def protected_keys(self):
        result = set()
        for key, row in self.rows.items():
            mounts = row.get("mountpoints") or []
            if any(x and (x == "[SWAP]" or x == "/" or x == "/home" or x.startswith("/boot") or x.startswith("/home/")) for x in mounts):
                result |= self.family(key)
        return result


def topology():
    data = json.loads(command(["/usr/bin/lsblk", "--json", "--bytes", "--paths", "--output",
                              "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,RM,HOTPLUG,RO,MOUNTPOINTS,MAJ:MIN"]))
    return Topology(data["blockdevices"])


def open_device_users(keys, strict=False, procroot=Path("/proc")):
    """Find existing raw block opens in addition to mount/holder claims.

    The privileged endpoint uses strict inspection. The unprivileged selector
    can only report processes visible to the caller; the endpoint rechecks.
    """
    users = []
    for process in procroot.iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
            for entry in descriptors:
                try:
                    if not os.readlink(entry).startswith("/dev/"):
                        continue
                    s = os.stat(entry)
                except FileNotFoundError:
                    continue
                if stat.S_ISBLK(s.st_mode) and f"{os.major(s.st_rdev)}:{os.minor(s.st_rdev)}" in keys:
                    name = (process / "comm").read_text().strip()
                    users.append(f"{name} (PID {process.name})")
                    break
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as e:
            if strict:
                raise SafetyError(f"Cannot establish exclusive access: process {process.name} descriptors are not inspectable.") from e
    return users


def path_devices(path, topo=None):
    """Filesystem backing, including bind mounts; missing paths use parent."""
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    s = p.stat()
    key = f"{os.major(s.st_dev)}:{os.minor(s.st_dev)}"
    topo = topo or topology()
    if key in topo.rows:
        return topo.family(key)
    # Btrfs and other multi-device mounts can report anonymous st_dev. Ask findmnt
    # for every physical source; fail closed if no mapping can be established.
    info = json.loads(command(["/usr/bin/findmnt", "--json", "--target", str(p), "--output", "MAJ:MIN,SOURCE,FSTYPE"]))
    sources = info.get("filesystems", [])
    keys = set()
    for fs in sources:
        mm = fs.get("maj:min", "")
        if mm in topo.rows:
            keys |= topo.family(mm)
        elif fs.get("fstype") in ("tmpfs", "ramfs"):
            raise SafetyError("Volatile RAM-backed storage cannot hold a durable card recovery snapshot. Choose persistent independent storage.")
        elif fs.get("fstype") in ("nfs", "nfs4", "cifs"):
            continue
        else:
            raise SafetyError(f"Cannot establish storage backing {p}; use a local independent filesystem with known backing devices.")
    return keys


def discover(topo=None, protected_paths=None):
    topo = topo or topology()
    protected = topo.protected_keys()
    # Protect system paths and all active swap including swap files. /home may
    # live below a differently named mounted volume.
    paths = ["/", "/boot", str(Path.home())] if protected_paths is None else protected_paths
    for p in paths:
        if Path(p).exists():
            protected |= path_devices(p, topo)
    if protected_paths is None:
        try:
            for line in Path("/proc/swaps").read_text().splitlines()[1:]:
                swap = line.split()[0]
                st = Path(swap).stat()
                key = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}" if stat.S_ISBLK(st.st_mode) else None
                protected |= topo.family(key) if key else path_devices(swap, topo)
        except OSError as e:
            raise SafetyError(f"Cannot inspect active swap: {e}") from e
    result = []
    for key, row in topo.rows.items():
        if row["type"] != "disk":
            continue
        family = topo.family(key)
        external = row.get("tran") == "usb" or (row.get("tran") == "mmc" and bool(row.get("rm")))
        sys = topo.sysroot / "dev/block" / key
        seqpath = sys / "diskseq"
        seq = seqpath.read_text().strip() if seqpath.exists() else ""
        mounts = sorted({m for k in family for m in (topo.rows.get(k, {}).get("mountpoints") or []) if m})
        reasons = []
        if family & protected:
            reasons.append("Backs root, boot, home, swap or another protected system path")
        if not external:
            reasons.append("Not an identified external USB reader or removable MMC card")
        if row.get("ro"):
            reasons.append("Device is read-only")
        if not seq:
            reasons.append("Kernel disk sequence is unavailable; stable device replacement checks are required")
        holders = sys / "holders"
        if holders.exists() and any(holders.iterdir()):
            reasons.append("Device is held by another block device (RAID/LVM/device mapper)")
        if external and protected_paths is None:
            users = open_device_users(family, strict=os.geteuid() == 0)
            if users:
                reasons.append("Raw device is open by " + ", ".join(users))
        result.append(Device(row.get("path") or row["name"], int(row["size"]), (row.get("model") or "Unknown reader").strip(),
                             row.get("serial") or "", row.get("wwn") or "", row.get("tran") or "", key,
                             str(sys.resolve()), seq, bool(row.get("ro")), external, tuple(mounts), tuple(reasons),
                             tuple(sorted(topo.rows[k].get("path", topo.rows[k]["name"]) for k in family if k != key and k in topo.rows))))
    return result


def validate(expected, storage_paths=(), storage_fds=()):
    topo = topology()
    candidates = discover(topo)
    found = next((d for d in candidates if d.path == expected["path"]), None)
    if found is None or found.identity != expected:
        raise SafetyError("Card identity/capacity changed or device was replaced. Refresh and select it again.")
    if not found.usable:
        raise SafetyError("Card is in use or unsafe: " + "; ".join((*found.reasons, *found.mounts)))
    related = topo.family(found.major_minor)
    for path in storage_paths:
        if related & path_devices(path, topo):
            raise SafetyError("Backup/source/working storage is backed by the target card. Choose independent storage.")
    for fd in storage_fds:
        st = os.fstat(fd)
        key = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        if key not in topo.rows:
            # /proc/self/fd resolves the actual open file after path changes.
            keys = path_devices(f"/proc/self/fd/{fd}", topo)
        else:
            keys = topo.family(key)
        if related & keys:
            raise SafetyError("An open source/backup descriptor is backed by the target card.")
    return found


def unmount(device):
    """Explicit UI action only. UDisks supplies per-session authentication."""
    current = next((d for d in discover() if d.identity == device.identity), None)
    if not current or current.reasons:
        raise SafetyError("Cannot unmount a protected or changed device.")
    topo = topology()
    for key in topo.family(device.major_minor):
        row = topo.rows.get(key)
        if row and any(row.get("mountpoints") or []):
            command(["/usr/bin/udisksctl", "unmount", "--block-device", row.get("path") or row["name"]])
    return validate(device.identity)
