"""Write the sample lesson notebooks in canonical nbformat 4.5 form."""
from __future__ import annotations

import uuid
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

HERE = Path(__file__).resolve().parent


def _id(name: str) -> str:
    return name if 1 <= len(name) <= 64 else uuid.uuid4().hex[:12]


def _md(text: str, cell_id: str):
    cell = new_markdown_cell(text)
    cell.id = _id(cell_id)
    cell.metadata = {}
    return cell


def _code(text: str, cell_id: str):
    cell = new_code_cell(text)
    cell.id = _id(cell_id)
    cell.metadata = {}
    cell.outputs = []
    cell.execution_count = None
    return cell


def _write(name: str, cells) -> Path:
    nb = new_notebook(cells=cells, metadata={
        "kernelspec": {"name": "python3", "display_name": "Python 3 (ipykernel)", "language": "python"},
        "language_info": {"name": "python"},
    })
    nb.nbformat = 4
    nb.nbformat_minor = 5
    path = HERE / name
    path.write_text(nbformat.writes(nb) + "\n", encoding="utf-8")
    return path


def python_and_data():
    return [
        _md("# 1. Python, tables and plots\n\nThis is a **handout**. It lives in `shared/` (read-only). Copy it into `notebooks/` if you want to edit it.\n\nRun each cell with **Shift+Enter**.", "p1-title"),
        _md("## Load the class scores\n\nThe CSV is in the same shared folder as this notebook.", "p1-load-h"),
        _code(
            """from pathlib import Path

import pandas as pd

candidates = [
    Path.home() / "shared" / "class-scores.csv",
    Path.cwd() / "class-scores.csv",
]
csv = next(p for p in candidates if p.is_file())
df = pd.read_csv(csv)
df.head()""",
            "p1-load",
        ),
        _md("## Averages by group", "p1-avg-h"),
        _code(
            """summary = df.groupby("group")[["pretest", "project", "quiz"]].mean().round(1)
summary""",
            "p1-avg",
        ),
        _md("## Draw the project scores\n\nIf a picture appears under the cell, matplotlib and seaborn are working.", "p1-plot-h"),
        _code(
            """import matplotlib.pyplot as plt
import seaborn as sns

sns.set_theme(style="whitegrid")
fig, ax = plt.subplots(figsize=(6, 3.2))
sns.barplot(data=df, x="name", y="project", hue="group", ax=ax)
ax.set_title("Project scores")
ax.set_xlabel("")
ax.set_ylabel("score")
plt.xticks(rotation=35, ha="right")
plt.tight_layout()
plt.show()""",
            "p1-plot",
        ),
        _md("## Hand this in\n\nWhen you are done, copy the notebook into `submit/` and name it `week-1-yourname.ipynb`.", "p1-handin"),
    ]


def machine_learning():
    return [
        _md("# 2. A tiny machine-learning model\n\nUses **scikit-learn** (already in the image). The last cell is optional and loads XGBoost — skip it if the class is short on RAM.", "ml-title"),
        _md("## Make a small dataset and split it", "ml-data-h"),
        _code(
            """from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split

X, y = make_classification(n_samples=400, n_features=6, n_informative=4, random_state=0)
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=0)
print(f"{len(X_train)} training rows, {len(X_test)} test rows")""",
            "ml-data",
        ),
        _md("## Train a logistic regression and check accuracy", "ml-fit-h"),
        _code(
            """from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix

model = LogisticRegression(max_iter=500)
model.fit(X_train, y_train)
pred = model.predict(X_test)
print("accuracy", round(accuracy_score(y_test, pred), 3))
print("confusion matrix\\n", confusion_matrix(y_test, pred))""",
            "ml-fit",
        ),
        _md("## Optional: the same job with XGBoost\n\nOnly run this if your teacher asks — it uses more memory.", "ml-xgb-h"),
        _code(
            """from xgboost import XGBClassifier

boost = XGBClassifier(n_estimators=40, max_depth=3, n_jobs=1, eval_metric="logloss")
boost.fit(X_train, y_train)
print("xgboost accuracy", round(accuracy_score(y_test, boost.predict(X_test)), 3))""",
            "ml-xgb",
        ),
    ]


def computer_vision():
    return [
        _md("# 3. Computer vision with OpenCV\n\nNo camera needed. We draw a shape, blur it, and find its edges.", "cv-title"),
        _md("## Draw a circle, then blur and detect edges", "cv-h"),
        _code(
            """import cv2
import matplotlib.pyplot as plt
import numpy as np

img = np.zeros((180, 320, 3), dtype=np.uint8)
cv2.circle(img, (160, 90), 55, (40, 160, 255), -1)
cv2.putText(img, "SDR", (128, 98), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)

grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
blur = cv2.GaussianBlur(grey, (9, 9), 0)
edges = cv2.Canny(blur, 80, 160)

fig, axes = plt.subplots(1, 3, figsize=(9, 2.6))
axes[0].imshow(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)); axes[0].set_title("drawn")
axes[1].imshow(blur, cmap="gray"); axes[1].set_title("blurred")
axes[2].imshow(edges, cmap="gray"); axes[2].set_title("edges")
for ax in axes:
    ax.axis("off")
plt.tight_layout()
plt.show()
print("OpenCV", cv2.__version__)""",
            "cv-code",
        ),
        _md("If three pictures appear, OpenCV is working. There is no desktop GUI inside the container (`opencv-python-headless`), which is why we draw with matplotlib.", "cv-note"),
    ]


def radio_signals():
    return [
        _md("# 4. Radio signals (without a dongle)\n\nYou do not need a USB radio to learn the idea. This notebook makes a pretend 1 kHz tone, plots its spectrum, then checks whether SoapySDR can see a real dongle.\n\n*Using SoapySDR in a notebook of your own? Copy the `SOAPY_SDR_PLUGIN_PATH` line to the top of it.*", "radio-title"),
        _code(
            """import os

import matplotlib.pyplot as plt
import numpy as np
from scipy import signal

fs = 48_000
t = np.arange(fs) / fs
rng = np.random.default_rng(0)
tone = np.sin(2 * np.pi * 1000 * t) + 0.3 * rng.standard_normal(t.size)

freqs, power = signal.welch(tone, fs=fs, nperseg=2048)
fig, ax = plt.subplots(figsize=(6, 2.4))
ax.semilogy(freqs, power)
ax.set_xlim(0, 5000)
ax.set_title("Spectrum of a 1 kHz tone with noise")
ax.set_xlabel("frequency (Hz)")
ax.set_ylabel("power")
plt.tight_layout()
plt.show()

os.environ.setdefault("SOAPY_SDR_PLUGIN_PATH", "/opt/conda/lib/SoapySDR/modules0.8")
try:
    import SoapySDR

    SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    try:
        radios = SoapySDR.Device.enumerate()
    finally:
        SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_INFO)
    drivers = [m for m in SoapySDR.listModules() if SoapySDR.getModuleVersion(m)]
    if radios:
        for radio in radios:
            print("SoapySDR found a radio:", radio)
    elif not drivers:
        print("SoapySDR: its radio drivers did not load.")
        print("  Try Kernel → Restart Kernel…, then run this cell again.")
    else:
        print("SoapySDR: No radio plugged in — that's fine for now.")
except Exception as problem:
    print("SoapySDR could not look for radios:", problem)

try:
    from rtlsdr import RtlSdr

    print("pyrtlsdr is ready. With an RTL-SDR dongle plugged in, RtlSdr() would open it.")
except Exception as problem:
    print("pyrtlsdr is not available:", problem)""",
            "radio-code",
        ),
    ]


NOTEBOOKS = {
    "01-python-and-data.ipynb": python_and_data,
    "02-machine-learning.ipynb": machine_learning,
    "03-computer-vision.ipynb": computer_vision,
    "04-radio-signals.ipynb": radio_signals,
}


def main() -> None:
    for name, factory in NOTEBOOKS.items():
        path = _write(name, factory())
        print("wrote", path.name)


if __name__ == "__main__":
    main()
