"""Blender-background regression check for Kitsuke Undo/Redo state."""

from __future__ import annotations

import os
import importlib
import sys
from pathlib import Path

import bpy
import numpy as np


installed_check = os.environ.get("HAORI_INSTALLED_CHECK") == "1"
if installed_check:
    from bl_ext.user_default import haori
    from bl_ext.user_default.haori import kitsuke, haori_svg_parser
    from bl_ext.user_default.haori.mesh_loader import create_clothes_mesh
else:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "tests"))
    for wheel in sorted((repo / "wheels").glob("*.whl")):
        sys.path.insert(0, str(wheel))
    from source_package import load_source_package

    haori = load_source_package(repo)
    kitsuke = sys.modules[f"{haori.__name__}.kitsuke"]
    haori_svg_parser = importlib.import_module(f"{haori.__name__}.haori_svg_parser")
    create_clothes_mesh = sys.modules[f"{haori.__name__}.mesh_loader"].create_clothes_mesh


source = Path.home() / "Desktop" / "test2.pdf"
if not source.is_file():
    raise RuntimeError(f"Missing Undo integration input: {source}")


def persisted_state(collection, seam_count):
    ranges = []
    offset = 0
    for obj in kitsuke._parts(collection):
        ranges.append(kitsuke._PartRange(obj, offset, len(obj.data.vertices)))
        offset += len(obj.data.vertices)
    return kitsuke._read_persisted_state(collection, ranges, seam_count)


if not installed_check:
    haori.register()
try:
    bpy.context.preferences.edit.use_global_undo = True
    document = haori_svg_parser.parse_pdf(source)
    collection = create_clothes_mesh(bpy.context, document)
    collection_name = collection.name
    bpy.context.scene.haori.clothes_collection = collection
    bpy.context.scene.haori.auto_lock = False

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(100.0, 100.0, 100.0))
    body = bpy.context.object
    bpy.context.scene.haori.body_object = body

    for obj in collection.objects:
        if obj.get("haori_role") == "part":
            obj.location.x += 0.001
    bpy.ops.ed.undo_push(message="Haori Undo test initial state")

    assert bpy.ops.haori.kitsuke() == {"FINISHED"}
    session = kitsuke._sessions[collection.as_pointer()]
    seam_one = session.runtime.seam_state().copy()
    velocity_one = session.velocities.copy()
    revision_one = session.revision
    bpy.ops.ed.undo_push(message="Haori Undo test click 1")

    click_two_start_positions, click_two_start_velocities = session.runtime.state()
    click_two_start_seams = session.runtime.seam_state()

    assert bpy.ops.haori.kitsuke() == {"FINISHED"}
    session = kitsuke._sessions[collection.as_pointer()]
    seam_two = session.runtime.seam_state().copy()
    velocity_two = session.velocities.copy()
    revision_two = session.revision
    bpy.ops.ed.undo_push(message="Haori Undo test click 2")

    assert bpy.ops.ed.undo() == {"FINISHED"}
    assert not kitsuke._sessions
    collection = bpy.data.collections[collection_name]
    restored = persisted_state(collection, len(seam_one))
    assert restored is not None
    revision, seam_rest, velocities = restored
    assert revision == revision_one
    assert np.array_equal(seam_rest, seam_one)
    assert np.array_equal(velocities, velocity_one)

    assert bpy.ops.ed.redo() == {"FINISHED"}
    assert not kitsuke._sessions
    collection = bpy.data.collections[collection_name]
    restored = persisted_state(collection, len(seam_two))
    assert restored is not None
    revision, seam_rest, velocities = restored
    assert revision == revision_two
    assert np.array_equal(seam_rest, seam_two)
    assert np.array_equal(velocities, velocity_two)

    # Repeating click 2 after Undo must continue from click 1, not from the
    # stale in-memory click-2 target that originally exposed the bug.
    assert bpy.ops.ed.undo() == {"FINISHED"}
    assert not kitsuke._sessions
    collection = bpy.data.collections[collection_name]
    reconstructed = kitsuke._KitsukeSession(
        bpy.context,
        collection,
        bpy.context.scene.haori.body_object,
        kitsuke._sewn_preview(collection),
        kitsuke.KITSUKE_BACKEND_STABLE_COSSERAT,
    )
    reconstructed_positions, reconstructed_velocities = reconstructed.runtime.state()
    reconstructed_seams = reconstructed.runtime.seam_state()
    assert np.array_equal(reconstructed_positions, click_two_start_positions)
    assert np.array_equal(reconstructed_velocities, click_two_start_velocities)
    assert np.array_equal(reconstructed_seams, click_two_start_seams)
    print(
        "HAORI_UNDO_START_DIFF",
        f"positions={np.max(np.abs(reconstructed_positions - click_two_start_positions)):.9g}",
        f"velocities={np.max(np.abs(reconstructed_velocities - click_two_start_velocities)):.9g}",
        f"seams={np.max(np.abs(reconstructed_seams - click_two_start_seams)):.9g}",
    )
    kitsuke._sessions[collection.as_pointer()] = reconstructed
    assert bpy.ops.haori.kitsuke() == {"FINISHED"}
    collection = bpy.data.collections[collection_name]
    session = kitsuke._sessions[collection.as_pointer()]
    assert session.revision == revision_two
    repeated_seams = session.runtime.seam_state()
    maximum_seam_delta = float(np.max(np.abs(repeated_seams - seam_two)))
    assert np.allclose(repeated_seams, seam_two, rtol=0.0, atol=1.0e-5), maximum_seam_delta
    print(
        f"HAORI_UNDO_REDO_OK revisions={revision_one},{revision_two} "
        f"seams={len(seam_two)} repeat_delta={maximum_seam_delta:.3g}"
    )
finally:
    if not installed_check:
        haori.unregister()
