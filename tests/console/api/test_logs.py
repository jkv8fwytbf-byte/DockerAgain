"""Logs feature: tail, cursor follow, rotation, tracebacks, download safety, permissions."""
from __future__ import annotations

import os
import time

import pytest

from console.logs import LogChunk, read_since, tail

TS = "2026-09-13 08:00:00"


def line(level: str, msg: str, ts: str = TS, logger: str = "JupyterHub app:123") -> str:
    return f"[{level} {ts} {logger}] {msg}\n"


def write(path: str, text: str, mode: str = "w") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="") as fh:
        fh.write(text)


def write_bytes(path: str, data: bytes, mode: str = "wb") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as fh:
        fh.write(data)


@pytest.fixture
def hub_log(settings):
    return settings.hub_log_file


@pytest.fixture
def console_log(settings):
    return settings.console_log_file


# --- tail -----------------------------------------------------------------------------

def test_tail_returns_last_n_lines_newest_last(as_teacher, prefix, hub_log):
    write(hub_log, "".join(line("I", f"message {i}") for i in range(300)))
    r = as_teacher.get(f"{prefix}/api/logs?source=hub&limit=100")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["source"] == "hub" and body["file"] == "jupyterhub.log"
    assert len(body["lines"]) == 100
    assert body["lines"][0]["msg"] == "message 200" and body["lines"][-1]["msg"] == "message 299"
    assert body["truncated"] is True and body["rotated"] is False
    assert body["cursor"] == body["size"] == os.path.getsize(hub_log)


def test_tail_whole_file_is_not_truncated(as_teacher, prefix, hub_log):
    write(hub_log, "".join(line("I", f"m{i}") for i in range(5)))
    body = as_teacher.get(f"{prefix}/api/logs?source=hub").json()
    assert [e["msg"] for e in body["lines"]] == ["m0", "m1", "m2", "m3", "m4"]
    assert body["truncated"] is False


def test_tail_parses_levels_logger_and_timestamp(as_teacher, prefix, hub_log):
    write(hub_log, line("D", "dbg") + line("I", "inf") + line("W", "wrn") + line("E", "err") + line("C", "crit")
          + line("I", "console style", logger="uvicorn.error") + line("X", "unknown letter"))
    lines = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["lines"]
    assert [e["level"] for e in lines] == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "INFO", "OTHER"]
    assert lines[0] == {"ts": TS, "level": "DEBUG", "logger": "JupyterHub", "msg": "dbg", "raw": line("D", "dbg").rstrip("\n")}
    assert lines[5]["logger"] == "uvicorn.error" and lines[5]["msg"] == "console style"
    assert lines[6]["msg"] == "unknown letter" and lines[6]["ts"] == TS


def test_console_source_reads_console_log(as_teacher, prefix, console_log, hub_log):
    write(hub_log, line("I", "hub side"))
    write(console_log, line("I", "console side", logger="console"))
    body = as_teacher.get(f"{prefix}/api/logs?source=console").json()
    assert body["source"] == "console" and body["file"] == "console.log"
    assert [e["msg"] for e in body["lines"]] == ["console side"]


def test_missing_log_file_is_empty_not_an_error(as_teacher, prefix, console_log):
    assert not os.path.exists(console_log)
    body = as_teacher.get(f"{prefix}/api/logs?source=console").json()
    assert body == {"source": "console", "file": "console.log", "lines": [], "cursor": 0, "truncated": False, "rotated": False,
                    "size": 0, "utc_offset": time.localtime().tm_gmtoff}


def test_response_carries_the_server_clock_offset(as_teacher, prefix, hub_log):
    # log stamps are the server's local time; the page converts them to the browser's zone with this
    write(hub_log, line("I", "x"))
    body = as_teacher.get(f"{prefix}/api/logs?source=hub").json()
    assert body["utc_offset"] == time.localtime().tm_gmtoff and isinstance(body["utc_offset"], int)
    assert -14 * 3600 <= body["utc_offset"] <= 14 * 3600
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&after={body['cursor']}").json()
    assert body["utc_offset"] == time.localtime().tm_gmtoff


def test_filter_is_case_insensitive_substring_on_raw(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "Priya logged in") + line("I", "someone else") + line("W", "PRIYA's server stopped") + line("I", "bye"))
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&filter=priya").json()
    assert [e["msg"] for e in body["lines"]] == ["Priya logged in", "PRIYA's server stopped"]
    # the filter also sees the header part of the raw line (level letter, logger)
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&filter=app:123").json()
    assert len(body["lines"]) == 4
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&filter=nomatch").json()
    assert body["lines"] == [] and body["cursor"] == os.path.getsize(hub_log)


def test_filter_limit_applies_to_matching_lines(as_teacher, prefix, hub_log):
    write(hub_log, "".join(line("I", f"keep {i}") + line("I", f"skip {i}") for i in range(50)))
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&filter=keep&limit=10").json()
    assert [e["msg"] for e in body["lines"]] == [f"keep {i}" for i in range(40, 50)]
    assert body["truncated"] is True


# --- cursor follow --------------------------------------------------------------------

def test_cursor_returns_only_appended_lines(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "one") + line("I", "two"))
    first = as_teacher.get(f"{prefix}/api/logs?source=hub").json()
    cursor = first["cursor"]
    nothing = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert nothing["lines"] == [] and nothing["cursor"] == cursor and nothing["rotated"] is False
    write(hub_log, line("I", "three") + line("W", "four"), mode="a")
    more = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert [e["msg"] for e in more["lines"]] == ["three", "four"]
    assert more["cursor"] == os.path.getsize(hub_log) and more["truncated"] is False
    again = as_teacher.get(f"{prefix}/api/logs?source=hub&after={more['cursor']}").json()
    assert again["lines"] == []


def test_cursor_waits_for_a_half_written_line(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "complete"))
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    write(hub_log, "[I 2026-09-13 08:00:01 JupyterHub app:1] not yet fin", mode="a")
    pending = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert pending["lines"] == [] and pending["cursor"] == cursor
    tail_view = as_teacher.get(f"{prefix}/api/logs?source=hub").json()
    assert [e["msg"] for e in tail_view["lines"]] == ["complete"] and tail_view["cursor"] == cursor
    write(hub_log, "ished\n", mode="a")
    done = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert [e["msg"] for e in done["lines"]] == ["not yet finished"]
    assert done["cursor"] == os.path.getsize(hub_log)


def test_cursor_limit_continues_without_gaps(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "start"))
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    write(hub_log, "".join(line("I", f"burst {i}") for i in range(25)), mode="a")
    seen = []
    for _ in range(10):
        page = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}&limit=10").json()
        seen += [e["msg"] for e in page["lines"]]
        cursor = page["cursor"]
        if not page["truncated"]:
            break
    assert seen == [f"burst {i}" for i in range(25)]
    assert cursor == os.path.getsize(hub_log)


def test_rotation_is_detected_and_tail_returned(as_teacher, prefix, hub_log):
    write(hub_log, "".join(line("I", f"old {i}") for i in range(20)))
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    os.replace(hub_log, hub_log + ".1")                       # what RotatingFileHandler does
    write(hub_log, line("I", "fresh 1") + line("I", "fresh 2"))
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert body["rotated"] is True
    assert [e["msg"] for e in body["lines"]] == ["fresh 1", "fresh 2"]
    assert body["cursor"] == os.path.getsize(hub_log)
    # rotated to nothing yet: still fine
    os.unlink(hub_log)
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert body["rotated"] is True and body["lines"] == [] and body["cursor"] == 0


# --- tracebacks and odd bytes --------------------------------------------------------

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/opt/conda/lib/python3.12/site-packages/jupyterhub/app.py", line 10, in start\n'
    "    raise KeyError('priya')\n"
    "KeyError: 'priya'\n"
)


def test_traceback_lines_join_previous_entry_in_tail(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "before") + line("E", "Spawn failed") + TRACEBACK + line("I", "after"))
    lines = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["lines"]
    assert [e["level"] for e in lines] == ["INFO", "ERROR", "INFO"]
    err = lines[1]
    assert err["msg"] == "Spawn failed\n" + TRACEBACK.rstrip("\n")
    assert err["raw"].startswith("[E ") and err["raw"].endswith("KeyError: 'priya'")
    # the filter sees the whole entry, so a search for the exception finds it
    hits = as_teacher.get(f"{prefix}/api/logs?source=hub&filter=keyerror").json()["lines"]
    assert len(hits) == 1 and hits[0]["level"] == "ERROR"


def test_traceback_lines_join_previous_entry_when_following(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "before"))
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    write(hub_log, line("E", "Spawn failed") + TRACEBACK + line("I", "after"), mode="a")
    lines = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()["lines"]
    assert [e["level"] for e in lines] == ["ERROR", "INFO"]
    assert lines[0]["msg"].endswith("KeyError: 'priya'")


def test_headerless_lines_at_file_start_become_one_other_entry(as_teacher, prefix, hub_log):
    write(hub_log, "first line no header\nsecond line no header\n" + line("I", "real"))
    lines = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["lines"]
    assert [e["level"] for e in lines] == ["OTHER", "INFO"]
    assert lines[0]["msg"] == "first line no header\nsecond line no header"
    assert "continues" not in lines[0]


# A record flushed in two writes (or a poll that ran between header and traceback): the traceback comes
# back as a continuation of the ERROR entry, never as a header-less OTHER line.

HEADER = line("E", "Spawn failed for student01")


def test_traceback_arriving_after_its_header_continues_the_error_entry(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "before") + HEADER)
    first = as_teacher.get(f"{prefix}/api/logs?source=hub").json()
    assert [(e["level"], e["msg"]) for e in first["lines"]] == [("INFO", "before"), ("ERROR", "Spawn failed for student01")]
    write(hub_log, TRACEBACK, mode="a")
    second = as_teacher.get(f"{prefix}/api/logs?source=hub&after={first['cursor']}").json()
    assert len(second["lines"]) == 1
    cont = second["lines"][0]
    assert cont["level"] == "ERROR" and cont["ts"] == TS and cont["logger"] == "JupyterHub" and cont["continues"] is True
    assert cont["msg"] == TRACEBACK.rstrip("\n")
    assert cont["raw"] == HEADER.rstrip("\n") + "\n" + TRACEBACK.rstrip("\n")   # header first, so the page knows where it belongs
    assert second["cursor"] == os.path.getsize(hub_log)
    # more of the same traceback plus a fresh line: still one continuation, then the new entry
    write(hub_log, "    during handling of the above\n" + line("I", "after"), mode="a")
    third = as_teacher.get(f"{prefix}/api/logs?source=hub&after={second['cursor']}").json()
    assert [(e["level"], e.get("continues", False)) for e in third["lines"]] == [("ERROR", True), ("INFO", False)]
    assert third["lines"][0]["msg"] == "    during handling of the above"
    assert "continues" not in third["lines"][1]
    # a normal poll (header and traceback written together) is unchanged
    write(hub_log, line("E", "again") + TRACEBACK, mode="a")
    fourth = as_teacher.get(f"{prefix}/api/logs?source=hub&after={third['cursor']}").json()
    assert len(fourth["lines"]) == 1 and "continues" not in fourth["lines"][0]
    assert fourth["lines"][0]["msg"] == "again\n" + TRACEBACK.rstrip("\n")


def test_continuation_entry_is_found_by_the_filter_through_its_header(as_teacher, prefix, hub_log):
    write(hub_log, HEADER)
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub&filter=keyerror").json()["cursor"]
    write(hub_log, TRACEBACK, mode="a")
    # the traceback text matches: returned with the header's level so the page shows it red
    hits = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}&filter=keyerror").json()["lines"]
    assert len(hits) == 1 and hits[0]["level"] == "ERROR" and hits[0]["continues"] is True
    # the header text matches (the traceback alone does not): the continuation still belongs to a matching entry
    hits = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}&filter=student01").json()["lines"]
    assert len(hits) == 1 and hits[0]["continues"] is True
    assert as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}&filter=nomatch").json()["lines"] == []


def test_continuation_without_any_header_before_it_is_other(as_teacher, prefix, hub_log):
    write(hub_log, "first line no header\n")
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    write(hub_log, "second line no header\n", mode="a")
    lines = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()["lines"]
    assert lines == [{"ts": "", "level": "OTHER", "logger": "", "msg": "second line no header", "raw": "second line no header", "continues": True}]
    # blank lines on their own are nothing to show
    write(hub_log, "\n\n", mode="a")
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    write(hub_log, "\n", mode="a")
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert body["lines"] == [] and body["cursor"] == os.path.getsize(hub_log)


def test_byte_budget_cut_inside_a_traceback_continues_it_next_poll(as_teacher, prefix, hub_log, settings):
    write(hub_log, line("I", "start"))
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    frames = "".join(f'  File "/opt/conda/lib/python3.12/site-packages/jupyterhub/app.py", line {i}, in step{i}\n' for i in range(100))
    write(hub_log, HEADER + frames + "KeyError: 'priya'\n" + line("I", "after"), mode="a")
    settings.log_tail_max_bytes = 2048
    first = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}").json()
    assert first["truncated"] is True and len(first["lines"]) == 1
    assert first["lines"][0]["level"] == "ERROR" and "continues" not in first["lines"][0]
    got = first["lines"][0]["msg"]
    for _ in range(20):
        page = as_teacher.get(f"{prefix}/api/logs?source=hub&after={first['cursor']}").json()
        first = page
        for e in page["lines"]:
            if e.get("continues"):
                assert e["level"] == "ERROR" and e["ts"] == TS
                got += "\n" + e["msg"]
            else:
                assert e["level"] == "INFO" and e["msg"] == "after"
        if not page["truncated"]:
            break
    assert got == "Spawn failed for student01\n" + frames + "KeyError: 'priya'"   # nothing lost, nothing repeated
    assert first["cursor"] == os.path.getsize(hub_log)


def test_continuation_header_is_found_across_small_blocks(tmp_path):
    path = str(tmp_path / "jupyterhub.log")
    prefix_text = "".join(line("I", f"x{i}") for i in range(40)) + HEADER
    write(path, prefix_text)
    cursor = len(prefix_text.encode())
    write(path, TRACEBACK + line("W", "next"), mode="a")
    for block in (5, 7, 64, 65536):
        chunk = read_since(path, cursor, 100, block=block)
        assert [(e["level"], e.get("continues", False)) for e in chunk.lines] == [("ERROR", True), ("WARNING", False)], block
        assert chunk.lines[0]["msg"] == TRACEBACK.rstrip("\n") and chunk.lines[0]["raw"].startswith(HEADER.rstrip("\n")), block
    # a limit of one stops right before the next header, so the follow-up poll starts on it
    chunk = read_since(path, cursor, 1, block=7)
    assert len(chunk.lines) == 1 and chunk.truncated is True
    rest = read_since(path, chunk.cursor, 10, block=7)
    assert [e["msg"] for e in rest.lines] == ["next"] and "continues" not in rest.lines[0]


def test_invalid_utf8_crlf_and_ansi_are_cleaned(as_teacher, prefix, hub_log):
    write_bytes(hub_log, b"[I 2026-09-13 08:00:00 JupyterHub app:1] caf\xc3\xa9 \xff end\r\n"
                b"\x1b[32m[W 2026-09-13 08:00:01 JupyterHub app:1]\x1b[0m coloured\n")
    lines = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["lines"]
    assert lines[0]["msg"] == "café � end" and not lines[0]["raw"].endswith("\r")
    assert lines[1]["level"] == "WARNING" and lines[1]["msg"] == "coloured"


# --- limits ---------------------------------------------------------------------------

def test_byte_budget_caps_how_far_back_we_read(as_teacher, prefix, hub_log, settings):
    write(hub_log, "".join(line("I", f"m{i:04d}") for i in range(2000)))
    settings.log_tail_max_bytes = 4096
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&limit=5000").json()
    assert 0 < len(body["lines"]) < 200
    assert body["lines"][-1]["msg"] == "m1999" and body["truncated"] is True


def test_byte_budget_when_following_returns_first_part_and_asks_to_continue(as_teacher, prefix, hub_log, settings):
    write(hub_log, line("I", "start"))
    cursor = as_teacher.get(f"{prefix}/api/logs?source=hub").json()["cursor"]
    write(hub_log, "".join(line("I", f"m{i:04d}") for i in range(500)), mode="a")
    settings.log_tail_max_bytes = 2048
    body = as_teacher.get(f"{prefix}/api/logs?source=hub&after={cursor}&limit=5000").json()
    assert body["truncated"] is True and body["lines"][0]["msg"] == "m0000"
    assert cursor < body["cursor"] < os.path.getsize(hub_log)
    rest = as_teacher.get(f"{prefix}/api/logs?source=hub&after={body['cursor']}&limit=5000").json()
    assert rest["lines"][0]["msg"] == f"m{len(body['lines']):04d}"   # no gap, no repeat


def test_tail_reads_across_block_boundaries(tmp_path):
    path = str(tmp_path / "jupyterhub.log")
    write(path, "".join(line("I", f"x{i}") for i in range(40)) + line("E", "err") + TRACEBACK)
    for block in (7, 33, 128, 65536):
        chunk = tail(path, 8, block=block)
        assert isinstance(chunk, LogChunk)
        assert [e["msg"] for e in chunk.lines[:7]] == ["x33", "x34", "x35", "x36", "x37", "x38", "x39"], block
        assert chunk.lines[-1]["level"] == "ERROR" and chunk.lines[-1]["msg"].endswith("KeyError: 'priya'"), block
        assert len(chunk.lines) == 8 and chunk.truncated is True and chunk.cursor == os.path.getsize(path), block
    whole = tail(path, 500, block=5)
    assert len(whole.lines) == 41 and whole.truncated is False and whole.lines[0]["msg"] == "x0"


def test_read_since_matches_tail_for_the_same_bytes(tmp_path):
    path = str(tmp_path / "jupyterhub.log")
    write(path, line("I", "a") + line("E", "b") + TRACEBACK + "loose\n" + line("W", "c"))
    assert [e["msg"] for e in read_since(path, 0, 100).lines] == [e["msg"] for e in tail(path, 100).lines]


@pytest.mark.parametrize("query", [
    "source=syslog", "source=", "limit=0", "limit=5001", "limit=abc", "after=-1", "after=x", "filter=" + "a" * 201,
])
def test_bad_query_is_400(as_teacher, prefix, query):
    r = as_teacher.get(f"{prefix}/api/logs?{query}")
    assert r.status_code == 400 and r.json()["error"] == "validation_failed"


# --- files list -----------------------------------------------------------------------

def test_files_lists_current_and_rotated_in_order(as_teacher, prefix, settings, hub_log):
    write(hub_log, line("I", "live"))
    write(hub_log + ".2", "older\n")
    write(hub_log + ".1", "old\n")
    write(hub_log + ".10", "oldest\n")
    write(settings.console_log_file + ".1", "not ours\n")
    write(hub_log + "\n", "trailing newline in the name\n")   # matches the pattern with "$", not with fullmatch
    os.symlink("/etc/hosts", hub_log + ".3")              # a planted link is never listed
    body = as_teacher.get(f"{prefix}/api/logs/files?source=hub").json()
    assert body["source"] == "hub"
    assert [f["name"] for f in body["files"]] == ["jupyterhub.log", "jupyterhub.log.1", "jupyterhub.log.2", "jupyterhub.log.10"]
    assert body["files"][0]["current"] is True and body["files"][1]["current"] is False
    assert body["files"][1]["size"] == 4 and body["files"][1]["modified"].endswith("Z")
    body = as_teacher.get(f"{prefix}/api/logs/files?source=console").json()
    assert [f["name"] for f in body["files"]] == ["console.log.1"]
    assert as_teacher.get(f"{prefix}/api/logs/files?source=bogus").status_code == 400


# --- download -------------------------------------------------------------------------

def test_download_current_log(as_teacher, prefix, hub_log):
    write(hub_log, line("I", "hello") + "tail\n")
    r = as_teacher.get(f"{prefix}/api/logs/download?source=hub")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;") and 'filename="jupyterhub-' in disposition and disposition.endswith('.log"')
    assert r.headers["cache-control"] == "no-store"
    assert r.content == open(hub_log, "rb").read()


def test_download_console_and_rotated_file(as_teacher, prefix, console_log, hub_log):
    write(console_log, "console text\n")
    write(hub_log + ".1", "rotated text\n")
    r = as_teacher.get(f"{prefix}/api/logs/download?source=console")
    assert r.status_code == 200 and r.content == b"console text\n" and 'filename="console-' in r.headers["content-disposition"]
    r = as_teacher.get(f"{prefix}/api/logs/download?file=jupyterhub.log.1")
    assert r.status_code == 200 and r.content == b"rotated text\n"
    assert "-part1.log" in r.headers["content-disposition"]


@pytest.mark.parametrize("name", [
    "../roster.json", "..%2Froster.json", "/etc/passwd", "roster.json", "jupyterhub.log.123", "jupyterhub.log.",
    "jupyterhub.log.1/", "console_secret", "jupyterhub.log.1%00.txt", "JUPYTERHUB.LOG", "jupyterhub.log.1.bak",
    "..\\roster.json", "logs/jupyterhub.log", "jupyterhub.log.1" + "x" * 40,
    "jupyterhub.log%0A", "jupyterhub.log.1%0A", "console.log%0D%0A", "%0Ajupyterhub.log",
])
def test_download_rejects_other_names(as_teacher, prefix, settings, name):
    write(settings.roster_path, '{"version": 1, "users": [{"username": "a", "password": "secret"}]}')
    r = as_teacher.get(f"{prefix}/api/logs/download?file={name}")
    assert r.status_code == 400, name
    assert r.json()["error"] in ("bad_name", "validation_failed")
    assert b"secret" not in r.content


def test_download_name_with_trailing_newline_is_rejected_even_when_such_a_file_exists(as_teacher, prefix, hub_log):
    write(hub_log + "\n", "odd name\n")
    r = as_teacher.get(f"{prefix}/api/logs/download?file=jupyterhub.log%0A")
    assert r.status_code == 400 and r.json()["error"] == "bad_name" and b"odd name" not in r.content


def test_download_missing_file_is_404(as_teacher, prefix, hub_log):
    r = as_teacher.get(f"{prefix}/api/logs/download?file=jupyterhub.log.4")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert not os.path.exists(hub_log)
    r = as_teacher.get(f"{prefix}/api/logs/download?source=hub")
    assert r.status_code == 404


def test_download_never_follows_a_symlink(as_teacher, prefix, settings, hub_log, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("outside secret\n")
    os.makedirs(settings.log_dir, exist_ok=True)
    os.symlink(str(secret), hub_log + ".1")
    r = as_teacher.get(f"{prefix}/api/logs/download?file=jupyterhub.log.1")
    assert r.status_code == 404 and b"outside secret" not in r.content
    os.symlink(str(secret), hub_log)
    r = as_teacher.get(f"{prefix}/api/logs/download?source=hub")
    assert r.status_code == 404


# --- permissions ----------------------------------------------------------------------

def test_logs_api_requires_a_session(client, prefix, hub_log):
    write(hub_log, line("I", "secret-ish"))
    for path in ("api/logs?source=hub", "api/logs/files?source=hub", "api/logs/download?source=hub"):
        r = client.get(f"{prefix}/{path}")
        assert r.status_code == 401 and r.json()["error"] == "not_authenticated", path
        assert b"secret-ish" not in r.content


def test_logs_api_forbidden_for_students(as_student, prefix, hub_log):
    write(hub_log, line("I", "secret-ish"))
    for path in ("api/logs?source=hub", "api/logs/files?source=hub", "api/logs/download?source=hub"):
        r = as_student.get(f"{prefix}/{path}")
        assert r.status_code == 403 and r.json()["error"] == "forbidden", path
        assert b"secret-ish" not in r.content


# --- page -----------------------------------------------------------------------------

def test_logs_page_renders_viewer(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/logs")
    assert r.status_code == 200
    assert 'id="logs-view"' in r.text and 'role="log"' in r.text
    assert 'id="logs-source"' in r.text and 'id="logs-filter"' in r.text and 'id="logs-level"' in r.text
    assert 'id="logs-autoscroll"' in r.text and 'id="logs-clear"' in r.text and 'id="logs-jump"' in r.text
    assert 'id="logs-loading"' in r.text and 'id="logs-empty" class="cc-empty cc-logs__empty" hidden' in r.text
    assert 'id="logs-zone"' in r.text and 'id="logs-alert"' in r.text
    assert f"{prefix}/api/logs/download?source=hub" in r.text
    assert f"{prefix}/static/logs.js" in r.text and f"{prefix}/static/logs.css" in r.text
    assert as_teacher.get(f"{prefix}/static/logs.js").status_code == 200
    assert as_teacher.get(f"{prefix}/static/logs.css").status_code == 200
