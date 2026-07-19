# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate that an opened saved Yohsai file is usable after a Blender restart."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from source_package import load_source_package  # noqa: E402


haori = load_source_package(ROOT)
simulation = sys.modules[f"{haori.__name__}.simulation"]

detected_collection, body = simulation.detect_yohsai_inputs(bpy.context.scene)
requested_collection = os.environ.get("HAORI_TEST_CLOTHES", "").strip()
collection = (
    bpy.data.collections.get(requested_collection)
    if requested_collection
    else detected_collection
)
assert collection is not None
if requested_collection:
    assert collection.name == requested_collection
assert body is not None
state = simulation.read_source_state(collection)
snapshot = simulation.body_snapshot(bpy.context, body)
assert len(state.parts) >= 2
assert len(state.positions) == sum(part.count for part in state.parts)
assert len(state.seams) == len(state.seam_state) > 0
assert len(snapshot.vertices) > 0 and len(snapshot.faces) > 0
print(
    "HAORI_SAVED_STATE_OK",
    collection.name,
    len(state.parts),
    len(state.positions),
    len(state.seams),
    body.name,
    len(snapshot.vertices),
    len(snapshot.faces),
)
