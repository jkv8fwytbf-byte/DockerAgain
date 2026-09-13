"""Handouts (/srv/shared) and submissions (~/submit) API."""
from __future__ import annotations

import io
import os
import stat
import zipfile

import pytest

from console import files as files_mod
from console.roster import Roster, Student

API = "/services/console/api"


# --- helpers ---------------------------------------------------------------------------


def save_roster(settings, *students):
    Roster(settings.roster_path).save([Student(username=u, password="pw-1", display_name=d) for u, d in students])


def multipart(parts, boundary="xxBOUNDARYxx"):
    """parts: (field, filename|None, bytes) in the order they should appear in the body."""
    body = b""
    for name, filename, content in parts:
        body += f"--{boundary}\r\n".encode()
        if filename is None:
            body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + content + b"\r\n"
        else:
            body += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def make_zip(entries, symlinks=()):
    """entries: {name: bytes}; symlinks: [(name, target)] stored with a symlink mode."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name, target in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


def upload(client, filename, content, **fields):
    data = {"path": "", "unzip": "0", "overwrite": "0", "keep_zip": "0"}
    data.update({k: str(v) for k, v in fields.items()})
    return client.post(f"{API}/handouts", data=data, files={"files": (filename, content, "application/octet-stream")})


def shared_names(settings):
    return sorted(os.listdir(settings.shared_dir))


def zip_names(response):
    assert response.headers["content-type"].startswith("application/zip")
    return sorted(zipfile.ZipFile(io.BytesIO(response.content)).namelist())


# --- page and permissions --------------------------------------------------------------------


def test_files_page_renders(as_teacher):
    r = as_teacher.get("/services/console/files")
    assert r.status_code == 200
    assert "Handouts" in r.text and "Submissions" in r.text and "files.js" in r.text
    assert "/lab/tree/shared" in r.text


def test_student_is_forbidden_everywhere(as_student):
    assert as_student.get(f"{API}/handouts").status_code == 403
    assert as_student.get(f"{API}/submissions").status_code == 403
    assert as_student.get(f"{API}/submissions/download-all").status_code == 403
    assert as_student.delete(f"{API}/handouts?path=x").status_code == 403
    r = upload(as_student, "a.txt", b"x")
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_anonymous_is_401(client):
    assert client.get(f"{API}/handouts").status_code == 401
    assert client.get(f"{API}/submissions/student01/download").status_code == 401


# --- handouts: listing ------------------------------------------------------------------------


def test_list_handouts_reports_entry_types(as_teacher, settings, tmp_path):
    shared = settings.shared_dir
    with open(os.path.join(shared, "notes.txt"), "w") as fh:
        fh.write("hello")
    os.mkdir(os.path.join(shared, "week1"))
    os.symlink("/etc/passwd", os.path.join(shared, "passwd-link"))
    os.mkfifo(os.path.join(shared, "pipe"))
    with open(os.path.join(shared, ".upload.part-0123456789ab"), "w") as fh:
        fh.write("in progress")
    os.mkdir(os.path.join(shared, ".ipynb_checkpoints"))
    with open(os.path.join(shared, ".DS_Store"), "w") as fh:
        fh.write("finder junk")
    r = as_teacher.get(f"{API}/handouts")
    assert r.status_code == 200
    data = r.json()
    assert data["path"] == "" and data["max_upload_bytes"] == settings.max_upload_bytes
    by_name = {e["name"]: e for e in data["entries"]}
    assert set(by_name) == {"notes.txt", "week1", "passwd-link", "pipe"}   # dot-entries (temp uploads, checkpoints) hidden
    assert by_name["notes.txt"]["type"] == "file" and by_name["notes.txt"]["size"] == 5
    assert by_name["week1"]["type"] == "dir"
    assert by_name["passwd-link"]["type"] == "symlink"
    assert by_name["pipe"]["type"] == "other"
    assert data["entries"][0]["name"] == "week1"           # folders first
    assert all("mtime" in e for e in data["entries"])
    r = as_teacher.get(f"{API}/handouts", params={"path": "week1"})
    assert r.status_code == 200 and r.json() == {"path": "week1", "entries": [], "max_upload_bytes": settings.max_upload_bytes}
    for root in ("/", ".", "./", " "):                       # spellings of the root
        assert as_teacher.get(f"{API}/handouts", params={"path": root}).json()["path"] == "", root


def test_list_handouts_rejects_bad_paths(as_teacher, settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), os.path.join(settings.shared_dir, "escape"))
    for bad in ("..", "../", "a/../../b", "a\x00b", "escape", "escape/x", "/etc", "/week1", "//x", "\\..\\x", "x/" + "a" * 1200):
        r = as_teacher.get(f"{API}/handouts", params={"path": bad})
        assert r.status_code == 400, bad
        assert r.json()["error"] == "bad_path", bad
    assert as_teacher.get(f"{API}/handouts", params={"path": "nope"}).status_code == 404
    with open(os.path.join(settings.shared_dir, "f.txt"), "w") as fh:
        fh.write("x")
    assert as_teacher.get(f"{API}/handouts", params={"path": "f.txt"}).status_code == 400
    # a path *through* a file (ENOTDIR) and an over-long component (ENAMETOOLONG) are 400s, never 500s
    os.mkdir(os.path.join(settings.shared_dir, "dir1"))
    for bad in ("f.txt/x", "f.txt/x/y", "é" * 300, "dir1/" + "a" * 256):
        r = as_teacher.get(f"{API}/handouts", params={"path": bad})
        assert r.status_code == 400 and r.json()["error"] == "bad_path", bad
        assert as_teacher.delete(f"{API}/handouts", params={"path": bad}).status_code == 400, bad
        assert as_teacher.get(f"{API}/handouts/download", params={"path": bad}).status_code in (400, 404), bad
        assert upload(as_teacher, "a.txt", b"x", path=bad).status_code == 400, bad
    # 300 bytes but 100 characters: ENAMETOOLONG on ext4 (the image), plain ENOENT on APFS (the Mac);
    # a missing middle component is ENOENT before the long name is even looked at. Never a 500.
    for iffy in ("dir1/" + "字" * 100, "dir1/missing/" + "a" * 256):
        assert as_teacher.get(f"{API}/handouts", params={"path": iffy}).status_code in (400, 404), iffy
        assert as_teacher.delete(f"{API}/handouts", params={"path": iffy}).status_code in (400, 404), iffy
        assert upload(as_teacher, "a.txt", b"x", path=iffy).status_code in (400, 404), iffy
    r = as_teacher.post(f"{API}/handouts/mkdir", json={"path": "f.txt/sub"})
    assert r.status_code in (400, 404) and r.json()["error"] in ("bad_path", "not_found")


def test_list_handouts_when_shared_dir_missing(as_teacher, settings):
    os.rmdir(settings.shared_dir)
    r = as_teacher.get(f"{API}/handouts")
    assert r.status_code == 200 and r.json()["entries"] == []


# --- handouts: upload ---------------------------------------------------------------------------


def test_upload_single_file(as_teacher, settings):
    r = upload(as_teacher, "lesson 1.ipynb", b'{"cells": []}')
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "", "saved": ["lesson 1.ipynb"], "extracted": 0, "skipped": [], "unpacked_into": []}
    path = os.path.join(settings.shared_dir, "lesson 1.ipynb")
    with open(path, "rb") as fh:
        assert fh.read() == b'{"cells": []}'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o664
    assert shared_names(settings) == ["lesson 1.ipynb"]      # no .part left behind


def test_upload_into_subfolder_and_listing_shows_it(as_teacher, settings):
    os.mkdir(os.path.join(settings.shared_dir, "week1"))
    r = upload(as_teacher, "data.csv", b"a,b\n1,2\n", path="week1")
    assert r.status_code == 200 and r.json()["saved"] == ["week1/data.csv"]
    r = as_teacher.get(f"{API}/handouts", params={"path": "week1"})
    assert [e["name"] for e in r.json()["entries"]] == ["data.csv"]


def test_upload_field_after_file_still_lands_in_folder(as_teacher, settings):
    os.mkdir(os.path.join(settings.shared_dir, "late"))
    body, ctype = multipart([("files", "x.txt", b"payload"), ("path", None, b"late"), ("overwrite", None, b"0")])
    r = as_teacher.post(f"{API}/handouts", content=body, headers={"Content-Type": ctype})
    assert r.status_code == 200 and r.json()["saved"] == ["late/x.txt"]
    assert os.listdir(os.path.join(settings.shared_dir, "late")) == ["x.txt"]
    assert shared_names(settings) == ["late"]


def test_upload_several_files(as_teacher, settings):
    r = as_teacher.post(f"{API}/handouts", data={"path": ""}, files=[
        ("files", ("a.txt", b"a", "text/plain")), ("files", ("b.txt", b"bb", "text/plain")),
    ])
    assert r.status_code == 200 and r.json()["saved"] == ["a.txt", "b.txt"]


def test_upload_existing_is_409_unless_overwrite(as_teacher, settings):
    assert upload(as_teacher, "same.txt", b"one").status_code == 200
    r = upload(as_teacher, "same.txt", b"two")
    assert r.status_code == 409
    assert r.json()["error"] == "exists" and r.json()["detail"]["name"] == "same.txt"
    with open(os.path.join(settings.shared_dir, "same.txt")) as fh:
        assert fh.read() == "one"
    r = upload(as_teacher, "same.txt", b"two", overwrite=1)
    assert r.status_code == 200
    with open(os.path.join(settings.shared_dir, "same.txt")) as fh:
        assert fh.read() == "two"
    assert shared_names(settings) == ["same.txt"]
    os.mkdir(os.path.join(settings.shared_dir, "folder"))
    r = upload(as_teacher, "folder", b"x", overwrite=1)
    assert r.status_code == 409 and r.json()["detail"]["kind"] == "dir"


def test_oversized_upload_is_413_before_anything_is_written(as_teacher, settings, app):
    app.state.settings.max_upload_bytes = 100
    r = upload(as_teacher, "big.bin", b"x" * 1000)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert shared_names(settings) == []


def test_chunked_upload_without_content_length_is_capped_while_streaming(as_teacher, settings, app):
    app.state.settings.max_upload_bytes = 100
    body, ctype = multipart([("path", None, b""), ("files", "big.bin", b"y" * 5000)])

    def chunks():
        for i in range(0, len(body), 700):
            yield body[i:i + 700]

    r = as_teacher.post(f"{API}/handouts", content=chunks(), headers={"Content-Type": ctype})
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert shared_names(settings) == []                       # the .part file was removed


def test_upload_filename_is_reduced_to_a_basename(as_teacher, settings, tmp_path):
    r = upload(as_teacher, "../../evil.txt", b"nope")
    assert r.status_code == 200 and r.json()["saved"] == ["evil.txt"]
    assert not (tmp_path / "evil.txt").exists()
    r = upload(as_teacher, "..\\..\\win.txt", b"nope")
    assert r.status_code == 200 and r.json()["saved"] == ["win.txt"]
    r = upload(as_teacher, ".hidden", b"nope")
    assert r.status_code == 200 and r.json()["saved"] == ["_hidden"]
    assert sorted(shared_names(settings)) == ["_hidden", "evil.txt", "win.txt"]


def test_upload_very_long_names_are_shortened_to_fit_the_filesystem(as_teacher, settings):
    long_cjk = "字" * 150 + ".ipynb"                       # 450 bytes: allowed by clean_filename, refused by ext4
    r = upload(as_teacher, long_cjk, b"{}")
    assert r.status_code == 200, r.text
    saved = r.json()["saved"][0]
    assert saved.endswith(".ipynb") and len(saved.encode("utf-8")) <= 255 and saved.startswith("字字字")
    assert os.path.isfile(os.path.join(settings.shared_dir, saved))
    assert files_mod._fit_name("a" * 300) == "a" * 255
    assert files_mod._fit_name("é" * 200 + ".tar.gz").endswith(".gz") and len(files_mod._fit_name("é" * 200 + ".tar.gz").encode()) <= 255
    assert files_mod._fit_name("short.txt") == "short.txt"
    r = as_teacher.post(f"{API}/handouts/mkdir", json={"path": "字" * 100})    # 300 bytes, 100 characters
    assert r.status_code == 400 and r.json()["error"] == "bad_name"


def test_upload_path_traversal_is_400(as_teacher, settings, tmp_path):
    for bad in ("..", "../", "../../", "x/../../y", "a\x00b", "/tmp", "/" + str(tmp_path)):
        r = upload(as_teacher, "a.txt", b"x", path=bad)
        assert r.status_code == 400 and r.json()["error"] == "bad_path", bad
    assert shared_names(settings) == []
    assert not (tmp_path / "a.txt").exists() and not os.path.exists("/tmp/a.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), os.path.join(settings.shared_dir, "escape"))
    r = upload(as_teacher, "a.txt", b"x", path="escape")
    assert r.status_code == 400
    assert os.listdir(outside) == []
    assert upload(as_teacher, "a.txt", b"x", path="missing").status_code == 404


def test_upload_needs_a_file(as_teacher):
    body, ctype = multipart([("path", None, b""), ("files", "", b"")])       # an empty <input type=file>
    r = as_teacher.post(f"{API}/handouts", content=body, headers={"Content-Type": ctype})
    assert r.status_code == 400 and r.json()["error"] == "no_files"
    r = as_teacher.post(f"{API}/handouts", data={"path": ""})                  # urlencoded, not multipart
    assert r.status_code == 400 and r.json()["error"] == "bad_upload"
    r = as_teacher.post(f"{API}/handouts", json={"path": ""})
    assert r.status_code == 400 and r.json()["error"] == "bad_upload"


def test_upload_zip_is_unpacked_in_place_when_it_has_one_top_folder(as_teacher, settings):
    data = make_zip({"week3/a.txt": b"A", "week3/sub/b.txt": b"B", "week3/empty/": b""})
    r = upload(as_teacher, "week3.zip", data, unzip=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["extracted"] == 2 and body["saved"] == [] and body["skipped"] == [] and body["unpacked_into"] == [""]
    shared = settings.shared_dir
    assert open(os.path.join(shared, "week3", "a.txt")).read() == "A"
    assert open(os.path.join(shared, "week3", "sub", "b.txt")).read() == "B"
    assert os.path.isdir(os.path.join(shared, "week3", "empty"))
    assert stat.S_IMODE(os.stat(os.path.join(shared, "week3", "sub")).st_mode) == 0o2775
    assert stat.S_IMODE(os.stat(os.path.join(shared, "week3", "a.txt")).st_mode) == 0o664
    assert shared_names(settings) == ["week3"]              # zip discarded, no temp files


def test_upload_zip_with_loose_files_goes_into_a_folder_named_after_it(as_teacher, settings):
    data = make_zip({"a.txt": b"A", "b/c.txt": b"C", "__MACOSX/._a.txt": b"junk", ".DS_Store": b"junk"})
    r = upload(as_teacher, "Bundle.zip", data, unzip=1, keep_zip=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["extracted"] == 2 and body["saved"] == ["Bundle.zip"] and body["unpacked_into"] == ["Bundle"]
    assert sorted(os.listdir(os.path.join(settings.shared_dir, "Bundle"))) == ["a.txt", "b"]
    assert shared_names(settings) == ["Bundle", "Bundle.zip"]


def test_upload_zip_with_a_single_file_unpacks_in_place(as_teacher, settings):
    r = upload(as_teacher, "lesson.zip", make_zip({"lesson.ipynb": b"{}"}), unzip=1)
    assert r.status_code == 200, r.text
    assert r.json()["extracted"] == 1 and r.json()["unpacked_into"] == [""]
    assert shared_names(settings) == ["lesson.ipynb"]


def test_zip_entry_under_an_existing_file_is_skipped_not_fatal(as_teacher, settings):
    os.mkdir(os.path.join(settings.shared_dir, "pack"))
    with open(os.path.join(settings.shared_dir, "pack", "notes"), "w") as fh:
        fh.write("a file called notes")
    data = make_zip({"pack/notes/inner.txt": b"x", "pack/notes/deeper/": b"", "pack/other.txt": b"y"})
    r = upload(as_teacher, "pack.zip", data, unzip=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["extracted"] == 1 and body["unpacked_into"] == [""]
    assert sorted(s["name"] for s in body["skipped"]) == ["pack/notes/deeper", "pack/notes/inner.txt"]
    assert {s["reason"] for s in body["skipped"]} == {"a file has that folder's name"}
    assert sorted(os.listdir(os.path.join(settings.shared_dir, "pack"))) == ["notes", "other.txt"]
    assert open(os.path.join(settings.shared_dir, "pack", "notes")).read() == "a file called notes"


def test_upload_zip_unpacked_into_subfolder_is_reported_relative(as_teacher, settings):
    os.mkdir(os.path.join(settings.shared_dir, "week4"))
    r = upload(as_teacher, "set.zip", make_zip({"one.txt": b"1", "two.txt": b"2"}), unzip=1, path="week4")
    assert r.status_code == 200 and r.json()["unpacked_into"] == ["week4/set"]
    assert os.path.isfile(os.path.join(settings.shared_dir, "week4", "set", "one.txt"))


def test_upload_zip_without_unzip_is_stored_as_a_file(as_teacher, settings):
    r = upload(as_teacher, "keep.zip", make_zip({"a.txt": b"A"}))
    assert r.status_code == 200 and r.json()["saved"] == ["keep.zip"]
    assert shared_names(settings) == ["keep.zip"]


def test_zip_slip_entries_are_skipped_and_nothing_escapes(as_teacher, settings, tmp_path):
    data = make_zip({
        "../../evil.txt": b"evil", "/abs.txt": b"abs", "C:\\drive.txt": b"drive",
        "ok/../../up.txt": b"up", "ok/fine.txt": b"fine", "ok/bad\x01name.txt": b"ctl",
    })
    r = upload(as_teacher, "slip.zip", data, unzip=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["extracted"] == 1
    reasons = {s["name"]: s["reason"] for s in body["skipped"]}
    assert set(reasons) == {"../../evil.txt", "/abs.txt", "C:/drive.txt", "ok/../../up.txt", "ok/bad\x01name.txt"}
    assert set(reasons.values()) == {"unsafe path"}
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "up.txt").exists()
    assert not os.path.exists("/abs.txt")
    # the only safe entry lives under one top folder, so the archive unpacks in place
    assert shared_names(settings) == ["ok"]
    assert os.listdir(os.path.join(settings.shared_dir, "ok")) == ["fine.txt"]


def test_zip_symlink_entries_are_skipped(as_teacher, settings):
    data = make_zip({"pack/readme.txt": b"hi"}, symlinks=[("pack/shadow", "/etc/shadow")])
    r = upload(as_teacher, "pack.zip", data, unzip=1)
    assert r.status_code == 200, r.text
    assert r.json()["extracted"] == 1
    assert r.json()["skipped"] == [{"name": "pack/shadow", "reason": "link"}]
    assert os.listdir(os.path.join(settings.shared_dir, "pack")) == ["readme.txt"]


def test_zip_entries_never_write_through_a_planted_link(as_teacher, settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.mkdir(os.path.join(settings.shared_dir, "pack"))
    os.symlink(str(outside), os.path.join(settings.shared_dir, "pack", "out"))
    r = upload(as_teacher, "pack.zip", make_zip({"pack/out/x.txt": b"x", "pack/ok.txt": b"ok"}), unzip=1)
    assert r.status_code == 200, r.text
    assert r.json()["extracted"] == 1
    assert [s["reason"] for s in r.json()["skipped"]] == ["unsafe path"]
    assert os.listdir(outside) == []


def test_zip_existing_files_are_skipped_unless_overwrite(as_teacher, settings):
    os.mkdir(os.path.join(settings.shared_dir, "w"))
    with open(os.path.join(settings.shared_dir, "w", "a.txt"), "w") as fh:
        fh.write("old")
    r = upload(as_teacher, "w.zip", make_zip({"w/a.txt": b"new", "w/b.txt": b"b"}), unzip=1)
    assert r.status_code == 200 and r.json()["extracted"] == 1
    assert r.json()["skipped"] == [{"name": "w/a.txt", "reason": "exists"}]
    assert open(os.path.join(settings.shared_dir, "w", "a.txt")).read() == "old"
    r = upload(as_teacher, "w.zip", make_zip({"w/a.txt": b"new"}), unzip=1, overwrite=1)
    assert r.status_code == 200 and r.json()["extracted"] == 1
    assert open(os.path.join(settings.shared_dir, "w", "a.txt")).read() == "new"


def test_zip_bomb_ratio_is_rejected(as_teacher, settings):
    data = make_zip({"boom/zeros.bin": b"\0" * (3 * 1024 * 1024)})
    r = upload(as_teacher, "boom.zip", data, unzip=1)
    assert r.status_code == 400 and r.json()["error"] == "bad_zip"
    assert shared_names(settings) == []


def test_zip_with_too_many_entries_or_too_big_is_413(as_teacher, settings, app):
    app.state.settings.max_zip_entries = 2
    r = upload(as_teacher, "many.zip", make_zip({"m/a": b"1", "m/b": b"2", "m/c": b"3"}), unzip=1)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    app.state.settings.max_zip_entries = 20000
    app.state.settings.max_unzipped_bytes = 10
    r = upload(as_teacher, "big.zip", make_zip({"b/a.txt": b"x" * 50}), unzip=1)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert shared_names(settings) == []


def test_corrupt_zip_is_400_and_leaves_nothing(as_teacher, settings):
    r = upload(as_teacher, "broken.zip", b"PK\x03\x04 this is not really a zip", unzip=1)
    assert r.status_code == 400 and r.json()["error"] == "bad_zip"
    assert shared_names(settings) == []


# --- handouts: folders, delete, download ------------------------------------------------------------


def test_mkdir(as_teacher, settings):
    r = as_teacher.post(f"{API}/handouts/mkdir", json={"path": "week2"})
    assert r.status_code == 201 and r.json() == {"path": "week2"}
    assert stat.S_IMODE(os.stat(os.path.join(settings.shared_dir, "week2")).st_mode) == 0o2775
    assert as_teacher.post(f"{API}/handouts/mkdir", json={"path": "week2"}).status_code == 409
    assert as_teacher.post(f"{API}/handouts/mkdir", json={"path": "week2/part-a"}).status_code == 201
    assert as_teacher.post(f"{API}/handouts/mkdir", json={"path": "nope/sub"}).status_code == 404
    for bad in (".hidden", "a/../b", "", "x\x00y", "a\\b"):
        r = as_teacher.post(f"{API}/handouts/mkdir", json={"path": bad})
        assert r.status_code == 400, bad
    assert as_teacher.post(f"{API}/handouts/mkdir", json={"nope": 1}).status_code == 400


def test_delete_file_and_folder(as_teacher, settings):
    shared = settings.shared_dir
    with open(os.path.join(shared, "gone.txt"), "w") as fh:
        fh.write("x")
    os.makedirs(os.path.join(shared, "tree", "deep"))
    with open(os.path.join(shared, "tree", "deep", "f.txt"), "w") as fh:
        fh.write("x")
    assert as_teacher.delete(f"{API}/handouts", params={"path": "gone.txt"}).status_code == 204
    assert as_teacher.delete(f"{API}/handouts", params={"path": "tree"}).status_code == 204
    assert shared_names(settings) == []
    assert as_teacher.delete(f"{API}/handouts", params={"path": "gone.txt"}).status_code == 404


def test_delete_refuses_root_and_traversal(as_teacher, settings):
    for root in ("", "/", ".", "./"):
        r = as_teacher.delete(f"{API}/handouts", params={"path": root})
        assert r.status_code == 400 and r.json()["error"] == "refuse_root", root
    with open(os.path.join(settings.shared_dir, "keep.txt"), "w") as fh:
        fh.write("x")
    for bad in ("../shared", "/keep.txt", "/" + os.path.join(settings.shared_dir, "keep.txt"), "a\x00b"):
        r = as_teacher.delete(f"{API}/handouts", params={"path": bad})
        assert r.status_code == 400 and r.json()["error"] == "bad_path", bad
    assert os.path.isdir(settings.shared_dir) and shared_names(settings) == ["keep.txt"]


def test_delete_never_follows_links(as_teacher, settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    shared = settings.shared_dir
    os.symlink(str(outside), os.path.join(shared, "dirlink"))
    os.symlink(str(outside / "keep.txt"), os.path.join(shared, "filelink"))
    # through a link: refused, target untouched
    r = as_teacher.delete(f"{API}/handouts", params={"path": "dirlink/keep.txt"})
    assert r.status_code == 400 and (outside / "keep.txt").exists()
    # the links themselves: removed, targets untouched
    assert as_teacher.delete(f"{API}/handouts", params={"path": "dirlink"}).status_code == 204
    assert as_teacher.delete(f"{API}/handouts", params={"path": "filelink"}).status_code == 204
    assert (outside / "keep.txt").read_text() == "keep" and outside.is_dir()
    assert shared_names(settings) == []


def test_download_handout(as_teacher, settings):
    with open(os.path.join(settings.shared_dir, "réport.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4 fake")
    r = as_teacher.get(f"{API}/handouts/download", params={"path": "réport.pdf"})
    assert r.status_code == 200 and r.content == b"%PDF-1.4 fake"
    assert r.headers["content-length"] == "13"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "r_port.pdf" in r.headers["content-disposition"] and "r%C3%A9port.pdf" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"


def test_download_refuses_folders_links_and_bad_paths(as_teacher, settings, tmp_path):
    shared = settings.shared_dir
    os.mkdir(os.path.join(shared, "folder"))
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    os.symlink(str(secret), os.path.join(shared, "link"))
    with open(os.path.join(shared, "inside.txt"), "w") as fh:
        fh.write("inside")
    os.symlink(os.path.join(shared, "inside.txt"), os.path.join(shared, "inner-link"))
    cases = (
        ("folder", 400, "not_a_file"), ("link", 400, "not_a_file"), ("inner-link", 400, "not_a_file"),
        ("../secret.txt", 400, "bad_path"), ("/" + str(secret), 400, "bad_path"), ("", 400, "bad_path"),
        ("missing.txt", 404, "not_found"), ("folder/../inside.txt", 400, "bad_path"),
    )
    for bad, status, code in cases:
        r = as_teacher.get(f"{API}/handouts/download", params={"path": bad})
        assert r.status_code == status and r.json()["error"] == code, bad
        assert b"secret" not in r.content


def test_download_errors_are_friendly_pages_for_link_clicks(as_teacher):
    r = as_teacher.get(f"{API}/handouts/download", params={"path": "missing.txt"}, headers={"Accept": "text/html"})
    assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    assert "Back to Handouts" in r.text


# --- submissions -------------------------------------------------------------------------------------


@pytest.fixture
def classroom(settings, fake_system, hub):
    """Roster with two students, one admin and one name without a Linux account."""
    # The real file walker checks filesystem ownership; use the runner's actual
    # uid so the fixture works unprivileged on macOS and as root in Linux.
    fake_system.add_user("student01", os.getuid())
    fake_system.add_user("student02", os.getuid())
    fake_system.make_home("student01", 2001, {"submit/hw1.ipynb": "{}", "submit/notes/n.txt": "abc", "Welcome.ipynb": "x"})
    fake_system.make_home("student02", 2002, {"submit/README.md": "# empty"})
    os.symlink("/etc/passwd", os.path.join(settings.home_root, "student01", "submit", "passwd"))
    os.mkfifo(os.path.join(settings.home_root, "student01", "submit", "pipe"))
    save_roster(settings, ("student01", "Student One"), ("student02", "Student Two"), ("teacher", "The Teacher"), ("ghost", "No Account"))
    return settings


def test_submissions_listing(as_teacher, classroom):
    r = as_teacher.get(f"{API}/submissions")
    assert r.status_code == 200
    rows = {e["username"]: e for e in r.json()}
    assert set(rows) == {"student01", "student02", "teacher", "ghost"}
    s1 = rows["student01"]
    assert s1["display_name"] == "Student One" and s1["is_admin"] is False and s1["exists"] is True
    assert s1["file_count"] == 2 and s1["total_bytes"] == 5 and s1["latest_mtime"] and s1["capped"] is False
    assert rows["student02"]["file_count"] == 1
    t = rows["teacher"]
    assert t["is_admin"] is True and t["exists"] is False and t["file_count"] == 0 and t["latest_mtime"] is None
    g = rows["ghost"]
    assert g["account"] is False and g["exists"] is False and g["file_count"] == 0
    assert [e["username"] for e in r.json()][:2] == ["student01", "student02"] or r.json()[0]["latest_mtime"] >= r.json()[1]["latest_mtime"]
    assert [e["username"] for e in r.json()][2:] == ["ghost", "teacher"]      # no files: alphabetical


def test_submissions_listing_with_empty_roster(as_teacher, settings):
    assert as_teacher.get(f"{API}/submissions").json() == []


def test_submit_folder_that_is_a_link_is_ignored(as_teacher, classroom, tmp_path, fake_system):
    fake_system.add_user("student03", 2003)
    home = fake_system.make_home("student03", 2003)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x")
    os.symlink(str(outside), os.path.join(home, "submit"))
    save_roster(classroom, ("student03", "Linky"))
    row = as_teacher.get(f"{API}/submissions").json()[0]
    assert row["username"] == "student03" and row["exists"] is False and row["file_count"] == 0
    r = as_teacher.get(f"{API}/submissions/student03/download")
    assert r.status_code == 200 and zip_names(r) == ["README.txt"]


def test_walk_only_yields_regular_files_owned_by_the_student(classroom, fake_system, monkeypatch):
    submit = os.path.join(classroom.home_root, "student01", "submit")
    found = []
    for rel, fd, size, mtime in files_mod._walk_submit(submit, os.getuid()):
        os.close(fd)
        found.append((rel, size))
    assert found == [("hw1.ipynb", 2), ("notes/n.txt", 3)]
    # as root the owner check is strict: a different uid sees nothing
    monkeypatch.setattr(files_mod.os, "geteuid", lambda: 0)
    assert list(files_mod._walk_submit(submit, os.getuid() + 1)) == []
    got = list(files_mod._walk_submit(submit, os.getuid()))
    for _, fd, _, _ in got:
        os.close(fd)
    assert [g[0] for g in got] == ["hw1.ipynb", "notes/n.txt"]
    assert list(files_mod._walk_submit(os.path.join(classroom.home_root, "nobody", "submit"), 1)) == []


def test_download_one_student_zip(as_teacher, classroom):
    r = as_teacher.get(f"{API}/submissions/student01/download")
    assert r.status_code == 200
    assert "submit-student01-" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["hw1.ipynb", "notes/n.txt"]     # link and fifo skipped
    assert zf.read("notes/n.txt") == b"abc"
    assert os.listdir(classroom.tmp_dir) == []                        # temp zip removed after sending


def test_download_empty_submit_folder_gives_readme(as_teacher, classroom, fake_system):
    fake_system.add_user("student04", 2004)
    home = fake_system.make_home("student04", 2004)
    os.mkdir(os.path.join(home, "submit"))
    save_roster(classroom, ("student04", "Quiet"))
    r = as_teacher.get(f"{API}/submissions/student04/download")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == ["README.txt"] and b"Quiet (student04)" in zf.read("README.txt")


def test_download_student_validation(as_teacher, classroom):
    r = as_teacher.get(f"{API}/submissions/Root/download")
    assert r.status_code == 400 and r.json()["error"] == "bad_username"
    r = as_teacher.get(f"{API}/submissions/bad%20name/download")
    assert r.status_code == 400
    r = as_teacher.get(f"{API}/submissions/nobody/download")
    assert r.status_code == 400 and r.json()["error"] == "bad_username"      # reserved name
    r = as_teacher.get(f"{API}/submissions/zoe/download")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = as_teacher.get(f"{API}/submissions/zoe/download", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "Back to Handouts" in r.text
    assert os.listdir(classroom.tmp_dir) == []


def test_download_all_uses_username_folders(as_teacher, classroom):
    r = as_teacher.get(f"{API}/submissions/download-all")
    assert r.status_code == 200
    assert "submissions-test-school-" in r.headers["content-disposition"]
    names = zip_names(r)
    assert names == ["ghost/README.txt", "student01/hw1.ipynb", "student01/notes/n.txt", "student02/README.md", "teacher/README.txt"]
    assert os.listdir(classroom.tmp_dir) == []


def test_download_all_with_empty_roster(as_teacher, settings):
    r = as_teacher.get(f"{API}/submissions/download-all")
    assert r.status_code == 200 and zip_names(r) == ["README.txt"]


def test_submission_caps_point_at_backups(as_teacher, classroom, monkeypatch):
    monkeypatch.setattr(files_mod, "SUBMISSION_MAX_FILES", 1)
    r = as_teacher.get(f"{API}/submissions/student01/download")
    assert r.status_code == 413
    body = r.json()
    assert body["error"] == "too_large" and body["detail"]["hint"] == "backups" and "student01" in body["message"]
    assert os.listdir(classroom.tmp_dir) == []
    r = as_teacher.get(f"{API}/submissions").json()
    assert next(e for e in r if e["username"] == "student01")["capped"] is True
    monkeypatch.setattr(files_mod, "SUBMISSION_MAX_FILES", 5000)
    monkeypatch.setattr(files_mod, "SUBMISSION_MAX_FILE_BYTES", 2)
    r = as_teacher.get(f"{API}/submissions/student01/download")
    assert r.status_code == 200 and zip_names(r) == ["hw1.ipynb"]          # the 3-byte file is over the per-file cap
    assert next(e for e in as_teacher.get(f"{API}/submissions").json() if e["username"] == "student01")["large_files"] == 1


def test_download_all_total_cap(as_teacher, classroom, app):
    app.state.settings.max_submission_zip_bytes = 3
    r = as_teacher.get(f"{API}/submissions/download-all")
    assert r.status_code == 413 and "All the submissions" in r.json()["message"]
    assert os.listdir(classroom.tmp_dir) == []


def test_zip_builds_are_limited_to_two_at_a_time(as_teacher, classroom):
    assert files_mod.ZIP_SLOTS.acquire(blocking=False) and files_mod.ZIP_SLOTS.acquire(blocking=False)
    try:
        r = as_teacher.get(f"{API}/submissions/student01/download")
        assert r.status_code == 409 and r.json()["error"] == "busy"
    finally:
        files_mod.ZIP_SLOTS.release()
        files_mod.ZIP_SLOTS.release()
    assert as_teacher.get(f"{API}/submissions/student01/download").status_code == 200
