import os
import sys

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import filehunter_core as core  # noqa: E402
import filehunter_faces as faces  # noqa: E402


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setattr(core, "config_dir", lambda: d)


def person_vec(seed):
    v = np.random.default_rng(seed).normal(size=128).astype("float32")
    return v / np.linalg.norm(v)


def face(photo, base, noise_seed, size=120, noise=0.25):
    v = base + noise * np.random.default_rng(noise_seed).normal(size=128).astype("float32") / np.sqrt(128)
    v /= np.linalg.norm(v)
    return faces.Face(photo=photo, box=(0, 0, size, size), score=0.95, emb=v, thumb=b"")


def library():
    a, b, c = person_vec(1), person_vec(2), person_vec(3)
    fs = [face("a1.jpg", a, 10), face("a2.jpg", a, 11), face("a3.jpg", a, 12),
          face("b1.jpg", b, 20), face("b2.jpg", b, 21),
          face("c1.jpg", c, 30),
          face("ab.jpg", a, 13), face("ab.jpg", b, 22)]   # group photo with A and B
    return fs, ["a1.jpg", "a2.jpg", "a3.jpg", "b1.jpg", "b2.jpg", "c1.jpg", "ab.jpg", "none.jpg"]


def test_grouping_finds_each_person_once():
    fs, _ = library()
    people = faces.group_faces(fs)
    sets = sorted(sorted(p.photos) for p in people)
    assert sets == [["a1.jpg", "a2.jpg", "a3.jpg", "ab.jpg"], ["ab.jpg", "b1.jpg", "b2.jpg"], ["c1.jpg"]]


def test_never_two_faces_of_one_person_in_a_photo():
    a = person_vec(1)
    # two near-identical faces in one photo (twins / poster) must not become one person
    fs = [face("x.jpg", a, 1, noise=0.05), face("x.jpg", a, 2, noise=0.05)]
    people = faces.group_faces(fs)
    assert len(people) == 2


def test_priority_decides_group_photos():
    fs, photos = library()
    people = faces.group_faces(fs)
    A = next(p for p in people if "a1.jpg" in p.photos)
    B = next(p for p in people if "b1.jpg" in p.photos)
    rest = [p for p in people if p not in (A, B)]
    A.name, B.name = "Ravi", "Priya"
    plan = faces.plan_folders([A, B] + rest, photos, ["none.jpg"])
    assert plan["ab.jpg"] == ["Ravi"]
    plan = faces.plan_folders([B, A] + rest, photos, ["none.jpg"])
    assert plan["ab.jpg"] == ["Priya"]
    assert plan["a1.jpg"] == ["Ravi"] and plan["b1.jpg"] == ["Priya"]
    assert plan["none.jpg"] == ["Others"]
    plan = faces.plan_folders([A, B] + rest, photos, ["none.jpg"], group_rule="all", others=None)
    assert sorted(plan["ab.jpg"]) == ["Priya", "Ravi"] and "none.jpg" not in plan


def test_excluded_person_and_min_photos():
    fs, photos = library()
    people = faces.group_faces(fs)
    A = next(p for p in people if "a1.jpg" in p.photos)
    B = next(p for p in people if "b1.jpg" in p.photos)
    A.include = False
    plan = faces.plan_folders(people, photos, [], min_photos=2, others="Others")
    assert plan["ab.jpg"] == [faces.folder_names(people)[people.index(B)]]
    assert plan["c1.jpg"] == ["Others"]  # seen once: not given a folder of its own


def test_remembered_people_keep_name_and_order():
    fs, _ = library()
    people = faces.group_faces(fs)
    A = next(p for p in people if "a1.jpg" in p.photos)
    B = next(p for p in people if "b1.jpg" in p.photos)
    A.name, B.name = "Ravi", "Priya"
    faces.save_known([B, A])
    fs2, _ = library()
    again = faces.group_faces(fs2, faces.load_known())
    assert [p.name for p in again[:2]] == ["Priya", "Ravi"] and again[0].known


def test_folder_names_are_safe_and_unique():
    ps = [faces.Person(pid=i, name=n) for i, n in enumerate(["Ravi", "ravi", "A/B:C"])]
    assert faces.folder_names(ps) == ["Ravi", "ravi (2)", "A_B_C"]
