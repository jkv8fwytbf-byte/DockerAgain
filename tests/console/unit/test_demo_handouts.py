"""Sample lesson notebooks shipped with the Docker-free complete demo."""
from __future__ import annotations

import ast
from pathlib import Path

import nbformat
import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDOUTS = ROOT / "demo" / "handouts"

NOTEBOOKS = [
    "01-python-and-data.ipynb",
    "02-machine-learning.ipynb",
    "03-computer-vision.ipynb",
    "04-radio-signals.ipynb",
]


@pytest.fixture(scope="module")
def notebooks():
    out = {}
    for name in NOTEBOOKS:
        path = HANDOUTS / name
        with open(path, encoding="utf-8") as fh:
            nb = nbformat.read(fh, as_version=4)
        out[name] = nb
    return out


def test_handout_notebooks_are_canonical_nbformat_4_5(notebooks):
    for name, nb in notebooks.items():
        nbformat.validate(nb)
        assert (nb.nbformat, nb.nbformat_minor) == (4, 5)
        path = HANDOUTS / name
        raw = path.read_text(encoding="utf-8")
        assert raw == nbformat.writes(nb) + "\n", name
        ids = [c.id for c in nb.cells]
        assert len(ids) == len(set(ids)), name
        for cell in nb.cells:
            if cell.cell_type == "code":
                assert cell.outputs == [] and cell.execution_count is None, name


def test_handout_code_is_valid_python_and_stays_in_the_image(notebooks):
    allowed = (
        "numpy", "pandas", "matplotlib", "seaborn", "sklearn", "xgboost", "cv2",
        "scipy", "SoapySDR", "rtlsdr", "pathlib", "os",
    )
    for name, nb in notebooks.items():
        text = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
        assert "pip install" not in text and "conda install" not in text, name
        for cell in nb.cells:
            if cell.cell_type != "code":
                continue
            plain = "\n".join(
                line for line in cell.source.splitlines() if not line.lstrip().startswith(("%", "!"))
            )
            compile(plain, cell.id, "exec")
            ast.parse(plain)
        assert any(lib in text for lib in allowed), name


def test_each_handout_covers_its_topic(notebooks):
    assert "read_csv" in "".join(c.source for c in notebooks["01-python-and-data.ipynb"].cells)
    assert "LogisticRegression" in "".join(c.source for c in notebooks["02-machine-learning.ipynb"].cells)
    assert "cv2.Canny" in "".join(c.source for c in notebooks["03-computer-vision.ipynb"].cells)
    assert "SOAPY_SDR_PLUGIN_PATH" in "".join(c.source for c in notebooks["04-radio-signals.ipynb"].cells)
    radio = "".join(c.source for c in notebooks["04-radio-signals.ipynb"].cells)
    assert radio.index("SOAPY_SDR_PLUGIN_PATH") < radio.index("import SoapySDR")


def test_homework_and_scores_are_present():
    hw = (HANDOUTS / "week-1-homework.md").read_text(encoding="utf-8")
    assert hw.startswith("# Week 1 homework")
    assert "submit/" in hw
    csv = (HANDOUTS / "class-scores.csv").read_text(encoding="utf-8")
    assert "Priya Sharma" in csv and "pretest" in csv
