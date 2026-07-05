"""Shared fixtures. The brownfield benchmark is generated once per test
session (it is deterministic and takes ~2s) and reused by every module's
acceptance tests."""

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def brownfield_root(tmp_path_factory) -> Path:
    from benchmark.brownfield.generate import generate

    root = tmp_path_factory.mktemp("brownfield")
    generate(root)
    return root
