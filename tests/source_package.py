# SPDX-License-Identifier: GPL-3.0-or-later
"""Load the repository root as an isolated Blender extension package."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def load_source_package(root: Path):
    name = "haori_source_test"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Haori package from {root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
