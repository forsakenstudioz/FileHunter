import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import filehunter_core as core  # noqa: E402


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setattr(core, "config_dir", lambda: d)


def mk(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


@pytest.fixture
def tree(tmp_path):
    r = tmp_path / "drive"
    deep = r.joinpath(*[f"l{i}" for i in range(20)])
    mk(str(deep / "buried.img"), b"A" * 200_000)
    mk(str(r / "a" / "copy.IMG"), b"A" * 200_000)
    mk(str(r / "b" / "copy.img"), b"B" * 1000)
    mk(str(r / "song.mp3"), b"m" * 10)
    mk(str(r / ".hidden" / "h.img"), b"h")
    mk(str(r / "node_modules" / "n.img"), b"n")
    return str(r)


def test_parse_exts():
    assert core.parse_exts("img, .ISO  *.mp3;tar.gz img") == ["img", "iso", "mp3", "tar.gz"]


def test_parse_size():
    assert core.parse_size("") is None
    assert core.parse_size("1.5 MB") == int(1.5 * 1024 ** 2)
    assert core.parse_size("2gb") == 2 * 1024 ** 3
    with pytest.raises(ValueError):
        core.parse_size("lots")


def test_scan_finds_buried_and_skips_noise(tree):
    found = core.Scanner([tree], core.ScanFilters(exts=["img"])).run_blocking()
    names = sorted(i.name for i in found)
    assert names == ["buried.img", "copy.IMG", "copy.img"]


def test_scan_hidden_and_system_options(tree):
    f = core.ScanFilters(exts=["img"], skip_system=False, include_hidden=True)
    assert len(core.Scanner([tree], f).run_blocking()) == 5


def test_filters(tree):
    f = core.ScanFilters(exts=["img", "mp3"], min_size=core.parse_size("100 KB"))
    assert {i.name for i in core.Scanner([tree], f).run_blocking()} == {"buried.img", "copy.IMG"}
    f = core.ScanFilters(exts=[], name="buri*")
    assert [i.name for i in core.Scanner([tree], f).run_blocking()] == ["buried.img"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin on Windows")
def test_symlink_loop_does_not_hang(tree):
    os.symlink(tree, os.path.join(tree, "loop"))
    t = time.time()
    core.Scanner([tree], core.ScanFilters(exts=["img"])).run_blocking()
    assert time.time() - t < 10


def test_duplicates(tree):
    items = core.Scanner([tree], core.ScanFilters(exts=["img"])).run_blocking()
    groups = core.find_duplicates(items)
    assert len(groups) == 1
    assert {i.name for i in groups[0]} == {"buried.img", "copy.IMG"}


def test_copy_rename_and_identical_skip(tree, tmp_path):
    items = core.Scanner([tree], core.ScanFilters(exts=["img"])).run_blocking()
    dest = str(tmp_path / "out")
    t = core.Transfer(items, dest, mode="copy", verify=True)
    t.run()
    assert t.counts["ok"] == 2 and t.counts["identical"] == 1  # buried.img == copy.IMG
    assert len(os.listdir(dest)) == 2
    t = core.Transfer(items, str(tmp_path / "out2"), mode="copy", skip_identical=False)
    t.run()
    assert t.counts["ok"] == 3  # name clash -> "copy (2).img" on case-insensitive systems
    t = core.Transfer(items, dest, mode="copy")
    t.run()
    assert t.counts["identical"] == 3
    assert all(os.path.exists(i.path) for i in items)


def test_move_and_undo(tree, tmp_path):
    items = core.Scanner([tree], core.ScanFilters(exts=["img"])).run_blocking()
    before = {i.path: open(i.path, "rb").read() for i in items}
    dest = str(tmp_path / "moved")
    t = core.Transfer(items, dest, mode="move", layout="keep", skip_identical=False)
    t.run()
    assert t.counts["ok"] == 3
    assert not any(os.path.exists(p) for p in before)
    res = core.undo_run(core.last_undoable())
    assert res["restored"] == 3 and res["failed"] == 0
    for p, data in before.items():
        assert open(p, "rb").read() == data


def test_layouts(tree):
    it = core.FileItem(os.path.join(tree, "a", "x.mp3"), "x.mp3", "mp3", 1, time.time(), tree)
    assert core.target_path(it, "D", "by_type").endswith(os.path.join("MP3", "x.mp3"))
    assert core.target_path(it, "D", "flat") == os.path.join("D", "x.mp3")
    keep = core.target_path(it, "D", "keep", {os.path.normcase(tree): "Data"})
    assert keep == os.path.join("D", "Data", "a", "x.mp3")


def test_stop_leaves_no_partial(tmp_path):
    big = mk(str(tmp_path / "src" / "big.iso"), os.urandom(5_000_000))
    it = core.FileItem(big, "big.iso", "iso", os.path.getsize(big), 0, str(tmp_path / "src"))
    t = core.Transfer([it], str(tmp_path / "dst"))
    t.stop()
    t.run()
    assert os.listdir(tmp_path / "dst") == []


def test_list_drives_returns_something():
    ds = core.list_drives()
    assert ds and all(d.path for d in ds)


def test_permanent_delete(tmp_path):
    a = mk(str(tmp_path / "x" / "a.img"))
    ro = mk(str(tmp_path / "x" / "readonly.img"))
    os.chmod(ro, 0o444)
    d = core.Deleter([a, ro, str(tmp_path / "missing.img")], permanent=True)
    d.run()
    assert not os.path.exists(a) and not os.path.exists(ro)
    assert set(d.deleted) == {a, ro} and list(d.failed) == [str(tmp_path / "missing.img")]


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="freedesktop Trash is Linux only")
def test_linux_trash(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ, XDG_DATA_HOME=str(tmp_path / "share")))
    f = mk(str(tmp_path / "x" / "song one.mp3"), b"abc")
    f2 = mk(str(tmp_path / "y" / "song one.mp3"), b"def")
    d = core.Deleter([f, f2])
    d.run()
    assert d.failed == {} and not os.path.exists(f) and not os.path.exists(f2)
    files = sorted(os.listdir(tmp_path / "share" / "Trash" / "files"))
    assert files == ["song one.2.mp3", "song one.mp3"]
    info = (tmp_path / "share" / "Trash" / "info" / "song one.mp3.trashinfo").read_text()
    assert "Path=" in info and "song%20one.mp3" in info and "DeletionDate=" in info


def test_skip_identical_by_contents_across_runs(tmp_path):
    src = tmp_path / "src"
    a = mk(str(src / "a" / "photo.jpg"), b"A" * 4000)
    mk(str(src / "b" / "photo.jpg"), b"B" * 4000)       # same name, different contents
    mk(str(src / "c" / "holiday.jpg"), b"A" * 4000)     # same contents as a, other name
    dest = tmp_path / "dest"
    mk(str(dest / "old" / "renamed.jpg"), b"B" * 4000)  # already there under another name
    items = core.Scanner([str(src)], core.ScanFilters(exts=["jpg"])).run_blocking()
    for _ in range(3):
        t = core.Transfer(items, str(dest))
        t.run()
    files = [f for _, _, fs in os.walk(dest) for f in fs]
    assert len(files) == 2  # renamed.jpg + one copy of the "A" photo, nothing more
    assert os.path.exists(a)


def test_report_lists_failures_and_not_reached(tmp_path):
    a = mk(str(tmp_path / "s" / "a.mp3"), b"a" * 10)
    b = mk(str(tmp_path / "s" / "b.mp3"), b"b" * 10)
    items = [core.FileItem(p, os.path.basename(p), "mp3", 10, 0, str(tmp_path / "s")) for p in (a, b)]
    os.remove(a)
    t = core.Transfer(items, str(tmp_path / "d"))
    t.run()
    assert [(r["status"], r["msg"]) for r in t.report][0] == ("failed", "file no longer exists")
    assert t.report[1]["status"] == "ok"
    t = core.Transfer(items, str(tmp_path / "d2"))
    t.stop()
    t.run()
    assert [r["status"] for r in t.report] == ["notreached", "notreached"]


def test_friendly_errors():
    import errno
    assert "full" in core.friendly_error(OSError(errno.ENOSPC, "x"))
    assert "permission" in core.friendly_error(PermissionError(errno.EACCES, "x"))
