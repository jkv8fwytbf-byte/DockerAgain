# syntax=docker/dockerfile:1
# =============================================================================
# Classroom JupyterHub image
#
# Recipe: start from the Jupyter project's "scipy-notebook" image (JupyterLab,
# JupyterHub, NumPy, pandas, SciPy, scikit-learn, matplotlib already inside),
# then add the pieces the meeting notes ask for. See README.md.
# =============================================================================
ARG BASE_IMAGE=quay.io/jupyter/scipy-notebook:python-3.12
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="classroom-jupyterhub" \
      org.opencontainers.image.description="Multi-user JupyterHub with Python AI/ML, OpenCV and SDR tooling for LAN classrooms"

# The Hub must run as root so it can create student accounts and start each
# student's server under their own Linux user.
USER root

# --- 1. System packages (apt) ------------------------------------------------
#   rtl-sdr   : CLI tools for RTL-SDR USB dongles; also pulls in librtlsdr,
#               the C library the Python package pyrtlsdr needs
#   usbutils  : lsusb, to check a dongle is visible inside the container
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      rtl-sdr \
      usbutils \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# --- 2. Conda packages (mamba) -----------------------------------------------
#   configurable-http-proxy : the proxy JupyterHub sits behind (not in base image)
#   soapysdr (+ rtlsdr module) : vendor-neutral SDR API with Python bindings
#                                             [RADIO PLACEHOLDER - extend here]
RUN mamba install --yes \
      configurable-http-proxy \
      soapysdr \
      soapysdr-module-rtlsdr \
 && mamba clean --all -f -y \
 && fix-permissions "${CONDA_DIR}"

# --- 3. PyTorch, CPU-only wheels ---------------------------------------------
# Installed on its own line with PyTorch's CPU index so pip can never pick the
# multi-gigabyte CUDA build from PyPI by mistake.
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision \
 && fix-permissions "${CONDA_DIR}"

# --- 4. Everything else (pip) ------------------------------------------------
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && fix-permissions "${CONDA_DIR}"

# --- 5. Small fixes ----------------------------------------------------------
#   sitecustomize.py : makes "import numpy; import torch" work on ARM builds (see file)
#   profile.d        : puts conda's python on PATH for login shells ("su - student01")
COPY sitecustomize.py /tmp/sitecustomize.py
RUN cp /tmp/sitecustomize.py "$(python -c 'import site; print(site.getsitepackages()[0])')/sitecustomize.py" \
 && rm /tmp/sitecustomize.py \
 && echo 'export PATH="/opt/conda/bin:$PATH"' > /etc/profile.d/conda-path.sh

# --- 6. JupyterHub configuration and start-up script -------------------------
COPY jupyterhub_config.py /etc/jupyterhub/jupyterhub_config.py
COPY pam-jupyterhub       /etc/pam.d/jupyterhub
COPY entrypoint.sh        /usr/local/bin/entrypoint.sh
COPY tests/smoke_test.py  /srv/smoke_test.py
RUN chmod 755 /usr/local/bin/entrypoint.sh \
 && mkdir -p /srv/jupyterhub /srv/shared

# JupyterHub listens here. Student notebooks, the shared folder and the Hub's
# own state live on volumes (see compose.yaml) so they survive restarts.
EXPOSE 8000

# The base image's health check probes the single-user notebook port (8888).
# Replace it with one that asks the Hub itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/hub/health || exit 1

# The base image's ENTRYPOINT runs its own start.sh, which drops to the
# unprivileged "jovyan" user. We need root (to create accounts), so we keep
# only "tini" (a tiny init that reaps processes) and run our own script.
ENTRYPOINT ["tini", "-g", "--"]
CMD ["/usr/local/bin/entrypoint.sh"]
