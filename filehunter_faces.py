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
MIN_FACE_PX = 20        # ignore faces smaller than this (tiny background faces)
MIN_SCORE = 0.85        # detector confidence
MAX_SIDE = 1600         # photos are shrunk to this before searching for faces


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
                im.draft("RGB", (MAX_SIDE, MAX_SIDE))
            except Exception:  # noqa: BLE001
                pass
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            img = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        img = None
    if img is None:
        data = np.fromfile(core._long(path), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise OSError("this image could not be read")
    h, w = img.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    return img


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
        for row in found:
            x, y, fw, fh = (int(v) for v in row[:4])
            score = float(row[-1])
            if score < MIN_SCORE or min(fw, fh) < MIN_FACE_PX:
                continue
            aligned = self.rec.alignCrop(img, row)
            emb = self.rec.feature(aligned).flatten().astype("float32")
            emb /= (np.linalg.norm(emb) + 1e-9)
            m = int(max(fw, fh) * 0.25)
            x0, y0 = max(0, x - m), max(0, y - m)
            x1, y1 = min(w, x + fw + m), min(h, y + fh + m)
            crop = img[y0:y1, x0:x1]
            if crop.size == 0:
                crop = aligned
            side = 72
            ch, cw = crop.shape[:2]
            s = side / max(ch, cw)
            crop = cv2.resize(crop, (max(1, int(cw * s)), max(1, int(ch * s))), interpolation=cv2.INTER_AREA)
            canvas = np.full((side, side, 3), 255, dtype=np.uint8)
            oy, ox = (side - crop.shape[0]) // 2, (side - crop.shape[1]) // 2
            canvas[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
            ok, png = cv2.imencode(".png", canvas)
            out.append(Face(photo=path, box=(x, y, fw, fh), score=score, emb=emb,
                            thumb=png.tobytes() if ok else b""))
        return out


class FaceScan:
    """Looks for faces in many photos in a background thread."""

    def __init__(self, paths: list[str]):
        self.paths = list(paths)
        self.faces: list[Face] = []
        self.no_face: list[str] = []
        self.errors: dict[str, str] = {}
        self.done_count = 0
        self.current = ""
        self.stop_ev = threading.Event()
        self.run_ev = threading.Event()
        self.run_ev.set()
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

    def run(self):
        self.started = time.time()
        try:
            eng = FaceEngine()
        except Exception as e:  # noqa: BLE001
            self.fatal = f"The face models could not be loaded: {e}"
            self.done = True
            return
        for p in self.paths:
            if self.stop_ev.is_set():
                break
            self.run_ev.wait()
            self.current = p
            try:
                fs = eng.faces_in(p)
                if fs:
                    self.faces.extend(fs)
                else:
                    self.no_face.append(p)
            except Exception as e:  # noqa: BLE001
                self.errors[p] = core.friendly_error(e) if isinstance(e, OSError) else str(e)
            self.done_count += 1
        self.finished = time.time()
        self.done = True


# ----------------------------------------------------------------------------- grouping
def _threshold(f: Face) -> float:
    """Small faces carry less detail, so they need a closer match to count as the same person."""
    side = min(f.box[2], f.box[3])
    if side >= 60:
        return SAME_PERSON
    if side >= 36:
        return SAME_PERSON + 0.06
    return SAME_PERSON + 0.12

def group_faces(faces: list[Face], known: list[dict] | None = None) -> list[Person]:
    """Put faces of the same person together. Biggest groups first.

    known: people remembered from earlier runs [{"name", "centroid"}]; matching groups get their
    names and keep the remembered order (priority).
    """
    import numpy as np
    people: list[Person] = []
    cents: list = []
    in_photos: list[set] = []   # photos each group already has a face in
    for f in sorted(faces, key=lambda f: -f.quality):
        f.person = -1
        need = _threshold(f)
        if cents:
            sims = np.asarray(cents) @ f.emb
            for best in np.argsort(-sims):
                if sims[best] < need:
                    break
                if f.photo in in_photos[best]:
                    continue  # one person can't be in the same photo twice
                p = people[best]
                p.faces.append(f)
                in_photos[best].add(f.photo)
                c = np.mean([x.emb for x in p.faces], axis=0)
                cents[best] = c / (np.linalg.norm(c) + 1e-9)
                break
            else:
                best = None
            if best is not None and f in people[best].faces:
                continue
        people.append(Person(pid=len(people), faces=[f]))
        cents.append(f.emb.copy())
        in_photos.append({f.photo})
    for p, c in zip(people, cents):
        p.centroid = c
    # merge groups that turned out to be the same person
    merged = True
    while merged and len(people) > 1:
        merged = False
        C = np.asarray([p.centroid for p in people])
        S = C @ C.T
        np.fill_diagonal(S, -1)
        for a_i in range(len(people)):           # never merge two people seen in the same photo
            for b_i in range(a_i + 1, len(people)):
                if people[a_i].photos & people[b_i].photos:
                    S[a_i, b_i] = S[b_i, a_i] = -1
        i, j = np.unravel_index(int(np.argmax(S)), S.shape)
        if S[i, j] >= MERGE_PEOPLE:
            a, b = (i, j) if len(people[i].faces) >= len(people[j].faces) else (j, i)
            people[a].faces += people[b].faces
            people[a].update_centroid()
            del people[b]
            merged = True
    people.sort(key=lambda p: (-len(p.photos), -max(f.quality for f in p.faces)))
    # names and priority from earlier runs
    order = {}
    for kn_i, kn in enumerate(known or []):
        kc = np.asarray(kn["centroid"], dtype="float32")
        best, best_s = None, KNOWN_PERSON
        for p in people:
            if p.known:
                continue
            s = float(p.centroid @ kc)
            if s >= best_s:
                best, best_s = p, s
        if best is not None:
            best.name, best.known = kn["name"], True
            best.include = kn.get("include", True)
            order[id(best)] = kn_i
    people.sort(key=lambda p: (0, order[id(p)]) if id(p) in order else (1, 0))
    for n, p in enumerate(people):
        p.pid = n
        if not p.name:
            p.name = f"Person {n + 1}"
        for f in p.faces:
            f.person = n
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
