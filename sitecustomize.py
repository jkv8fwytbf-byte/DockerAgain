"""
Loaded automatically by every Python interpreter in the image.

Purpose: on ARM (aarch64) builds, PyTorch's pip wheel bundles its own OpenBLAS
that contains symbols (sbgemm_) the conda OpenBLAS lacks. Whichever OpenBLAS is
loaded first wins, so "import numpy; import torch" fails while "import torch;
import numpy" works. Preloading PyTorch's copy here makes the import order
irrelevant. On x86_64 builds PyTorch does not ship that file, so this is a no-op.
"""
import ctypes
import glob
import os
import warnings

# pyrtlsdr 0.3 imports the deprecated pkg_resources module; hide that one
# warning so students do not see it every time they "import rtlsdr".
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

_torch_blas = glob.glob(
    os.path.join(os.path.dirname(__file__), "torch", "lib", "libopenblas*.so*")
)
for _lib in _torch_blas:
    try:
        ctypes.CDLL(_lib, mode=ctypes.RTLD_GLOBAL)
        break
    except OSError:
        pass
