"""
Shared fixtures: a FakeSystem that emulates useradd/userdel/usermod/chpasswd/
tar against an in-memory passwd table while using a real temporary directory
for home folders (ownership is tracked in a dict because tests are not root).
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "console"))

from console.accounts import AccountSettings, Accounts  # noqa: E402


from fakes import FakeSystem  # noqa: E402


@pytest.fixture
def home_root(tmp_path):
    p = tmp_path / "home"
    p.mkdir()
    return str(p)


@pytest.fixture
def skel_dir(tmp_path):
    p = tmp_path / "skel"
    (p / "submit").mkdir(parents=True)
    (p / "Welcome.ipynb").write_text('{"cells": []}')
    (p / "submit" / "README.md").write_text("# Hand-in folder\n")
    return str(p)


@pytest.fixture
def fake(home_root, skel_dir):
    return FakeSystem(home_root, skel_dir)


@pytest.fixture
def settings(tmp_path, home_root, skel_dir):
    shared = tmp_path / "shared"
    shared.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    return AccountSettings(
        home_root=home_root,
        shared_dir=str(shared),
        state_dir=str(state),
        kit_dir=skel_dir,
        kit_version=1,
    )


@pytest.fixture
def accounts(settings, fake):
    return Accounts(settings, fake)
