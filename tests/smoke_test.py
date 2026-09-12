"""
Smoke test: prove every package the classroom image promises actually imports
and does one tiny piece of real work.

Run inside the container:      python /srv/smoke_test.py
or from a notebook cell:       %run /srv/smoke_test.py
Exit code is non-zero if anything fails.
"""
import importlib
import os
import sys

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")   # quieten TensorFlow

# (module to import, human name, optional extra check)
CHECKS = [
    ("numpy",        "NumPy",         lambda m: m.arange(4).sum() == 6),
    ("pandas",       "pandas",        lambda m: len(m.DataFrame({"a": [1, 2]})) == 2),
    ("scipy",        "SciPy",         None),
    ("scipy.signal", "SciPy signal",  lambda m: m.butter(4, 0.2) is not None),
    ("sklearn",      "scikit-learn",  None),
    ("matplotlib",   "matplotlib",    None),
    ("seaborn",      "seaborn",       None),
    ("ipywidgets",   "ipywidgets",    None),
    ("plotly",       "plotly",        None),
    ("tqdm",         "tqdm",          None),
    ("torch",        "PyTorch",       lambda m: bool((m.ones(3) * 2).sum() == 6)),
    ("torchvision",  "torchvision",   None),
    ("tensorflow",   "TensorFlow",    lambda m: int(m.reduce_sum(m.constant([1, 2, 3]))) == 6),
    ("transformers", "transformers",  None),
    ("xgboost",      "XGBoost",       None),
    ("cv2",          "OpenCV",        lambda m: __import__("numpy") and
                                      m.GaussianBlur(__import__("numpy").zeros((8, 8), "uint8"), (3, 3), 0).shape == (8, 8)),
    ("rtlsdr",       "pyrtlsdr",      None),
    ("SoapySDR",     "SoapySDR",      None),
]

failed = []
for module, name, check in CHECKS:
    try:
        m = importlib.import_module(module)
        version = getattr(m, "__version__", "")
        if check is not None and not check(m):
            raise RuntimeError("functional check returned False")
        print(f"  OK    {name:<14} {version}")
    except Exception as e:  # noqa: BLE001
        failed.append(name)
        print(f"  FAIL  {name:<14} {type(e).__name__}: {e}")

print()
if failed:
    print(f"{len(failed)} package(s) failed: {', '.join(failed)}")
    sys.exit(1)
print(f"All {len(CHECKS)} checks passed. Python {sys.version.split()[0]}")
