# SPDX-License-Identifier: GPL-3.0-or-later
"""Run one non-saving Haori interval against an opened production Yohsai file."""

from __future__ import annotations

from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from source_package import load_source_package  # noqa: E402


haori = load_source_package(ROOT)
simulation = sys.modules[f"{haori.__name__}.simulation"]
collection, body = simulation.detect_yohsai_inputs(bpy.context.scene)
assert collection is not None and body is not None
start = int(bpy.context.scene.frame_current)
runner = simulation.SimulationRunner(
    bpy.context,
    collection,
    body,
    start,
    start + 1,
    1.0,
)
summary = runner.run_to_completion(bpy.context)
assert runner.output_collection is not None
assert bool(runner.output_collection["haori_cache_ready"])
assert len(runner.output_parts) == 4
assert int(runner.output_collection["haori_maximum_substeps"]) == 6
assert all(part.output.data.shape_keys is not None for part in runner.output_parts)
print("HAORI_SAVED_SIMULATION_OK", summary)
