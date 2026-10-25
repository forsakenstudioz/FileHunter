"""FileHunter core: drive detection, deep scanning, duplicate finding and copy/move.

No GUI code lives here, so everything can be tested on its own.
Works on Windows, macOS and Linux with only the Python standard library.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import string
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

APP_NAME = "FileHunter"
VERSION = "1.3.0"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# ----------------------------------------------------------------------------- file types
CATEGORIES: dict[str, list[str]] = {
    "Images": ["jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "heif",
               "raw", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "svg", "ico", "psd"],
    "Videos": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
               "3gp", "mts", "m2ts", "vob"],
    "Audio": ["mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "aiff", "alac", "amr", "mid"],
    "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
                  "txt", "rtf", "csv", "epub", "md"],
    "Archives": ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "cab"],
    "Programs": ["exe", "msi", "apk", "appx", "msix", "dmg", "pkg", "deb", "rpm", "appimage",
                 "bat", "cmd", "jar"],
    "Disk images": ["img", "iso", "vhd", "vhdx", "vmdk", "vdi", "qcow2", "wim", "esd", "nrg",
                    "mdf", "dmg"],
    "Code": ["py", "js", "ts", "html", "css", "java", "c", "cpp", "h", "cs", "go", "rs",
             "php", "rb", "sh", "ps1", "json", "xml", "sql", "ipynb"],
    "Fonts": ["ttf", "otf", "woff", "woff2"],
    "Ebooks": ["epub", "mobi", "azw", "azw3", "djvu", "cbz", "cbr"],
}
IMAGE_EXTS = set(CATEGORIES["Images"])


def parse_exts(text: str) -> list[str]:
    """'img, .ISO  *.mp3;tar.gz' -> ['img', 'iso', 'mp3', 'tar.gz'] (order kept, no repeats)."""
    out: list[str] = []
    for raw in text.replace(";", ",").replace(" ", ",").split(","):
        e = raw.strip().lower().lstrip("*").lstrip(".")
        if e and e not in out and all(c not in e for c in '\\/:"<>|'):
            out.append(e)
    return out


def ext_of(name: str) -> str:
    i = name.rfind(".")
    return name[i + 1:].lower() if 0 < i < len(name) - 1 else ""


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_size(text: str) -> int | None:
    """'500 MB' / '1.5gb' / '200k' / '' -> bytes or None."""
    t = (text or "").strip().lower().replace(" ", "")
    if not t:
        return None
    mult = {"tb": 1024 ** 4, "t": 1024 ** 4, "gb": 1024 ** 3, "g": 1024 ** 3, "mb": 1024 ** 2,
            "m": 1024 ** 2, "kb": 1024, "k": 1024, "b": 1}
    for suf in ("tb", "gb", "mb", "kb", "t", "g", "m", "k", "b"):
        if t.endswith(suf):
            return int(float(t[: -len(suf)]) * mult[suf])
    return int(float(t))


def human_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


# ----------------------------------------------------------------------------- settings
def config_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        d = base / APP_NAME
    elif IS_MAC:
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULTS = {
    "theme": "System",
    "categories": ["Disk images"],
    "custom_exts": "",
    "name_filter": "",
    "min_size": "",
    "max_size": "",
    "age": "Any time",
    "skip_system": True,
    "include_hidden": False,
    "dest": str(Path.home() / "FileHunter"),
    "mode": "copy",
    "layout": "flat",
    "conflict": "rename",
    "skip_identical": True,
    "verify": False,
    "unchecked_drives": [],
    "extra_folders": [],
    "geometry": "",
}


def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        data = json.loads((config_dir() / "settings.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg: dict) -> None:
    try:
        p = config_dir() / "settings.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


# ----------------------------------------------------------------------------- drives
@dataclass
class Drive:
    path: str            # "C:\\" or "/media/me/USB"
    name: str            # "C:" or "USB"
    label: str = ""      # volume label
    kind: str = "Local disk"
    fs: str = ""
    total: int = 0
    free: int = 0
    ready: bool = True
    system: bool = False
    model: str = ""

    @property
    def key(self) -> str:
        return os.path.normcase(self.path)

    @property
    def title(self) -> str:
        if self.label and self.label != self.name:
            return f"{self.name}  {self.label}"
        return self.name


_BUS = {1: "SCSI", 3: "ATA", 7: "USB", 8: "RAID", 10: "SAS", 11: "SATA", 12: "SD card",
        13: "MMC", 14: "Virtual", 15: "Virtual", 17: "NVMe"}


def _win_bus(letter: str) -> tuple[str, str]:
    """(bus, model) for a drive letter, via IOCTL_STORAGE_QUERY_PROPERTY. ('', '') on failure."""
    import ctypes
    from ctypes import wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                wt.DWORD, wt.HANDLE]
    k32.DeviceIoControl.restype = wt.BOOL
    k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                    ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wt.HANDLE]

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int),
                    ("Extra", ctypes.c_ubyte * 1)]

    class Desc(ctypes.Structure):
        _fields_ = [("Version", wt.DWORD), ("Size", wt.DWORD), ("DeviceType", ctypes.c_ubyte),
                    ("DeviceTypeModifier", ctypes.c_ubyte), ("RemovableMedia", ctypes.c_ubyte),
                    ("CommandQueueing", ctypes.c_ubyte), ("VendorIdOffset", wt.DWORD),
                    ("ProductIdOffset", wt.DWORD), ("ProductRevisionOffset", wt.DWORD),
                    ("SerialNumberOffset", wt.DWORD), ("BusType", ctypes.c_int),
                    ("RawPropertiesLength", wt.DWORD), ("Raw", ctypes.c_ubyte * 1)]

    h = k32.CreateFileW(f"\\\\.\\{letter}:", 0, 3, None, 3, 0, None)
    if not h or h == ctypes.c_void_p(-1).value:
        return "", ""
    try:
        q = Query(0, 0)
        buf = ctypes.create_string_buffer(1024)
        got = wt.DWORD(0)
        if not k32.DeviceIoControl(h, 0x2D1400, ctypes.byref(q), ctypes.sizeof(q), buf,
                                   ctypes.sizeof(buf), ctypes.byref(got), None):
            return "", ""
        d = Desc.from_buffer_copy(buf.raw[: ctypes.sizeof(Desc)])

        def s(off: int) -> str:
            if not off or off >= got.value:
                return ""
            return buf.raw[off: buf.raw.index(b"\0", off)].decode("ascii", "ignore").strip()

        model = " ".join(x for x in (s(d.VendorIdOffset), s(d.ProductIdOffset)) if x)
        return _BUS.get(d.BusType, ""), model
    except Exception:  # noqa: BLE001
        return "", ""
    finally:
        k32.CloseHandle(h)


def _windows_drives() -> list[Drive]:
    import ctypes

    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(0x0001 | 0x8000)  # no "insert a disk" pop-ups for empty card readers
    mask = k32.GetLogicalDrives()
    sysdrive = (os.environ.get("SystemDrive") or "C:")[:1].upper()
    kinds = {2: "Removable", 3: "Local disk", 4: "Network", 5: "CD/DVD", 6: "RAM disk"}
    out = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not (mask >> i) & 1:
            continue
        root = f"{letter}:\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype in (0, 1):
            continue
        name_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        ready = bool(k32.GetVolumeInformationW(root, name_buf, 261, None, None, None, fs_buf, 261))
        d = Drive(path=root, name=f"{letter}:", label=name_buf.value, fs=fs_buf.value,
                  kind=kinds.get(dtype, "Drive"), ready=ready, system=letter == sysdrive)
        if ready:
            try:
                u = shutil.disk_usage(root)
                d.total, d.free = u.total, u.free
            except OSError:
                d.ready = False
        if dtype in (2, 3) and ready:
            bus, model = _win_bus(letter)
            d.model = model
            if bus == "USB":
                d.kind = "USB drive" if dtype == 2 else "USB disk"
            elif bus == "SD card":
                d.kind = "SD card"
            elif bus and dtype == 3:
                d.kind = f"Local disk · {bus}"
        out.append(d)
    return out


_REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "ntfs", "ntfs3",
            "fuseblk", "vfat", "msdos", "exfat", "hfs", "hfsplus", "apfs", "udf", "iso9660",
            "nfs", "nfs4", "cifs", "smb3", "sshfs", "fuse.sshfs", "9p", "bcachefs"}
_SKIP_MOUNT_PREFIX = ("/proc", "/sys", "/dev", "/snap", "/boot", "/var/lib/docker",
                      "/var/lib/snapd", "/run/user", "/var/snap")


def _unescape_mount(p: str) -> str:
    return p.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _linux_kind(dev: str, mnt: str, fs: str) -> str:
    if fs in ("nfs", "nfs4", "cifs", "smb3", "sshfs", "fuse.sshfs", "9p"):
        return "Network"
    if fs in ("iso9660", "udf"):
        return "CD/DVD"
    base = os.path.basename(os.path.realpath(dev)) if dev.startswith("/dev/") else ""
    disk = base
    if base:
        for prefix in ("nvme", "mmcblk"):
            if base.startswith(prefix) and "p" in base[len(prefix):]:
                disk = base[: base.rfind("p")]
                break
        else:
            disk = base.rstrip("0123456789")
    try:
        sys_path = os.path.realpath(f"/sys/block/{disk}")
        if "/usb" in sys_path:
            return "USB drive"
        if disk.startswith("mmcblk"):
            return "SD card"
        with open(f"/sys/block/{disk}/removable") as f:
            if f.read().strip() == "1":
                return "Removable"
    except OSError:
        pass
    if mnt.startswith(("/media/", "/run/media/", "/mnt/")):
        return "Mounted disk"
    return "Local disk"


def _linux_drives() -> list[Drive]:
    labels = {}
    try:
        for lab in os.listdir("/dev/disk/by-label"):
            labels[os.path.realpath(f"/dev/disk/by-label/{lab}")] = _unescape_mount(
                lab.replace("\\x20", " "))
    except OSError:
        pass
    out, seen = [], set()
    try:
        lines = open("/proc/self/mounts", encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        lines = ["/ / ext4 rw 0 0"]
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = parts[0], _unescape_mount(parts[1]), parts[2]
        if fs not in _REAL_FS and mnt != "/":
            continue
        if mnt != "/" and mnt.startswith(_SKIP_MOUNT_PREFIX):
            continue
        if mnt.startswith("/run/") and not mnt.startswith("/run/media/"):
            continue
        try:
            st = os.stat(mnt)
            u = shutil.disk_usage(mnt)
        except OSError:
            continue
        if st.st_dev in seen:  # bind mounts / same filesystem twice
            continue
        seen.add(st.st_dev)
        real_dev = os.path.realpath(dev) if dev.startswith("/dev/") else dev
        name = "/" if mnt == "/" else os.path.basename(mnt.rstrip("/")) or mnt
        d = Drive(path=mnt, name=name, label=labels.get(real_dev, ""), fs=fs, total=u.total,
                  free=u.free, kind=_linux_kind(dev, mnt, fs), system=mnt == "/")
        if mnt == "/":
            d.label = d.label or "System"
        out.append(d)
    return out


def _mac_drives() -> list[Drive]:
    out = []
    root_dev = os.stat("/").st_dev
    data_dev = None
    if os.path.isdir("/System/Volumes/Data"):
        data_dev = os.stat("/System/Volumes/Data").st_dev
    u = shutil.disk_usage("/")
    out.append(Drive(path="/", name="Macintosh HD", label="", kind="Local disk", fs="apfs",
                     total=u.total, free=u.free, system=True))
    try:
        vols = sorted(os.listdir("/Volumes"))
    except OSError:
        vols = []
    for v in vols:
        p = os.path.join("/Volumes", v)
        try:
            st = os.stat(p)
            if st.st_dev in (root_dev, data_dev) or not os.path.isdir(p):
                continue
            u = shutil.disk_usage(p)
        except OSError:
            continue
        kind = "External disk"
        if v.lower().startswith(("time machine", "backups of")):
            kind = "Backup disk"
        out.append(Drive(path=p, name=v, kind=kind, total=u.total, free=u.free))
    return out


def list_drives() -> list[Drive]:
    """Every drive, partition, pendrive, SD card, CD and network share the OS has mounted."""
    try:
        if IS_WIN:
            return _windows_drives()
        if IS_MAC:
            return _mac_drives()
        return _linux_drives()
    except Exception:  # noqa: BLE001 - never let detection crash the app
        home = str(Path.home())
        u = shutil.disk_usage(home)
        return [Drive(path=home, name="Home", total=u.total, free=u.free)]


def folder_as_drive(path: str) -> Drive:
    p = os.path.abspath(path)
    try:
        u = shutil.disk_usage(p)
        total, free = u.total, u.free
    except OSError:
        total = free = 0
    return Drive(path=p, name=os.path.basename(p.rstrip("\\/")) or p, label="", kind="Folder",
                 total=total, free=free)


# ----------------------------------------------------------------------------- scanning
WIN_SKIP_AT_ROOT = {"windows", "program files", "program files (x86)", "programdata", "recovery",
                    "$windows.~bt", "$windows.~ws", "$winreagent", "$sysreset", "config.msi",
                    "msocache", "perflogs", "intel", "amd", "nvidia", "onedrivetemp", "$getcurrent"}
SKIP_ANYWHERE = {"$recycle.bin", "system volume information", "appdata", "windowsapps",
                 "node_modules", ".git", ".svn", ".hg", "__pycache__", ".trash", ".trashes",
                 ".spotlight-v100", ".fseventsd", ".documentrevisions-v100", ".temporaryitems",
                 "$recycle", "recycler", "found.000", ".cache", ".venv", "site-packages",
                 "lost+found"}
POSIX_SYSTEM_PATHS = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/usr", "/lib",
                      "/lib32", "/lib64", "/libx32", "/bin", "/sbin", "/etc", "/var", "/opt",
                      "/srv", "/System", "/Library", "/private", "/Volumes", "/cores",
                      "/Applications", "/nix", "/gnu")
# Always skipped even with "skip system folders" off: virtual or dangerous to read.
POSIX_NEVER = ("/proc", "/sys", "/dev", "/run", "/System/Volumes", "/private/var/vm", "/Volumes")
def looks_like_system(path: str) -> bool:
    """True for files inside Windows / Program Files / app-data style folders.

    Moving files out of these folders can break installed programs.
    """
    if IS_WIN:
        p = path.replace("\\", "/").lower()
        p = p.split(":", 1)[1] if ":" in p[:3] else p
        return p.startswith(("/windows/", "/program files", "/programdata/")) or "/appdata/" in p
    return path.startswith(tuple(s + "/" for s in POSIX_SYSTEM_PATHS)) or "/Library/" in path


class FileItem:
    __slots__ = ("path", "name", "ext", "size", "mtime", "root", "dup", "gone")

    def __init__(self, path: str, name: str, ext: str, size: int, mtime: float, root: str):
        self.path, self.name, self.ext = path, name, ext
        self.size, self.mtime, self.root = size, mtime, root
        self.dup = 0  # duplicate group number, 0 = not a duplicate
        self.gone = False  # deleted from disk

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    def __repr__(self) -> str:  # pragma: no cover
        return f"FileItem({self.path!r}, {self.size})"


@dataclass
class ScanFilters:
    exts: list[str] = field(default_factory=list)  # empty = every file
    name: str = ""                                  # "holiday*; *2023*" or plain text
    min_size: int | None = None
    max_size: int | None = None
    newer_than: float | None = None                 # unix time
    older_than: float | None = None
    skip_system: bool = True
    include_hidden: bool = False
    exclude: list[str] = field(default_factory=list)  # folders never to enter (e.g. destination)

    def name_matchers(self):
        pats = [p.strip().lower() for p in self.name.replace(",", ";").split(";") if p.strip()]
        out = []
        for p in pats:
            if any(c in p for c in "*?["):
                out.append(lambda n, p=p: fnmatch.fnmatchcase(n, p))
            else:
                out.append(lambda n, p=p: p in n)
        return out


AGE_CHOICES = {"Any time": None, "Last 24 hours": 1, "Last 7 days": 7, "Last 30 days": 30,
               "Last 6 months": 182, "Last year": 365, "Older than 1 year": -365,
               "Older than 5 years": -1826}


def age_to_filters(age: str, f: ScanFilters) -> None:
    days = AGE_CHOICES.get(age)
    if days is None:
        return
    if days > 0:
        f.newer_than = time.time() - days * 86400
    else:
        f.older_than = time.time() + days * 86400


class Scanner:
    """Walks every folder and sub-folder of the chosen roots in background threads.

    Results are collected in batches; the GUI calls take_batch() as often as it likes.
    """

    def __init__(self, roots: list[str], filters: ScanFilters, threads: int = 4):
        self.roots = [os.path.abspath(r) for r in roots]
        self.f = filters
        self.threads = max(1, min(threads, len(self.roots) or 1))
        self.stop_ev = threading.Event()
        self.run_ev = threading.Event()
        self.run_ev.set()
        self._lock = threading.Lock()
        self._batch: list[FileItem] = []
        self.dirs = 0
        self.files_seen = 0
        self.found = 0
        self.found_bytes = 0
        self.errors = 0
        self.error_samples: list[str] = []
        self.current = ""
        self.done = False
        self.started = 0.0
        self.finished = 0.0
        self._suffixes = tuple("." + e for e in filters.exts)
        self._name_ok = filters.name_matchers()
        self._exclude = {os.path.normcase(os.path.abspath(e)) for e in filters.exclude if e}

    # -- control
    def start(self) -> None:
        self.started = time.time()
        queue = list(self.roots)
        qlock = threading.Lock()

        def worker():
            while not self.stop_ev.is_set():
                with qlock:
                    if not queue:
                        return
                    root = queue.pop(0)
                self._walk(root)

        self._workers = [threading.Thread(target=worker, daemon=True) for _ in range(self.threads)]
        for t in self._workers:
            t.start()
        threading.Thread(target=self._wait_all, daemon=True).start()

    def _wait_all(self) -> None:
        for t in self._workers:
            t.join()
        self.finished = time.time()
        self.done = True

    def pause(self) -> None:
        self.run_ev.clear()

    def resume(self) -> None:
        self.run_ev.set()

    @property
    def paused(self) -> bool:
        return not self.run_ev.is_set()

    def stop(self) -> None:
        self.stop_ev.set()
        self.run_ev.set()

    def take_batch(self) -> list[FileItem]:
        with self._lock:
            b, self._batch = self._batch, []
        return b

    def run_blocking(self) -> list[FileItem]:
        """Convenience for tests and scripts."""
        self.start()
        out = []
        while not self.done:
            time.sleep(0.02)
            out += self.take_batch()
        return out + self.take_batch()

    # -- walking
    def _error(self, msg: str) -> None:
        with self._lock:
            self.errors += 1
            if len(self.error_samples) < 300:
                self.error_samples.append(msg)

    def _devs_for(self, root: str) -> set | None:
        """Filesystems we may enter from this root (posix: don't wander into other drives)."""
        if IS_WIN:
            return None
        try:
            devs = {os.stat(root).st_dev}
        except OSError:
            return None
        if IS_MAC and root == "/" and os.path.isdir("/System/Volumes/Data"):
            devs.add(os.stat("/System/Volumes/Data").st_dev)  # /Users lives here on modern macOS
        return devs

    def _skip_dir(self, path: str, name: str, depth: int, is_root_slash: bool) -> bool:
        low = name.lower()
        if os.path.normcase(path) in self._exclude:
            return True
        if not IS_WIN and is_root_slash and path.startswith(POSIX_NEVER):
            return True
        if not self.f.skip_system:
            return False
        if low in SKIP_ANYWHERE:
            return True
        if IS_WIN and depth == 0 and low in WIN_SKIP_AT_ROOT and len(os.path.dirname(path)) <= 3:
            return True
        if not IS_WIN and is_root_slash and depth == 0 and path in POSIX_SYSTEM_PATHS:
            return True
        return False

    def _hidden(self, entry: os.DirEntry, st) -> bool:
        if entry.name.startswith("."):
            return True
        if IS_WIN and st is not None:
            return bool(getattr(st, "st_file_attributes", 0) & 0x6)  # hidden | system
        return False

    def _walk(self, root: str) -> None:
        f = self.f
        devs = self._devs_for(root)
        is_root_slash = root == "/"
        stack: list[tuple[str, int]] = [(root, 0)]
        suffixes, name_ok = self._suffixes, self._name_ok
        local: list[FileItem] = []
        last_flush = time.time()
        while stack:
            if self.stop_ev.is_set():
                break
            if not self.run_ev.is_set():
                self._flush(local)
                local = []
                self.run_ev.wait()
            path, depth = stack.pop()
            self.current = path
            try:
                it = os.scandir(_long(path))
            except OSError as e:  # unreadable folder: note it and carry on
                self._error(f"{path}: {e.strerror or e}")
                continue
            with self._lock:
                self.dirs += 1
            subdirs = []
            seen = 0
            with it:
                for entry in it:
                    try:
                        is_link = entry.is_symlink() or (hasattr(entry, "is_junction")
                                                          and entry.is_junction())
                        if is_link:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            epath = os.path.join(path, entry.name)
                            if not f.include_hidden:
                                st = entry.stat(follow_symlinks=False) if IS_WIN else None
                                if self._hidden(entry, st):
                                    continue
                            if self._skip_dir(epath, entry.name, depth, is_root_slash):
                                continue
                            if devs is not None:
                                try:
                                    if entry.stat(follow_symlinks=False).st_dev not in devs:
                                        continue
                                except OSError:
                                    continue
                            subdirs.append(epath)
                            continue
                        seen += 1
                        name = entry.name
                        low = name.lower()
                        if suffixes and not low.endswith(suffixes):
                            continue
                        if name_ok and not any(m(low) for m in name_ok):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        if not f.include_hidden and self._hidden(entry, st):
                            continue
                        size = st.st_size
                        if f.min_size is not None and size < f.min_size:
                            continue
                        if f.max_size is not None and size > f.max_size:
                            continue
                        mt = st.st_mtime
                        if f.newer_than is not None and mt < f.newer_than:
                            continue
                        if f.older_than is not None and mt > f.older_than:
                            continue
                        local.append(FileItem(os.path.join(path, name), name, ext_of(name), size,
                                              mt, root))
                    except OSError as e:
                        self._error(f"{os.path.join(path, entry.name)}: {e.strerror or e}")
            for sd in reversed(subdirs):
                stack.append((sd, depth + 1))
            with self._lock:
                self.files_seen += seen
            if local and (len(local) >= 200 or time.time() - last_flush > 0.25):
                self._flush(local)
                local = []
                last_flush = time.time()
        self._flush(local)

    def _flush(self, items: list[FileItem]) -> None:
        if not items:
            return
        with self._lock:
            self._batch.extend(items)
            self.found += len(items)
            self.found_bytes += sum(i.size for i in items)


def _long(path: str) -> str:
    """Windows: allow paths longer than 260 characters."""
    if IS_WIN and len(path) > 240 and not path.startswith("\\\\?\\"):
        p = os.path.abspath(path)
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return path


# ----------------------------------------------------------------------------- duplicates
def _hash_file(path: str, stop: threading.Event | None, quick: bool = False,
               progress=None) -> str | None:
    h = hashlib.blake2b(digest_size=20)
    try:
        with open(_long(path), "rb") as fh:
            if quick:
                h.update(fh.read(65536))
                try:
                    fh.seek(-65536, os.SEEK_END)
                except OSError:
                    pass
                h.update(fh.read(65536))
            else:
                while True:
                    if stop is not None and stop.is_set():
                        return None
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                    if progress:
                        progress(len(chunk))
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(items: list[FileItem], stop: threading.Event | None = None,
                    progress=None) -> list[list[FileItem]]:
    """Group files with identical content. Sets item.dup = group number (1, 2, ...).

    progress(done_bytes, total_bytes, stage) is called from time to time.
    """
    for it in items:
        it.dup = 0
    by_size: dict[int, list[FileItem]] = {}
    for it in items:
        if it.size > 0:
            by_size.setdefault(it.size, []).append(it)
    cands = [g for g in by_size.values() if len(g) > 1]
    # drop the same path seen twice (overlapping scan roots)
    cands = [list({os.path.normcase(i.path): i for i in g}.values()) for g in cands]
    cands = [g for g in cands if len(g) > 1]
    total = sum(i.size for g in cands for i in g)
    done = [0]

    def bump(n):
        done[0] += n
        if progress:
            progress(done[0], total, "Comparing contents")

    groups: list[list[FileItem]] = []
    for g in cands:
        if stop is not None and stop.is_set():
            break
        quick: dict[str, list[FileItem]] = {}
        for it in g:
            q = _hash_file(it.path, stop, quick=True)
            if q:
                quick.setdefault(q, []).append(it)
        for qg in quick.values():
            if len(qg) < 2:
                for it in qg:
                    bump(it.size)
                continue
            if qg[0].size <= 131072:  # quick hash already covered the whole file
                for it in qg:
                    bump(it.size)
                groups.append(qg)
                continue
            full: dict[str, list[FileItem]] = {}
            for it in qg:
                d = _hash_file(it.path, stop, progress=bump)
                if d:
                    full.setdefault(d, []).append(it)
            groups.extend(x for x in full.values() if len(x) > 1)
    groups.sort(key=lambda g: (-g[0].size, g[0].name.lower()))
    for n, g in enumerate(groups, 1):
        g.sort(key=lambda i: (i.mtime, i.path.lower()))  # oldest first = the "original"
        for it in g:
            it.dup = n
    return groups


# ----------------------------------------------------------------------------- copy / move
LAYOUTS = {
    "flat": "All in one folder",
    "by_type": "Sub-folder per file type (MP3, JPG …)",
    "by_date": "Sub-folder per month (2024-05 …)",
    "keep": "Keep original folder structure",
}
CONFLICTS = {
    "rename": "Keep both (add a number)",
    "skip": "Skip the file",
    "replace": "Replace it",
    "newer": "Replace only if newer",
}


def _safe_part(s: str) -> str:
    bad = '<>:"/\\|?*'
    s = "".join("_" if c in bad or ord(c) < 32 else c for c in s).strip(" .")
    return s or "_"


def target_path(item: FileItem, dest: str, layout: str, drive_names: dict | None = None) -> str:
    if layout == "by_type":
        sub = (item.ext or "no extension").upper()
        return os.path.join(dest, _safe_part(sub), item.name)
    if layout == "by_date":
        sub = datetime.fromtimestamp(item.mtime).strftime("%Y-%m") if item.mtime > 0 else "unknown"
        return os.path.join(dest, sub, item.name)
    if layout == "keep":
        root = item.root
        rel = os.path.relpath(item.path, root)
        top = (drive_names or {}).get(os.path.normcase(root))
        if not top:
            top = root.rstrip("\\/").replace(":", "") or "root"
            top = os.path.basename(top) or top
            if IS_WIN and len(root) <= 3:
                top = f"Drive {root[0].upper()}"
            elif root == "/":
                top = "System"
        parts = [_safe_part(p) for p in rel.split(os.sep) if p not in ("", ".", "..")]
        return os.path.join(dest, _safe_part(top), *parts)
    return os.path.join(dest, item.name)


def numbered(path: str) -> str:
    base, ext = os.path.splitext(path)
    n = 2
    while True:
        cand = f"{base} ({n}){ext}"
        if not os.path.exists(cand):
            return cand
        n += 1


def same_content(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    qa, qb = _hash_file(a, None, quick=True), _hash_file(b, None, quick=True)
    if qa is None or qa != qb:
        return False
    if os.path.getsize(a) <= 131072:
        return True
    return _hash_file(a, None) == _hash_file(b, None)


def same_device(a: str, b_dir: str) -> bool:
    try:
        d = b_dir
        while not os.path.exists(d):
            nd = os.path.dirname(d)
            if nd == d:
                return False
            d = nd
        if IS_WIN:
            return os.path.splitdrive(os.path.abspath(a))[0].lower() == \
                os.path.splitdrive(os.path.abspath(d))[0].lower()
        return os.stat(a).st_dev == os.stat(d).st_dev
    except OSError:
        return False


def bytes_to_write(items: list[FileItem], dest: str, mode: str) -> int:
    """Space needed at the destination (moves on the same drive need none)."""
    total = 0
    for it in items:
        if mode == "move" and same_device(it.path, dest):
            continue
        total += it.size
    return total


def inside(child: str, parent: str) -> bool:
    c = os.path.normcase(os.path.abspath(child))
    p = os.path.normcase(os.path.abspath(parent)).rstrip("\\/") + os.sep
    return c.startswith(p)


RESULT_LABELS = {"ok": "Done", "failed": "Failed", "skipped": "Skipped",
                 "identical": "Already there", "partial": "Copied, original kept",
                 "notreached": "Not copied (stopped)"}


def friendly_error(e: BaseException) -> str:
    """Turn an OS error into a sentence a person understands."""
    import errno
    w = getattr(e, "winerror", None)
    no = getattr(e, "errno", None)
    if w in (32, 33):
        return "the file is open in another program"
    if w in (39, 112) or no == errno.ENOSPC:
        return "the destination drive is full"
    if w == 5 or isinstance(e, PermissionError):
        return "no permission (the file or folder is protected or read-only)"
    if w in (2, 3) or isinstance(e, FileNotFoundError):
        return "the file or folder no longer exists"
    if w == 206 or no == errno.ENAMETOOLONG:
        return "the name or path is too long"
    if w in (21, 23, 1117, 1167) or no in (errno.EIO, errno.ENODEV, getattr(errno, "ENXIO", -1)):
        return "the drive could not be read or written (unplugged or damaged?)"
    if w == 123:
        return "the file name contains characters the destination does not allow"
    if isinstance(e, OSError):
        return e.strerror or str(e)
    return str(e)


class Transfer:
    """Copies or moves files with live progress, pause/stop, verification and a journal."""

    def __init__(self, items: list[FileItem], dest: str, mode: str = "copy", layout: str = "flat",
                 conflict: str = "rename", skip_identical: bool = True, verify: bool = False,
                 drive_names: dict | None = None, subfolders: list[str] | None = None):
        """subfolders: one folder name per item (e.g. per person); overrides the layout."""
        self.items = list(items)
        self.subfolders = list(subfolders) if subfolders else None
        self.dest = os.path.abspath(dest)
        self.mode, self.layout, self.conflict = mode, layout, conflict
        self.skip_identical, self.verify = skip_identical, verify
        self.drive_names = drive_names or {}
        self.stop_ev = threading.Event()
        self.run_ev = threading.Event()
        self.run_ev.set()
        self.total_bytes = sum(i.size for i in self.items)
        self.done_bytes = 0
        self.index = 0
        self.current = ""
        self.cur_done = 0
        self.cur_size = 0
        self.journal: list[dict] = []
        self.counts = {"ok": 0, "skipped": 0, "identical": 0, "failed": 0, "partial": 0}
        self.done = False
        self.started = self.finished = 0.0
        self.log: list[str] = []
        self.report: list[dict] = []
        self._by_size: dict[int, list[str]] = {}   # everything already in the destination
        self._hash_cache: dict[tuple[str, bool], str | None] = {}

    # -- "is this file already somewhere in the destination?"
    def _index_destination(self):
        for root, _dirs, files in os.walk(_long(self.dest)):
            for name in files:
                if name.endswith(".fhpart"):
                    continue
                p = os.path.join(root, name)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size > 0:
                    self._by_size.setdefault(size, []).append(p)

    def _hash(self, path: str, quick: bool) -> str | None:
        key = (path, quick)
        if key not in self._hash_cache:
            self._hash_cache[key] = _hash_file(path, self.stop_ev, quick=quick)
        return self._hash_cache[key]

    def _find_identical(self, src: str, size: int, within: str | None = None) -> str | None:
        """Path of a file in the destination with exactly the same contents, if any.

        within: only look in this folder (used when sorting into per-person folders, where the
        same photo may rightly go into several folders)."""
        cands = [p for p in self._by_size.get(size, ())
                 if os.path.normcase(p) != os.path.normcase(_long(src))]
        if within is not None:
            w = os.path.normcase(_long(within))
            cands = [p for p in cands if os.path.normcase(os.path.dirname(p)) == w]
        if not cands:
            return None
        q = self._hash(_long(src), True)
        if q is None:
            return None
        for c in cands:
            if self._hash(c, True) != q:
                continue
            if size <= 131072 or self._hash(c, False) == self._hash(_long(src), False):
                return c
        return None

    def pause(self):
        self.run_ev.clear()

    def resume(self):
        self.run_ev.set()

    @property
    def paused(self):
        return not self.run_ev.is_set()

    def stop(self):
        self.stop_ev.set()
        self.run_ev.set()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def _note(self, entry: dict):
        self.journal.append(entry)
        st = entry["status"]
        key = {"ok": "ok", "skipped": "skipped", "identical": "identical",
               "partial": "partial"}.get(st, "failed")
        self.counts[key] += 1
        if st != "ok":
            self.log.append(f"{st.upper()}: {entry['src']}  {entry.get('msg', '')}".rstrip())

    def run(self):
        self.started = time.time()
        os.makedirs(self.dest, exist_ok=True)
        if self.skip_identical:
            self.current = "Checking what is already in the destination…"
            self._index_destination()
        for idx, it in enumerate(self.items):
            self._idx = idx
            self.index = idx
            if self.stop_ev.is_set():
                break
            self.run_ev.wait()
            self.current, self.cur_done, self.cur_size = it.path, 0, it.size
            entry = {"op": self.mode, "src": it.path, "dst": "", "size": it.size, "status": "",
                     "msg": "", "sub": self.subfolders[idx] if self.subfolders else ""}
            try:
                self._one(it, entry)
            except Exception as e:  # noqa: BLE001
                entry["status"], entry["msg"] = "failed", friendly_error(e)
            if entry["status"] == "stopped":
                break
            self._note(entry)
            if self.skip_identical and entry["status"] in ("ok", "partial") and entry["dst"] \
                    and it.size > 0:
                self._by_size.setdefault(it.size, []).append(_long(entry["dst"]))
            self.done_bytes = sum(i.size for i in self.items[: idx + 1])
        # files never reached (Stop pressed) belong in the report too
        reached = len(self.journal)
        self.report = [dict(e) for e in self.journal] + [
            {"op": self.mode, "src": it.path, "dst": "", "size": it.size, "status": "notreached",
             "msg": "not copied: stopped before this file was reached",
             "sub": self.subfolders[n] if self.subfolders else ""}
            for n, it in enumerate(self.items) if n >= reached]
        self.finished = time.time()
        self.done = True
        save_history(self)

    def _one(self, it: FileItem, entry: dict):
        src = it.path
        if not os.path.exists(_long(src)):
            entry["status"], entry["msg"] = "failed", "file no longer exists"
            return
        if self.subfolders:
            dst = os.path.join(self.dest, _safe_part(self.subfolders[self._idx]), it.name)
        else:
            dst = target_path(it, self.dest, self.layout, self.drive_names)
        if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
            entry["status"], entry["msg"], entry["dst"] = "skipped", "already in place", dst
            return
        if self.skip_identical and it.size > 0:
            same = self._find_identical(src, it.size,
                                        os.path.dirname(dst) if self.subfolders else None)
            if same:
                entry["status"], entry["dst"] = "identical", same
                entry["msg"] = f"same file already in the destination: {os.path.relpath(same, _long(self.dest))}"
                return
        os.makedirs(_long(os.path.dirname(dst)), exist_ok=True)
        if os.path.exists(_long(dst)):
            if self.skip_identical and it.size == 0 and same_content(src, dst):
                entry["status"], entry["msg"], entry["dst"] = "identical", \
                    "identical file already there", dst
                return
            if self.conflict == "skip":
                entry["status"], entry["msg"], entry["dst"] = "skipped", "name already used", dst
                return
            if self.conflict == "newer":
                try:
                    if os.path.getmtime(src) <= os.path.getmtime(dst):
                        entry["status"], entry["msg"], entry["dst"] = "skipped", \
                            "existing file is newer", dst
                        return
                except OSError:
                    pass
            if self.conflict == "rename":
                dst = numbered(dst)
            else:
                entry["replaced"] = True
        entry["dst"] = dst

        if self.mode == "move" and same_device(src, os.path.dirname(dst)):
            try:
                os.replace(_long(src), _long(dst))  # instant: just changes the folder entry
                self.cur_done = it.size
                entry["status"] = "ok"
                return
            except OSError:
                pass  # e.g. really another disk mounted in a folder: copy, then delete

        part = dst + ".fhpart"
        try:
            self._copy_data(src, part)
        except _Stopped:
            _quiet_remove(part)
            entry["status"] = "stopped"
            return
        except Exception:
            _quiet_remove(part)
            raise
        try:
            shutil.copystat(_long(src), _long(part))
        except OSError:
            pass
        if os.path.getsize(_long(part)) != os.path.getsize(_long(src)):
            _quiet_remove(part)
            entry["status"], entry["msg"] = "failed", "size mismatch after copy"
            return
        if self.verify and _hash_file(src, None) != _hash_file(part, None):
            _quiet_remove(part)
            entry["status"], entry["msg"] = "failed", "verification failed (contents differ)"
            return
        os.replace(_long(part), _long(dst))
        if self.mode == "move":
            try:
                os.chmod(_long(src), stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            try:
                os.remove(_long(src))
            except OSError as e:
                entry["status"], entry["msg"] = "partial", \
                    f"copied, but original could not be removed ({e.strerror or e})"
                entry["op"] = "copy"
                return
        entry["status"] = "ok"

    def _copy_data(self, src: str, dst: str):
        with open(_long(src), "rb") as fi, open(_long(dst), "wb") as fo:
            while True:
                if self.stop_ev.is_set():
                    raise _Stopped()
                if not self.run_ev.is_set():
                    self.run_ev.wait()
                buf = fi.read(4 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)
                self.cur_done += len(buf)

    @property
    def bytes_now(self) -> int:
        return self.done_bytes + (self.cur_done if not self.done else 0)


class _Stopped(Exception):
    pass


def _quiet_remove(p: str):
    try:
        os.remove(_long(p))
    except OSError:
        pass


# ----------------------------------------------------------------------------- history / undo
def _history_file() -> Path:
    return config_dir() / "history.json"


def load_history() -> list[dict]:
    try:
        data = json.loads(_history_file().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_history(t: Transfer) -> None:
    done = [e for e in t.journal if e["status"] in ("ok", "partial") and e.get("dst")]
    if not done:
        return
    runs = load_history()
    runs.append({"time": datetime.now().isoformat(timespec="seconds"), "mode": t.mode,
                 "dest": t.dest, "entries": done, "undone": False})
    runs = runs[-20:]
    try:
        p = _history_file()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def last_undoable() -> dict | None:
    for run in reversed(load_history()):
        if not run.get("undone"):
            return run
    return None


def undo_run(run: dict) -> dict:
    """Moves files back where they came from; removes copies that were made."""
    res = {"restored": 0, "removed": 0, "failed": 0, "messages": []}
    for e in reversed(run["entries"]):
        src, dst = e["src"], e["dst"]
        try:
            if e["op"] == "move" and not e.get("replaced"):
                if not os.path.exists(_long(dst)):
                    raise OSError("file is no longer in the destination")
                if os.path.exists(_long(src)):
                    raise OSError("a file with the original name is back in the original place")
                os.makedirs(_long(os.path.dirname(src)), exist_ok=True)
                shutil.move(_long(dst), _long(src))
                res["restored"] += 1
            elif e["op"] == "copy" and not e.get("replaced"):
                if os.path.exists(_long(dst)):
                    if os.path.getsize(_long(dst)) != e["size"]:
                        raise OSError("the copy was changed since, left in place")
                    os.remove(_long(dst))
                res["removed"] += 1
            else:
                raise OSError("replaced an older file, can't be undone automatically")
        except OSError as ex:
            res["failed"] += 1
            res["messages"].append(f"{dst}: {ex.strerror or ex}")
            continue
        # tidy up folders we created that are now empty
        d = os.path.dirname(dst)
        dest = run["dest"]
        while inside(d, dest):
            try:
                os.rmdir(_long(d))
            except OSError:
                break
            d = os.path.dirname(d)
    runs = load_history()
    for r in runs:
        if r.get("time") == run.get("time") and r.get("dest") == run.get("dest"):
            r["undone"] = True
    try:
        _history_file().write_text(json.dumps(runs, indent=1), encoding="utf-8")
    except OSError:
        pass
    return res


# ----------------------------------------------------------------------------- delete
TRASH_NAME = "Recycle Bin" if IS_WIN else "Trash"


def _win_recycle(paths: list[str]) -> None:
    """Send files to the Windows Recycle Bin in one shell call."""
    import ctypes
    from ctypes import wintypes as wt

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", ctypes.c_void_p),
                    ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wt.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wt.LPCWSTR)]

    shell = ctypes.WinDLL("shell32")
    shell.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell.SHFileOperationW.restype = ctypes.c_int
    # list of full paths, each ended by \0, whole list ended by an extra \0
    buf = ctypes.create_unicode_buffer("\0".join(os.path.abspath(p) for p in paths) + "\0\0")
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    # ALLOWUNDO = recycle; SILENT/NOCONFIRMATION/NOERRORUI = no pop-ups.
    op.fFlags = 0x0040 | 0x0004 | 0x0010 | 0x0400
    rc = shell.SHFileOperationW(ctypes.byref(op))
    if rc and rc != 0x4C7:  # 0x4C7 = cancelled
        raise OSError(f"Windows could not recycle the files (code {rc:#x})")


def _mac_trash(paths: list[str]) -> None:
    def q(p):
        return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'
    files = ", ".join(f"POSIX file {q(os.path.abspath(p))}" for p in paths)
    import subprocess
    r = subprocess.run(["osascript", "-e", f'tell application "Finder" to delete {{{files}}}'],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:  # Finder refused (e.g. no permission): move into ~/.Trash ourselves
        trash = Path.home() / ".Trash"
        for p in paths:
            if os.path.exists(p):
                dst = trash / os.path.basename(p)
                shutil.move(p, numbered(str(dst)) if dst.exists() else str(dst))


def _mount_point(path: str) -> str:
    path = os.path.abspath(path)
    dev = os.stat(path).st_dev
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return path
        try:
            if os.stat(parent).st_dev != dev:
                return path
        except OSError:
            return path
        path = parent


def _linux_trash_one(path: str) -> None:
    """freedesktop.org Trash: the file can be restored from the file manager's Trash."""
    from urllib.parse import quote
    path = os.path.abspath(path)
    home_trash = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"
    home_trash.mkdir(parents=True, exist_ok=True)
    fdev = os.stat(path).st_dev
    if os.stat(home_trash).st_dev == fdev:
        trash, shown = home_trash, path
    else:
        top = _mount_point(path)
        trash = Path(top) / f".Trash-{os.getuid()}"
        trash.mkdir(mode=0o700, exist_ok=True)
        shown = os.path.relpath(path, top)
    (trash / "files").mkdir(exist_ok=True)
    (trash / "info").mkdir(exist_ok=True)
    name = os.path.basename(path)
    base, ext = os.path.splitext(name)
    n = 1
    while True:
        cand = name if n == 1 else f"{base}.{n}{ext}"
        try:
            fd = os.open(trash / "info" / (cand + ".trashinfo"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w") as fh:
        fh.write(f"[Trash Info]\nPath={quote(shown)}\n"
                 f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        os.rename(path, trash / "files" / cand)
    except OSError:
        os.remove(trash / "info" / (cand + ".trashinfo"))
        raise


def can_recycle(path: str, drives: list | None = None) -> bool:
    """False where Windows deletes straight away instead of recycling (pendrives, network)."""
    if not IS_WIN:
        return True
    for d in drives or []:
        if os.path.normcase(path).startswith(os.path.normcase(d.path)) and \
                d.kind in ("Removable", "USB drive", "SD card", "Network"):
            return False
    return not path.startswith("\\\\")


class Deleter:
    """Deletes files in the background: to the Recycle Bin / Trash, or permanently."""

    def __init__(self, paths: list[str], permanent: bool = False):
        self.paths = list(paths)
        self.permanent = permanent
        self.done_count = 0
        self.results: dict[str, str] = {}  # path -> "" (deleted) or error text
        self.stop_ev = threading.Event()
        self.done = False

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    def run(self):
        batch = 50 if IS_WIN or IS_MAC else 1
        try:
            for i in range(0, len(self.paths), batch):
                if self.stop_ev.is_set():
                    break
                chunk = [p for p in self.paths[i:i + batch]]
                err = ""
                try:
                    if self.permanent:
                        for p in chunk:
                            try:
                                os.remove(_long(p))
                            except PermissionError:
                                os.chmod(_long(p), stat.S_IWRITE | stat.S_IREAD)
                                os.remove(_long(p))
                            except OSError as e:
                                self.results[p] = friendly_error(e)
                    elif IS_WIN:
                        _win_recycle(chunk)
                    elif IS_MAC:
                        _mac_trash(chunk)
                    else:
                        _linux_trash_one(chunk[0])
                except Exception as e:  # noqa: BLE001
                    err = friendly_error(e)
                for p in chunk:
                    if p in self.results:
                        continue
                    if os.path.lexists(_long(p)):
                        self.results[p] = err or "could not be deleted"
                    else:
                        self.results[p] = ""
                self.done_count += len(chunk)
        finally:
            self.done = True

    @property
    def deleted(self) -> list[str]:
        return [p for p, e in self.results.items() if not e]

    @property
    def failed(self) -> dict[str, str]:
        return {p: e for p, e in self.results.items() if e}


# ----------------------------------------------------------------------------- export
def export_csv(items: list[FileItem], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Name", "Type", "Size (bytes)", "Size", "Modified", "Folder", "Full path",
                    "Duplicate group"])
        for it in items:
            w.writerow([it.name, it.ext.upper(), it.size, human_size(it.size), human_time(it.mtime),
                        it.folder, it.path, it.dup or ""])
