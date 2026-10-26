"""FileHunter faces: find faces in photos, group them into people, and decide each photo's folder.

Everything runs on this computer. It uses OpenCV's free face models:
  YuNet  - finds faces in a photo           (~0.2 MB)
  SFace  - turns a face into a "fingerprint" (~37 MB) so the same person can be matched
The models are downloaded once into FileHunter's settings folder.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field

import filehunter_core as core

MODEL_BASE = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
MODELS = {
    "detector": ("face_detection_yunet_2023mar.onnx",
                 MODEL_BASE + "face_detection_yunet/face_detection_yunet_2023mar.onnx", 200_000),
    "recognizer": ("face_recognition_sface_2021dec.onnx",
                   MODEL_BASE + "face_recognition_sface/face_recognition_sface_2021dec.onnx", 30_000_000),
}
MODELS_MB = 39

PHOTO_EXTS = {"jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "jfif"}

# Similarity (cosine) above which two faces count as the same person. SFace's published
# threshold is 0.363 for two single faces; matching against a person's average is a bit stricter.
SAME_PERSON = 0.40
MERGE_PEOPLE = 0.42
KNOWN_PERSON = 0.45
MIN_FACE_PX = 24        # ignore faces smaller than this (tiny background faces)
MIN_SCORE = 0.85        # detector confidence
MAX_SIDE = 1280         # photos are shrunk to this before searching for faces
MAX_FACES_PER_PHOTO = 24  # in a crowd, only the biggest faces are recognised
THUMB = 64              # face thumbnail size in pixels
CACHE_VERSION = 2       # bump when face finding changes, so old cached results are redone


# ----------------------------------------------------------------------------- availability
def opencv_status() -> tuple[bool, str]:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"OpenCV is not installed ({e.__class__.__name__})"
    import cv2
    if not (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")):
        return False, f"OpenCV {cv2.__version__} is too old (4.5.4 or newer is needed)"
    return True, f"OpenCV {cv2.__version__}"


def install_opencv(log=print) -> bool:
    """pip-install the OpenCV add-on into the Python running FileHunter."""
    import subprocess
    import sys
    if getattr(sys, "frozen", False):
        return False
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
           "opencv-python-headless>=4.8", "numpy"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:  # noqa: BLE001
        log(f"Install failed: {e}")
        return False
    log(p.stdout[-2000:] + p.stderr[-2000:])
    if p.returncode != 0:
        return False
    import importlib
    importlib.invalidate_caches()
    return opencv_status()[0]


def models_dir() -> str:
    d = os.path.join(str(core.config_dir()), "models")
    os.makedirs(d, exist_ok=True)
    return d


def model_path(kind: str) -> str:
    return os.path.join(models_dir(), MODELS[kind][0])


def missing_models() -> list[str]:
    out = []
    for kind, (_name, _url, min_size) in MODELS.items():
        p = model_path(kind)
        if not os.path.isfile(p) or os.path.getsize(p) < min_size:
            out.append(kind)
    return out


def download_models(progress=None, stop: threading.Event | None = None) -> None:
    """Download any missing model. progress(done_bytes, total_bytes). Raises OSError on failure."""
    todo = missing_models()
    sizes, done = {}, 0
    for kind in todo:
        name, url, min_size = MODELS[kind]
        dst = model_path(kind)
        part = dst + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": f"{core.APP_NAME}/{core.VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as fh:
            sizes[kind] = int(r.headers.get("Content-Length") or 0)
            total = sum(sizes.values()) + sum(MODELS[k][2] for k in todo if k not in sizes)
            while True:
                if stop is not None and stop.is_set():
                    break
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, max(total, done))
        if stop is not None and stop.is_set():
            os.remove(part)
            raise OSError("download stopped")
        if os.path.getsize(part) < min_size:
            os.remove(part)
            raise OSError(f"{name} did not download completely; check the internet connection")
        os.replace(part, dst)


# ----------------------------------------------------------------------------- data
@dataclass
class Face:
    photo: str                 # path of the photo it was found in
    box: tuple                 # x, y, w, h in the photo (after shrinking)
    score: float
    emb: object                # numpy vector, length 1
    thumb: bytes               # small PNG of the face, for showing to the user
    person: int = -1           # index into the people list
    excluded: bool = False     # user said "this isn't them"

    @property
    def quality(self) -> float:
        return self.score * min(self.box[2], self.box[3])


@dataclass
class Person:
    pid: int
    faces: list = field(default_factory=list)
    name: str = ""
    include: bool = True
    centroid: object = None
    known: bool = False        # matched someone remembered from earlier

    @property
    def photos(self) -> set:
        return {f.photo for f in self.faces if not f.excluded}

    def update_centroid(self):
        import numpy as np
        embs = [f.emb for f in self.faces if not f.excluded]
        if embs:
            c = np.mean(embs, axis=0)
            self.centroid = c / (np.linalg.norm(c) + 1e-9)


# ----------------------------------------------------------------------------- analysis
def load_image(path: str):
    """BGR numpy image, upright (EXIF), shrunk to MAX_SIDE. Works with any characters in the path."""
    import numpy as np
    import cv2
    img = None
    try:
        from PIL import Image, ImageOps
        with Image.open(core._long(path)) as im:
            try:
                im.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG: decode at 1/2, 1/4 … size (much faster)
            except Exception:  # noqa: BLE001
                pass
            im = ImageOps.exif_transpose(im)
            if max(im.size) > MAX_SIDE:
                im.thumbnail((MAX_SIDE, MAX_SIDE), Image.BILINEAR)
            im = im.convert("RGB")
            img = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        img = None
    if img is None:
        data = np.fromfile(core._long(path), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_REDUCED_COLOR_2 if data.size > 3_000_000 else cv2.IMREAD_COLOR)
    if img is None:
        raise OSError("this image could not be read")
    h, w = img.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    return img


def thumb_png(data: bytes) -> bytes:
    """Face thumbnails are kept as small JPEGs; Tk needs PNG to show them."""
    if not data or data[:4] == b"\x89PNG":
        return data
    import numpy as np
    import cv2
    im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    ok, png = cv2.imencode(".png", im)
    return png.tobytes() if ok else b""


class FaceEngine:
    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(model_path("detector"), "", (320, 320), MIN_SCORE, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(model_path("recognizer"), "")

    def faces_in(self, path: str) -> list[Face]:
        import numpy as np
        cv2 = self.cv2
        img = load_image(path)
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        _, found = self.det.detect(img)
        out = []
        if found is None:
            return out
        rows = [r for r in found if float(r[-1]) >= MIN_SCORE and min(r[2], r[3]) >= MIN_FACE_PX]
        rows.sort(key=lambda r: -float(min(r[2], r[3])))
        for row in rows[:MAX_FACES_PER_PHOTO]:   # in a crowd, only the biggest faces matter
            x, y, fw, fh = (int(v) for v in row[:4])
            aligned = self.rec.alignCrop(img, row)
            emb = self.rec.feature(aligned).flatten().astype("float32")
            emb /= (np.linalg.norm(emb) + 1e-9)
            m = int(max(fw, fh) * 0.25)
            x0, y0 = max(0, x - m), max(0, y - m)
            x1, y1 = min(w, x + fw + m), min(h, y + fh + m)
            crop = img[y0:y1, x0:x1]
            if crop.size == 0:
                crop = aligned
            side = THUMB
            ch, cw = crop.shape[:2]
            s = side / max(ch, cw)
            crop = cv2.resize(crop, (max(1, int(cw * s)), max(1, int(ch * s))), interpolation=cv2.INTER_AREA)
            canvas = np.full((side, side, 3), 255, dtype=np.uint8)
            oy, ox = (side - crop.shape[0]) // 2, (side - crop.shape[1]) // 2
            canvas[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
            ok, jpg = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
            out.append(Face(photo=path, box=(x, y, fw, fh), score=float(row[-1]), emb=emb,
                            thumb=jpg.tobytes() if ok else b""))
        return out


# ----------------------------------------------------------------------------- results cache
class FaceCache:
    """Remembers the faces found in each photo (keyed by path, size and date), so scanning the
    same photos again is instant and a stopped scan carries on where it left off."""

    def __init__(self, path: str | None = None):
        import sqlite3
        self.path = path or os.path.join(str(core.config_dir()), "faces_cache.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"
                "CREATE TABLE IF NOT EXISTS photos (path TEXT PRIMARY KEY, size INT, mtime REAL,"
                " nfaces INT, err TEXT, ver INT);"
                "CREATE TABLE IF NOT EXISTS faces (path TEXT, x INT, y INT, w INT, h INT, score REAL,"
                " emb BLOB, thumb BLOB);"
                "CREATE INDEX IF NOT EXISTS faces_path ON faces(path);")

    @staticmethod
    def _sig(path: str):
        st = os.stat(core._long(path))
        return st.st_size, round(st.st_mtime, 2)

    def lookup(self, paths: list[str]):
        """{path: (faces, err)} for photos already analysed and unchanged since."""
        import numpy as np
        known = {}
        with self.lock:
            rows = self.db.execute("SELECT path, size, mtime, nfaces, err FROM photos WHERE ver=?",
                                   (CACHE_VERSION,)).fetchall()
        meta = {r[0]: r[1:] for r in rows}
        hits = []
        for p in paths:
            m = meta.get(p)
            if not m:
                continue
            try:
                if self._sig(p) != (m[0], m[1]):
                    continue
            except OSError:
                continue
            hits.append(p)
        out = {p: ([], meta[p][3]) for p in hits}
        for i in range(0, len(hits), 500):
            chunk = hits[i:i + 500]
            with self.lock:
                fr = self.db.execute(
                    f"SELECT path, x, y, w, h, score, emb, thumb FROM faces WHERE path IN "
                    f"({','.join('?' * len(chunk))})", chunk).fetchall()
            for path, x, y, w, h, score, emb, thumb in fr:
                out[path][0].append(Face(photo=path, box=(x, y, w, h), score=score,
                                         emb=np.frombuffer(emb, dtype="float32").copy(), thumb=thumb))
        return out

    def store(self, results: list[tuple[str, list, str]]):
        rows, frows, paths = [], [], []
        for path, fs, err in results:
            try:
                size, mtime = self._sig(path)
            except OSError:
                continue
            paths.append(path)
            rows.append((path, size, mtime, len(fs), err or None, CACHE_VERSION))
            for f in fs:
                frows.append((path, *f.box, f.score, f.emb.astype("float32").tobytes(), f.thumb))
        if not rows:
            return
        with self.lock:
            self.db.executemany("DELETE FROM faces WHERE path=?", [(p,) for p in paths])
            self.db.executemany("INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?)", rows)
            self.db.executemany("INSERT INTO faces VALUES (?,?,?,?,?,?,?,?)", frows)
            self.db.commit()

    def close(self):
        try:
            with self.lock:
                self.db.close()
        except Exception:  # noqa: BLE001
            pass


def clear_cache():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(os.path.join(str(core.config_dir()), "faces_cache.db" + suffix))
        except OSError:
            pass


def default_workers() -> int:
    n = os.cpu_count() or 2
    return max(2, min(6, n // 2)) if n >= 2 else 1


class FaceScan:
    """Looks for faces in many photos, on several processor cores, in the background."""

    def __init__(self, paths: list[str], workers: int | None = None, use_cache: bool = True):
        self.paths = list(dict.fromkeys(paths))
        self.workers = workers or default_workers()
        self.use_cache = use_cache
        self.faces: list[Face] = []
        self.no_face: list[str] = []
        self.errors: dict[str, str] = {}
        self.done_paths: list[str] = []
        self.from_cache = 0
        self.done_count = 0
        self.current = ""
        self.stage = "Starting…"
        self.stop_ev = threading.Event()
        self.run_ev = threading.Event()
        self.run_ev.set()
        self._lock = threading.Lock()
        self.done = False
        self.started = self.finished = 0.0
        self.fatal = ""

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.stop_ev.set()
        self.run_ev.set()

    def pause(self):
        self.run_ev.clear()

    def resume(self):
        self.run_ev.set()

    @property
    def paused(self):
        return not self.run_ev.is_set()

    def _record(self, path, fs, err):
        with self._lock:
            if err:
                self.errors[path] = err
            elif fs:
                self.faces.extend(fs)
            else:
                self.no_face.append(path)
            self.done_paths.append(path)
            self.done_count += 1

    def run(self):
        self.started = time.time()
        cache = None
        try:
            todo = self.paths
            if self.use_cache:
                self.stage = "Checking photos already analysed…"
                try:
                    cache = FaceCache()
                    hits = cache.lookup(self.paths)
                except Exception:  # noqa: BLE001 - a broken cache must never stop the scan
                    cache, hits = None, {}
                for p, (fs, err) in hits.items():
                    self._record(p, fs, err)
                self.from_cache = len(hits)
                todo = [p for p in self.paths if p not in hits]
            self.stage = "Looking for faces"
            if todo and not self.stop_ev.is_set():
                self._analyse(todo, cache)
        except Exception as e:  # noqa: BLE001
            self.fatal = f"Face search stopped: {e}"
        finally:
            if cache is not None:
                cache.close()
            self.finished = time.time()
            self.done = True

    def _analyse(self, todo, cache):
        import cv2
        n = max(1, min(self.workers, len(todo)))
        try:  # share the processor cores between the workers instead of each grabbing all of them
            cv2.setNumThreads(max(1, (os.cpu_count() or 2) // n))
        except Exception:  # noqa: BLE001
            pass
        engines = []
        try:
            engines = [FaceEngine() for _ in range(n)]
        except Exception as e:  # noqa: BLE001
            if not engines:
                self.fatal = f"The face models could not be loaded: {e}"
                return
        queue_ = list(reversed(todo))
        qlock = threading.Lock()
        pending: list = []
        plock = threading.Lock()

        def flush(force=False):
            if cache is None:
                return
            with plock:
                if not pending or (not force and len(pending) < 40):
                    return
                batch = pending[:]
                pending.clear()
            try:
                cache.store(batch)
            except Exception:  # noqa: BLE001
                pass

        def worker(eng):
            while not self.stop_ev.is_set():
                self.run_ev.wait()
                with qlock:
                    if not queue_:
                        return
                    p = queue_.pop()
                self.current = p
                try:
                    fs, err = eng.faces_in(p), ""
                except Exception as e:  # noqa: BLE001
                    fs, err = [], (core.friendly_error(e) if isinstance(e, OSError) else str(e)) or "unreadable"
                self._record(p, fs, err)
                with plock:
                    pending.append((p, fs, err))
                flush()

        threads = [threading.Thread(target=worker, args=(e,), daemon=True) for e in engines]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        flush(force=True)


# ----------------------------------------------------------------------------- grouping
def _thresholds(sizes):
    """Small faces carry less detail, so they need a closer match to count as the same person."""
    import numpy as np
    sizes = np.asarray(sizes)
    return np.where(sizes >= 60, SAME_PERSON, np.where(sizes >= 36, SAME_PERSON + 0.06, SAME_PERSON + 0.12))


def group_faces(faces: list[Face], known: list[dict] | None = None, progress=None,
                stop: threading.Event | None = None) -> list[Person]:
    """Put faces of the same person together. Biggest groups first.

    Fast enough for 100,000+ faces: every face is compared with each group's average face (one
    matrix product), then similar groups are joined in blocks. progress(done, total, text).
    known: people remembered from earlier runs [{"name", "centroid"}]; matching groups get their
    names and keep the remembered order (priority).
    """
    import numpy as np
    n = len(faces)
    if not n:
        return []
    E = np.stack([f.emb for f in faces]).astype("float32")
    E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
    sizes = [min(f.box[2], f.box[3]) for f in faces]
    thr = _thresholds(sizes)
    order = sorted(range(n), key=lambda i: -faces[i].quality)

    cap = 1024
    C = np.zeros((cap, E.shape[1]), dtype="float32")     # average face of each group (unit length)
    SUM = np.zeros((cap, E.shape[1]), dtype="float32")   # sum of the faces in each group
    members: list[list[int]] = []
    photos: list[set] = []
    k = 0
    for step, i in enumerate(order):
        if stop is not None and stop.is_set():
            break
        if progress and step % 2000 == 0:
            progress(step, n, "Grouping faces into people")
        e, photo = E[i], faces[i].photo
        g = -1
        if k:
            sims = C[:k] @ e
            cand = np.flatnonzero(sims >= thr[i])
            if cand.size:
                for c in cand[np.argsort(-sims[cand])]:
                    if photo not in photos[c]:   # one person can't be in the same photo twice
                        g = int(c)
                        break
        if g < 0:
            if k == cap:
                cap *= 2
                C = np.vstack([C, np.zeros_like(C)])
                SUM = np.vstack([SUM, np.zeros_like(SUM)])
            g = k
            k += 1
            members.append([])
            photos.append(set())
        members[g].append(i)
        photos[g].add(photo)
        SUM[g] += e
        C[g] = SUM[g] / (np.linalg.norm(SUM[g]) + 1e-9)

    # join groups that turned out to be the same person (compared block by block: low memory)
    if progress:
        progress(n, n, "Joining groups of the same person")
    G = C[:k]
    pairs = []
    B = 2048
    for a in range(0, k, B):
        if stop is not None and stop.is_set():
            break
        S = G[a:a + B] @ G[a:].T
        ii, jj = np.nonzero(S >= MERGE_PEOPLE)
        for x, y in zip(ii.tolist(), jj.tolist()):
            gi, gj = a + x, a + y
            if gj > gi:
                pairs.append((float(S[x, y]), gi, gj))
    pairs.sort(reverse=True)
    parent = list(range(k))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for _s, a_, b_ in pairs:
        ra, rb = find(a_), find(b_)
        if ra == rb or photos[ra] & photos[rb]:   # never join two people seen in the same photo
            continue
        ca = SUM[ra] / (np.linalg.norm(SUM[ra]) + 1e-9)
        cb = SUM[rb] / (np.linalg.norm(SUM[rb]) + 1e-9)
        if float(ca @ cb) < MERGE_PEOPLE:          # the groups drifted apart while joining others
            continue
        if len(members[rb]) > len(members[ra]):
            ra, rb = rb, ra
        parent[rb] = ra
        members[ra] += members[rb]
        photos[ra] |= photos[rb]
        SUM[ra] += SUM[rb]
        members[rb] = []

    people: list[Person] = []
    for g in range(k):
        if find(g) != g or not members[g]:
            continue
        p = Person(pid=0, faces=[faces[i] for i in members[g]])
        p.centroid = SUM[g] / (np.linalg.norm(SUM[g]) + 1e-9)
        p._nphotos = len(photos[g])
        people.append(p)
    people.sort(key=lambda p: (-p._nphotos, -max(f.quality for f in p.faces)))
    # names and priority from earlier runs
    order_known = {}
    if known and people:
        P = np.stack([p.centroid for p in people])
        for kn_i, kn in enumerate(known):
            kc = np.asarray(kn["centroid"], dtype="float32")
            sims = P @ kc
            for idx in np.argsort(-sims):
                if sims[idx] < KNOWN_PERSON:
                    break
                p = people[int(idx)]
                if not p.known:
                    p.name, p.known = kn["name"], True
                    p.include = kn.get("include", True)
                    order_known[id(p)] = kn_i
                    break
    people.sort(key=lambda p: (0, order_known[id(p)]) if id(p) in order_known else (1, 0))
    for num, p in enumerate(people):
        p.pid = num
        if not p.name:
            p.name = f"Person {num + 1}"
        for f in p.faces:
            f.person = num
    if progress:
        progress(n, n, "Done")
    return people


def split_face(people: list[Person], face: Face) -> None:
    """User says this face isn't that person: it is left out of the person's photos."""
    face.excluded = True
    if 0 <= face.person < len(people):
        people[face.person].update_centroid()


def merge_people(people: list[Person], keep: Person, others: list[Person]) -> None:
    for o in others:
        if o is keep:
            continue
        keep.faces += o.faces
        people.remove(o)
    keep.update_centroid()
    for n, p in enumerate(people):
        p.pid = n
        for f in p.faces:
            f.person = n


def plan_folders(people: list[Person], photos: list[str], no_face: list[str], *,
                 group_rule: str = "priority", others: str | None = "Others",
                 min_photos: int = 1) -> dict[str, list[str]]:
    """Decide the folder(s) for every photo.

    people is in priority order (first = highest). A photo with several chosen people goes to
    the highest one (group_rule="priority") or to all of them (group_rule="all").
    Photos with nobody chosen go to `others` (a folder name) or nowhere (None).
    Returns {photo: [folder name, ...]}; photos left out are not in the result.
    """
    rank = {}
    for n, p in enumerate(people):
        if p.include and len(p.photos) >= min_photos:
            rank[n] = n
    in_photo: dict[str, set[int]] = {}
    for n, p in enumerate(people):
        for f in p.faces:
            if not f.excluded:
                in_photo.setdefault(f.photo, set()).add(n)
    names = folder_names(people)
    out: dict[str, list[str]] = {}
    for ph in photos:
        chosen = sorted(i for i in in_photo.get(ph, ()) if i in rank)
        if chosen:
            out[ph] = [names[chosen[0]]] if group_rule == "priority" else [names[i] for i in chosen]
        elif others:
            out[ph] = [others]
    return out


def folder_names(people: list[Person]) -> list[str]:
    """Safe, unique folder names (two people both called 'Ravi' become 'Ravi' and 'Ravi (2)')."""
    seen: dict[str, int] = {}
    out = []
    for p in people:
        base = core._safe_part(p.name.strip() or f"Person {p.pid + 1}")
        k = base.lower()
        seen[k] = seen.get(k, 0) + 1
        out.append(base if seen[k] == 1 else f"{base} ({seen[k]})")
    return out


# ----------------------------------------------------------------------------- remembering people
def _known_file() -> str:
    return os.path.join(str(core.config_dir()), "people.json")


def load_known() -> list[dict]:
    try:
        with open(_known_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return [d for d in data if isinstance(d, dict) and "name" in d and "centroid" in d]
    except (OSError, ValueError):
        return []


def save_known(people: list[Person]) -> None:
    """Remember people the user named (in priority order). Unnamed 'Person N' groups are not kept."""
    keep = [p for p in people if p.centroid is not None and not p.name.startswith("Person ")]
    data = [{"name": p.name, "include": p.include,
             "centroid": [round(float(v), 5) for v in p.centroid]} for p in keep]
    old = {d["name"]: d for d in load_known()}
    for d in data:
        old.pop(d["name"], None)
    data += list(old.values())  # people not seen this time stay remembered, after the others
    tmp = _known_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _known_file())


def forget_known() -> None:
    try:
        os.remove(_known_file())
    except OSError:
        pass
