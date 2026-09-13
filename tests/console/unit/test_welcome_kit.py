"""
Welcome kit (skel/) and JupyterLab defaults (lab/overrides.json): the files
baked into the image for every student, plus the backfill that puts them into
homes that already exist (console.accounts, delivered through console.system).

The backfill security tests run against the REAL console.system.System, not
the fake, because that is where the hardening lives: every directory
component is opened O_NOFOLLOW relative to its parent, the file is created
O_EXCL, and the "kit delivered" marker lives in the root-only state dir, never
in the student's home. They work unprivileged (they chown to our own uid/gid)
and as root inside the image (`make test-api`).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import threading

import nbformat
import pytest

from console.accounts import Accounts
from console.system import System

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SKEL = os.path.join(ROOT, "skel")
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
IN_IMAGE = ROOT == "/opt/classroom"          # `make test-api` runs this suite inside the container

# On the Mac the repo copy; inside the image (-w /opt/classroom) only the installed
# copy exists, and checking the installed one is the more meaningful test anyway.
LAB_OVERRIDES_INSTALLED = "/opt/conda/share/jupyter/lab/settings/overrides.json"
_OVERRIDES_CANDIDATES = [os.path.join(ROOT, "lab", "overrides.json"), LAB_OVERRIDES_INSTALLED]
OVERRIDES = next((p for p in _OVERRIDES_CANDIDATES if os.path.isfile(p)), _OVERRIDES_CANDIDATES[0])

KIT_FILES = ["Welcome.ipynb", "notebooks/README.md", "submit/README.md"]

# A backfill of three small files takes milliseconds; a FIFO in the way must not make it hang.
BACKFILL_WAIT_S = float(os.environ.get("KIT_TEST_WAIT", "0.2"))

ME = os.getuid()


def _is_finder_junk(name: str) -> bool:
    return name == ".DS_Store" or name.startswith("._")


@pytest.fixture(scope="module")
def notebook():
    with open(os.path.join(SKEL, "Welcome.ipynb"), encoding="utf-8") as fh:
        return nbformat.read(fh, as_version=4)


def _code_cells(nb):
    return [c for c in nb.cells if c.cell_type == "code"]


# --- Welcome.ipynb ---------------------------------------------------------------

def test_notebook_is_valid_nbformat_4_5(notebook):
    nbformat.validate(notebook)
    assert (notebook.nbformat, notebook.nbformat_minor) == (4, 5)
    assert notebook.metadata.kernelspec == {
        "name": "python3", "display_name": "Python 3 (ipykernel)", "language": "python",
    }
    ids = [c.id for c in notebook.cells]
    assert len(ids) == len(set(ids)), "cell ids must be unique"


def test_notebook_is_stored_in_nbformat_canonical_form():
    """Byte-for-byte what nbformat.write produces: JupyterLab will not rewrite it on first save
    for formatting reasons only, and diffs stay readable when the kit is edited."""
    with open(os.path.join(SKEL, "Welcome.ipynb"), encoding="utf-8") as fh:
        raw = fh.read()
    assert raw == nbformat.writes(nbformat.reads(raw, as_version=4)) + "\n"


def test_notebook_has_the_fifteen_planned_cells(notebook):
    kinds = [c.cell_type for c in notebook.cells]
    assert len(kinds) == 15
    assert kinds == [
        "markdown", "markdown", "code", "markdown", "markdown", "code", "markdown", "code",
        "markdown", "code", "markdown", "code", "markdown", "code", "markdown",
    ]
    headings = [c.source.splitlines()[0] for c in notebook.cells if c.cell_type == "markdown"]
    assert headings == [
        "# Welcome to your JupyterLab",
        "## 1. How a notebook works",
        "## 2. Saving your work",
        "## 3. Your folders",
        "## 4. Handing in work",
        "## 5. Quick check (2 seconds)",
        "## 6. Full check (optional, ~30 s, loads PyTorch and TensorFlow)",
        "## 7. Radio (SDR) libraries",
        "## Tips",
    ]


def test_notebook_is_clean_no_outputs_or_execution_counts(notebook):
    for cell in _code_cells(notebook):
        assert cell.outputs == [], cell.id
        assert cell.execution_count is None, cell.id
    for cell in notebook.cells:
        assert cell.metadata == {}, cell.id      # no tags, no collapsed state, nothing sticky


@pytest.mark.parametrize("phrase", [
    "Shift+Enter",
    "[*]",
    'print("Hello! Everything is working.")',
    "Ctrl+S",
    "Cmd+S",
    "`notebooks/`",
    "`submit/`",
    "`shared/`",
    "03-loops-priya.ipynb",
    "getpass.getuser()",
    "hello-from-",
    "Your teacher can now see this file in Submissions.",
    "%run /srv/smoke_test.py",
    "Only run this if your teacher asks",
    "pyrtlsdr",
    "SoapySDR.Device.enumerate()",
    "from rtlsdr import RtlSdr",
    "No radio plugged in — that's fine for now.",
    "signal.welch",
    "Restart Kernel",
    "Ask your teacher",
    "Made with Classroom JupyterHub",
])
def test_notebook_mentions(notebook, phrase):
    text = "\n".join(c.source for c in notebook.cells)
    assert phrase in text


def test_notebook_code_cells_are_valid_python(notebook):
    for cell in _code_cells(notebook):
        plain = "\n".join(
            line for line in cell.source.splitlines() if not line.lstrip().startswith(("%", "!"))
        )
        compile(plain, cell.id, "exec")   # raises SyntaxError on a broken cell


def test_notebook_uses_only_libraries_in_the_image(notebook):
    text = "\n".join(c.source for c in _code_cells(notebook))
    for lib in ("numpy", "pandas", "matplotlib", "scipy", "SoapySDR", "rtlsdr", "getpass", "pathlib"):
        assert lib in text
    assert "pip install" not in text and "conda install" not in text


def test_hand_in_demo_writes_only_inside_submit(notebook):
    cell = next(c for c in _code_cells(notebook) if "hello-from-" in c.source)
    assert 'Path.home() / "submit"' in cell.source
    assert "write_text" in cell.source
    assert "open(" not in cell.source           # one file, via pathlib, no stray handles


def test_radio_cell_points_soapysdr_at_its_drivers_before_its_first_look(notebook):
    """The Hub forwards only PATH to student servers (c.Spawner.environment), so the image's
    ENV SOAPY_SDR_PLUGIN_PATH never reaches a kernel. SoapySDR reads the variable when it loads
    its driver modules, which happens exactly once per kernel, at the first Device.enumerate():
    set it any later and the RTL-SDR driver stays unloaded for the rest of the session, dongle
    or no dongle. Setting it before the import is the simplest way to be early enough, and the
    section's markdown tells students to do the same at the top of their own notebooks."""
    cell = next(c for c in _code_cells(notebook) if "SoapySDR.Device.enumerate()" in c.source)
    src = cell.source
    assert 'os.environ.setdefault("SOAPY_SDR_PLUGIN_PATH", "/opt/conda/lib/SoapySDR/modules0.8")' in src
    assert src.index("SOAPY_SDR_PLUGIN_PATH") < src.index("import SoapySDR")
    intro = next(c for c in notebook.cells if c.cell_type == "markdown" and c.source.startswith("## 7. Radio"))
    assert "`SOAPY_SDR_PLUGIN_PATH`" in intro.source and "notebook of your own" in intro.source


def test_radio_cell_does_not_claim_no_radio_when_the_driver_never_loaded(notebook):
    """Because that loader is one-shot, a kernel whose first enumerate() ran without the variable
    (an earlier cell, or a Hub that did not forward it) returns () even with a dongle attached,
    and "No radio plugged in" would be a lie. A driver that loaded reports a version
    (getModuleVersion -> "0.3.3" for librtlsdrSupport.so; "" when it never loaded, "" for the
    corrupted compiled-in path), so the cell rules out "no driver" before it says "no radio",
    with advice a student can follow (restart the kernel, then ask)."""
    cell = next(c for c in _code_cells(notebook) if "SoapySDR.Device.enumerate()" in c.source)
    src = cell.source
    assert "SoapySDR.listModules()" in src and "SoapySDR.getModuleVersion(" in src
    assert src.index("getModuleVersion") < src.index("did not load") < src.index("No radio plugged in")

    # Both messages hang off ONE if/elif/else, each in its own branch.
    chain = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.If) and "did not load" in ast.unparse(n) and "No radio plugged in" in ast.unparse(n)
    )
    branches, node = [], chain
    while isinstance(node, ast.If):
        branches.append("\n".join(ast.unparse(s) for s in node.body))
        node = node.orelse[0] if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If) else node.orelse
    if node:                                                  # the final else:
        branches.append("\n".join(ast.unparse(s) for s in node))
    no_driver = [b for b in branches if "did not load" in b]
    no_radio = [b for b in branches if "No radio plugged in" in b]
    assert len(no_driver) == 1 and len(no_radio) == 1 and no_driver != no_radio
    assert "No radio plugged in" not in no_driver[0]
    assert "Restart Kernel" in no_driver[0] and "Ask your teacher" in no_driver[0]


def test_radio_cell_restores_soapysdr_log_level_even_when_enumerate_fails(notebook):
    """The cell silences SoapySDR while it looks for radios. If enumerate() raises, the kernel
    must not stay at FATAL for the rest of the lesson (a student who plugs in a dongle later
    would see none of the driver's warnings), so the restore lives in a finally: block."""
    cell = next(c for c in _code_cells(notebook) if "SoapySDR.Device.enumerate()" in c.source)
    tries = [n for n in ast.walk(ast.parse(cell.source)) if isinstance(n, ast.Try)]

    def mentions(nodes, snippet):
        # direct statements only, so an outer try: that merely contains the inner one does not count
        return any(snippet in ast.unparse(n) for n in nodes if not isinstance(n, ast.Try))

    guarded = [t for t in tries if mentions(t.body, "SoapySDR.Device.enumerate()")]
    assert len(guarded) == 1, "enumerate() must sit directly in exactly one try: block"
    inner = guarded[0]
    assert mentions(inner.finalbody, "SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_INFO)")
    assert not mentions(inner.body, "SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_INFO)")
    src = cell.source
    assert src.index("SOAPY_SDR_FATAL") < src.index("SoapySDR.Device.enumerate()") < src.index("SOAPY_SDR_INFO")


# --- README files -----------------------------------------------------------------

def test_submit_readme_text():
    text = open(os.path.join(SKEL, "submit", "README.md"), encoding="utf-8").read()
    assert text.startswith("# Hand-in folder")
    assert "Your teacher sees this folder straight away" in text
    assert '"send" button' in text
    assert "03-loops-priya.ipynb" in text
    assert "Files stay here until you or your teacher move them." in text


def test_notebooks_readme_text():
    text = open(os.path.join(SKEL, "notebooks", "README.md"), encoding="utf-8").read()
    assert text.strip()
    assert "Keep your own notebooks here." in text
    assert "New Notebook" in text and "File → New → Notebook" in text


def test_kit_contains_exactly_the_planned_files_and_no_dotfiles():
    """On the Mac, Finder junk (.DS_Store, ._*) in skel/ is tolerated because the image build
    deletes it (see test_dockerfile_bakes_the_kit_and_lab_defaults). Inside the image nothing is
    tolerated: a junk file there means the build baked it in and every new home would get it."""
    found = []
    for dirpath, dirnames, filenames in os.walk(SKEL):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if _is_finder_junk(name) and not IN_IMAGE:
                continue
            found.append(os.path.relpath(os.path.join(dirpath, name), SKEL))
    assert sorted(found) == KIT_FILES
    for rel in found:
        assert not os.path.basename(rel).startswith("."), rel
        assert not os.path.islink(os.path.join(SKEL, rel)), rel


@pytest.mark.skipif(not IN_IMAGE, reason="checks the image's /etc/skel (run via make test-api)")
def test_image_has_the_kit_in_both_places_and_the_lab_defaults():
    for base in ("/etc/skel", "/opt/classroom/skel"):
        for rel in KIT_FILES:
            path = os.path.join(base, rel)
            assert os.path.isfile(path), path
            assert stat.S_IMODE(os.stat(path).st_mode) & 0o444 == 0o444, path   # readable by the student
        for dirpath, _, filenames in os.walk(base):
            for name in filenames:
                assert not _is_finder_junk(name), os.path.join(dirpath, name)
    # useradd copies /etc/skel modes into the new home: match what console.accounts creates (0750)
    for sub in ("submit", "notebooks"):
        assert stat.S_IMODE(os.stat(os.path.join("/etc/skel", sub)).st_mode) == 0o750, sub
    assert os.path.isfile(LAB_OVERRIDES_INSTALLED)


@pytest.mark.skipif(not os.path.isfile(DOCKERFILE), reason="repo Dockerfile not present (inside the image)")
def test_dockerfile_bakes_the_kit_and_lab_defaults():
    """Block 7 copies skel/ twice (new homes via /etc/skel, backfill via /opt/classroom/skel) and
    installs overrides.json where JupyterLab reads it, with plain COPY (no labs-only flags). The
    same RUN strips Finder junk - .dockerignore patterns only match at the context root, so a
    .DS_Store inside skel/ would otherwise land in every student home - and opens the modes."""
    with open(DOCKERFILE, encoding="utf-8") as fh:
        lines = [line.strip() for line in fh]
    start = next(i for i, line in enumerate(lines) if line.startswith("# --- 7."))
    end = next(i for i, line in enumerate(lines) if line.startswith("# --- 8."))
    block = lines[start:end]
    copies = [line.split()[1:] for line in block if line.startswith("COPY ")]

    assert [c for c in copies if c[0] == "skel/"] == [["skel/", "/etc/skel/"], ["skel/", "/opt/classroom/skel/"]]
    assert ["lab/overrides.json", LAB_OVERRIDES_INSTALLED] in copies
    for c in copies:
        assert not any(token.startswith("--") for token in c), c

    run = " ".join(line.rstrip("\\").strip() for line in block[block.index(next(l for l in block if l.startswith("RUN "))):])
    assert re.search(r"&& find /etc/skel /opt/classroom/skel \\\( -name \.DS_Store -o -name '\._\*' \\\) -type f -delete ", run)
    assert "&& chmod -R u=rwX,go=rX /etc/skel /opt/classroom/skel " in run
    assert "&& chmod 750 /etc/skel/submit /etc/skel/notebooks " in run
    assert run.index("find /etc/skel") < run.index("chmod -R u=rwX,go=rX /etc/skel") < run.index("chmod 750 /etc/skel")
    # the COPY lines come before the RUN that fixes them up
    assert max(i for i, line in enumerate(block) if line.startswith("COPY ")) < next(i for i, line in enumerate(block) if line.startswith("RUN "))


# --- lab/overrides.json -----------------------------------------------------------

@pytest.fixture(scope="module")
def overrides():
    with open(OVERRIDES, encoding="utf-8") as fh:
        return json.load(fh)


def test_overrides_no_news_popup_and_no_update_checks(overrides):
    notification = overrides["@jupyterlab/apputils-extension:notification"]
    assert notification["fetchNews"] == "false"           # a STRING in JupyterLab's schema ("none" = the popup)
    assert notification["checkForUpdates"] is False
    assert overrides["@jupyterlab/extensionmanager-extension:plugin"] == {"enabled": False}


def test_overrides_protect_student_work(overrides):
    doc = overrides["@jupyterlab/docmanager-extension:plugin"]
    assert doc["autosave"] is True
    assert doc["autosaveInterval"] == 30
    assert doc["confirmClosingDocument"] is True


def test_overrides_readable_defaults(overrides):
    browser = overrides["@jupyterlab/filebrowser-extension:browser"]
    assert browser["showFileSizeColumn"] is True and browser["showLastModifiedColumn"] is True
    assert browser["sortNotebooksFirst"] is True and browser["showHiddenFiles"] is False
    assert browser["useFuzzyFilter"] is True
    assert overrides["@jupyterlab/notebook-extension:tracker"]["codeCellConfig"] == {"lineNumbers": True, "lineWrap": True}
    assert overrides["@jupyterlab/fileeditor-extension:plugin"]["editorConfig"] == {"lineNumbers": True, "lineWrap": True}
    themes = overrides["@jupyterlab/apputils-extension:themes"]
    assert themes["theme"] == "JupyterLab Light" and themes["adaptive-theme"] is False
    assert themes["theme-scrollbars"] is False
    font = overrides["@jupyterlab/terminal-extension:plugin"]["fontSize"]
    assert isinstance(font, int) and 9 <= font <= 72   # schema: integer in [9, 72]


def test_overrides_plugin_ids_are_well_formed(overrides):
    # Every top-level key is "@jupyterlab/<extension>:<plugin>" and every value an object.
    assert len(overrides) == 8
    for plugin_id, settings in overrides.items():
        pkg, _, plugin = plugin_id.partition(":")
        assert pkg.startswith("@jupyterlab/") and pkg.endswith("-extension") and plugin, plugin_id
        assert isinstance(settings, dict) and settings, plugin_id


# --- backfill into an existing home (console.accounts) ---------------------------

@pytest.fixture
def real_kit(tmp_path):
    """The repo's skel/ as the image ships it: the build deletes Finder's junk."""
    dst = tmp_path / "real-skel"
    shutil.copytree(SKEL, dst, ignore=shutil.ignore_patterns(".DS_Store", "._*", "__pycache__"))
    return str(dst)


def _marker(settings, username: str) -> str:
    """Where console.accounts records the kit version a home received: the root-only state
    dir, never the student's home (a student must not be able to plant or re-trigger it)."""
    return os.path.join(settings.state_dir, "kit", username)


def _marker_version(settings, username: str) -> str | None:
    try:
        with open(_marker(settings, username), encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return None


def test_backfill_copies_real_kit_into_existing_home(accounts, fake, settings, real_kit):
    """A student whose home pre-dates the kit gets Welcome.ipynb and the READMEs, exactly once."""
    settings.kit_dir = real_kit
    fake.add_user("priya", 2004)
    home = fake.make_home("priya", 2004, {"notebooks/old.ipynb": "{}"})
    added = accounts.ensure_home_extras("priya", 2004)

    assert added == 3
    for rel in KIT_FILES:
        dst = os.path.join(home, rel)
        assert os.path.isfile(dst), rel
        with open(os.path.join(SKEL, rel), "rb") as a, open(dst, "rb") as b:
            assert a.read() == b.read(), rel             # byte-identical copy
        assert stat.S_IMODE(os.stat(dst).st_mode) == 0o644
        assert fake.owners[dst] == 2004                  # owned by the student, not root
    assert fake.owners[os.path.join(home, "submit")] == 2004
    assert open(os.path.join(home, "notebooks", "old.ipynb")).read() == "{}"   # untouched
    assert _marker_version(settings, "priya") == str(settings.kit_version)
    assert not os.path.lexists(os.path.join(home, ".classroom"))               # nothing of ours in the home
    nbformat.validate(nbformat.read(os.path.join(home, "Welcome.ipynb"), as_version=4))

    # Second pass: marker present, nothing re-added, a deleted file stays deleted.
    os.unlink(os.path.join(home, "Welcome.ipynb"))
    assert accounts.ensure_home_extras("priya", 2004) == 0
    assert not os.path.exists(os.path.join(home, "Welcome.ipynb"))


def test_backfill_never_overwrites_a_students_own_file(accounts, fake, settings, real_kit):
    settings.kit_dir = real_kit
    fake.add_user("sam", 2005)
    home = fake.make_home("sam", 2005, {"submit/README.md": "my notes", "Welcome.ipynb": "mine"})
    assert accounts.ensure_home_extras("sam", 2005) == 1          # only notebooks/README.md
    assert open(os.path.join(home, "submit", "README.md")).read() == "my notes"
    assert open(os.path.join(home, "Welcome.ipynb")).read() == "mine"
    assert os.path.isfile(os.path.join(home, "notebooks", "README.md"))


def test_backfill_runs_again_after_a_kit_version_bump(accounts, fake, settings, real_kit):
    """Bumping AccountSettings.kit_version re-offers the kit at the next sync, still without
    overwriting anything the student already has."""
    settings.kit_dir = real_kit
    fake.add_user("kim", 2006)
    home = fake.make_home("kim", 2006)
    assert accounts.ensure_home_extras("kim", 2006) == 3
    os.unlink(os.path.join(home, "notebooks", "README.md"))
    with open(os.path.join(home, "Welcome.ipynb"), "w") as fh:
        fh.write("edited by kim")

    settings.kit_version = 2
    assert accounts.ensure_home_extras("kim", 2006) == 1          # only the missing README comes back
    assert open(os.path.join(home, "Welcome.ipynb")).read() == "edited by kim"
    assert _marker_version(settings, "kim") == "2"


def test_sync_reports_backfill_for_existing_accounts(accounts, fake, settings, real_kit):
    from console.roster import Student

    settings.kit_dir = real_kit
    fake.add_user("student01", 2001)
    fake.make_home("student01", 2001)
    report = accounts.sync([Student("student01", "pw1")], set())
    assert report.errors == []
    assert report.kit_homes == 1 and report.kit_files_added == 3
    report2 = accounts.sync([Student("student01", "pw1")], set())
    assert report2.kit_homes == 0 and report2.kit_files_added == 0


def test_remove_forgets_the_kit_marker_so_a_readded_student_gets_the_kit(accounts, fake, settings, real_kit):
    settings.kit_dir = real_kit
    fake.add_user("lee", 2007)
    fake.make_home("lee", 2007)
    accounts.ensure_home_extras("lee", 2007)
    assert _marker_version(settings, "lee") == "1"
    accounts.remove("lee", archive=False)
    assert _marker_version(settings, "lee") is None


# --- backfill against the REAL System: what a student can plant in their own home ------
#
# The backfill runs as root at every boot (`python -m console.accounts sync`, before the Hub
# starts), from POST /api/students/repair and when a student is re-added with a kept home. The
# student owns ~/notebooks and ~/submit and can put anything there, so anything planted must be
# harmless: never followed, never written through, never waited on.

@pytest.fixture
def real_accounts(settings, real_kit):
    """Accounts on the real System. Unprivileged runs chown to our own uid/gid; root can do anything."""
    settings.kit_dir = real_kit
    settings.students_gid = os.getgid()
    return Accounts(settings, System())


@pytest.fixture
def victim(tmp_path):
    """Stand-in for a root-owned file such as /etc/passwd."""
    p = tmp_path / "victim.txt"
    p.write_text("root:x:0:0:root:/root:/bin/bash\n")
    return p


def _plain_home(settings, username: str) -> str:
    home = os.path.join(settings.home_root, username)
    os.makedirs(home, exist_ok=True)
    return home


def _delivered(home: str) -> list[str]:
    return sorted(
        os.path.relpath(os.path.join(d, f), home)
        for d, dirs, files in os.walk(home)
        for f in files
    )


def _run_with_timeout(fn, seconds: float):
    """Run fn in a daemon thread; return (finished, result). A hung backfill must not hang pytest."""
    result = {}

    def work():
        result["value"] = fn()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(seconds)
    return (not worker.is_alive(), result.get("value"), worker)


def _release_fifo_reader(fifo: str, thread: threading.Thread) -> None:
    """A reader stuck in open(FIFO) returns as soon as a writer opens and closes (it reads EOF)."""
    if not thread.is_alive():
        return
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass
    thread.join(2.0)


def test_real_backfill_works_unprivileged(real_accounts, settings):
    """Sanity check for the fixture: the real System delivers the kit into a plain home, and the
    marker lands in the state dir with root-only modes."""
    home = _plain_home(settings, "alice")
    assert real_accounts.ensure_home_extras("alice", ME) == 3
    for rel in KIT_FILES:
        path = os.path.join(home, rel)
        assert os.path.isfile(path) and not os.path.islink(path), rel
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
        assert os.stat(path).st_uid == ME
    assert os.path.islink(os.path.join(home, "shared"))
    assert _delivered(home) == KIT_FILES                           # nothing else appeared in the home
    marker = _marker(settings, "alice")
    assert open(marker).read().strip() == str(settings.kit_version)
    assert stat.S_IMODE(os.stat(marker).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(marker)).st_mode) == 0o700
    assert real_accounts.ensure_home_extras("alice", ME) == 0


def test_backfill_leaves_a_symlink_planted_at_a_kit_path_alone(real_accounts, settings, victim):
    """~/Welcome.ipynb or ~/notebooks/README.md pointing at a root-owned file: never written through."""
    home = _plain_home(settings, "oscar")
    os.mkdir(os.path.join(home, "notebooks"))
    for rel in ("Welcome.ipynb", "notebooks/README.md"):
        os.symlink(str(victim), os.path.join(home, rel))
    before = victim.read_text()

    assert real_accounts.ensure_home_extras("oscar", ME) == 1      # only submit/README.md
    assert victim.read_text() == before
    for rel in ("Welcome.ipynb", "notebooks/README.md"):
        assert os.path.islink(os.path.join(home, rel)), rel


def test_backfill_never_touches_a_planted_dot_classroom(real_accounts, settings, victim):
    """Older designs kept the marker at ~/.classroom/kit-version. A student who plants that path
    (a symlink to /etc/passwd, a guessable .tmp-<pid> name) must find it untouched: the marker
    is root-only state outside the home."""
    home = _plain_home(settings, "eve")
    planted = os.path.join(home, ".classroom")
    os.mkdir(planted)
    for name in ("kit-version", f"kit-version.tmp-{os.getpid()}"):
        os.symlink(str(victim), os.path.join(planted, name))
    before = victim.read_text()

    assert real_accounts.ensure_home_extras("eve", ME) == 3

    assert victim.read_text() == before
    assert sorted(os.listdir(planted)) == sorted(["kit-version", f"kit-version.tmp-{os.getpid()}"])
    for name in os.listdir(planted):
        assert os.path.islink(os.path.join(planted, name)), name
    assert _marker_version(settings, "eve") == str(settings.kit_version)


@pytest.mark.parametrize("planted, expected_added", [
    (".classroom/kit-version", 3),     # the old marker location: irrelevant now, must not be read
    ("Welcome.ipynb", 2),               # a kit file slot: O_EXCL fails at once, no open() of the FIFO
    ("notebooks", 2),                   # a kit directory slot: O_DIRECTORY fails at once (ENOTDIR)
])
def test_backfill_is_not_blocked_by_a_planted_fifo(real_accounts, settings, planted, expected_added):
    """`mkfifo` anywhere the backfill looks must not park the boot sync (the Hub never starts) or
    the repair thread (holding the roster lock) forever: root's open() of a FIFO waits for a writer."""
    home = _plain_home(settings, "mallory")
    fifo = os.path.join(home, planted)
    os.makedirs(os.path.dirname(fifo), exist_ok=True)
    os.mkfifo(fifo)

    finished, added, worker = _run_with_timeout(lambda: real_accounts.ensure_home_extras("mallory", ME), BACKFILL_WAIT_S)
    try:
        assert finished, f"backfill hung on the FIFO at ~/{planted}"
        assert added == expected_added
        assert stat.S_ISFIFO(os.lstat(fifo).st_mode)                  # still the student's FIFO
        assert os.path.isfile(os.path.join(home, "submit", "README.md"))
    finally:
        _release_fifo_reader(fifo, worker)


def test_backfill_does_not_write_through_a_directory_symlink(real_accounts, settings, tmp_path):
    """~/notebooks replaced by a symlink to a root-writable directory (/etc/skel, /srv/shared, ...):
    root must not create a student-owned README.md there. The rest of the kit still arrives."""
    outside = tmp_path / "outside"
    outside.mkdir()
    home = _plain_home(settings, "trudy")
    os.symlink(str(outside), os.path.join(home, "notebooks"))

    assert real_accounts.ensure_home_extras("trudy", ME) == 2

    assert os.listdir(outside) == []
    assert os.path.islink(os.path.join(home, "notebooks"))
    assert os.path.isfile(os.path.join(home, "Welcome.ipynb"))
    assert os.path.isfile(os.path.join(home, "submit", "README.md"))


def test_backfill_skips_a_regular_file_where_a_kit_directory_belongs(real_accounts, settings):
    """A student who saved a file called `notebooks` keeps it; the kit skips that branch."""
    home = _plain_home(settings, "nina")
    with open(os.path.join(home, "notebooks"), "w") as fh:
        fh.write("not a folder")

    assert real_accounts.ensure_home_extras("nina", ME) == 2
    assert open(os.path.join(home, "notebooks")).read() == "not a folder"
    assert _delivered(home) == ["Welcome.ipynb", "notebooks", "submit/README.md"]


def test_backfill_skips_every_dotfile_in_the_kit(real_accounts, settings, real_kit):
    """The kit manifest is 'files under the kit dir that are not dotfiles': /etc/skel also holds
    .bashrc & co and Finder may leave .DS_Store / ._* behind. None of that is kit content."""
    for junk in (".DS_Store", ".bashrc", "._Welcome.ipynb", "notebooks/.DS_Store"):
        path = os.path.join(real_kit, junk)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("junk\n")
    home = _plain_home(settings, "dot")

    assert real_accounts.ensure_home_extras("dot", ME) == 3
    assert _delivered(home) == KIT_FILES


# --- the home-layout step BEFORE the kit: ~/shared symlink refresh --------------------
#
# Student-owned temporary paths must not prevent account creation or kit delivery.


def test_a_planted_directory_at_the_shared_links_tmp_name_does_not_block_the_kit(real_accounts, settings):
    """Real System: ~/shared.tmp-<our pid> is a directory the student made. The kit still arrives,
    ~/shared still points at the shared folder, and the student's directory is left alone."""
    home = _plain_home(settings, "eve")
    planted = os.path.join(home, f"shared.tmp-{os.getpid()}")
    os.mkdir(planted)

    assert real_accounts.ensure_home_extras("eve", ME) == 3

    assert _delivered(home) == KIT_FILES
    link = os.path.join(home, "shared")
    assert os.path.islink(link) and os.readlink(link) == settings.shared_dir
    assert os.path.isdir(planted) and not os.path.islink(planted)
    assert _marker_version(settings, "eve") == str(settings.kit_version)


def test_create_does_not_roll_the_account_back_over_a_planted_tmp_directory(accounts, fake, settings, real_kit):
    """FakeSystem (its symlink_replace uses the fixed name shared.tmp): re-creating an account whose
    home already holds that directory must not end in userdel; the kit lands and the marker is set."""
    settings.kit_dir = real_kit
    home = fake.make_home("bob", 2010)
    os.mkdir(os.path.join(home, "shared.tmp"))

    out = accounts.create("bob", "correct-horse-42")

    assert out.uid == 2010 and not out.home_created
    assert "bob" in fake.users
    assert fake.argv_of("userdel") == []
    assert _delivered(home) == KIT_FILES
    assert _marker_version(settings, "bob") == "1"
