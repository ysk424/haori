"""Emit a first-click state hash for cross-thread determinism checks."""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
from pathlib import Path

import bpy
import numpy as np


repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "tests"))
for wheel in sorted((repo / "wheels").glob("*.whl")):
    sys.path.insert(0, str(wheel))

from source_package import load_source_package  # noqa: E402


def state_hash(*arrays: np.ndarray) -> str:
    digest = hashlib.sha256()
    for value in arrays:
        array = np.ascontiguousarray(value)
        digest.update(str(array.dtype).encode("ascii"))
        digest.update(np.asarray(array.shape, dtype=np.int64).tobytes())
        digest.update(array.tobytes())
    return digest.hexdigest()


source = Path.home() / "Desktop" / "test2.pdf"
if not source.is_file():
    raise RuntimeError(f"Missing determinism input: {source}")

haori = load_source_package(repo)
kitsuke = sys.modules[f"{haori.__name__}.kitsuke"]
haori_svg_parser = importlib.import_module(f"{haori.__name__}.haori_svg_parser")
create_clothes_mesh = sys.modules[f"{haori.__name__}.mesh_loader"].create_clothes_mesh

haori.register()
try:
    document = haori_svg_parser.parse_pdf(source)
    collection = create_clothes_mesh(bpy.context, document)
    bpy.context.scene.haori.clothes_collection = collection

    if os.environ.get("HAORI_CONTACT_BODY") == "1":
        bpy.ops.mesh.primitive_cube_add(location=(0.0, -1.0, 0.1), scale=(0.1, 0.1, 0.1))
    else:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(100.0, 100.0, 100.0))
    bpy.context.scene.haori.body_object = bpy.context.object

    for obj in collection.objects:
        if obj.get("haori_role") == "part":
            obj.location.x += 0.001
    assert bpy.ops.haori.kitsuke() == {"FINISHED"}

    session = kitsuke._sessions[collection.as_pointer()]
    positions, velocities = session.runtime.state()
    seams = session.runtime.seam_state()
    stats = session.runtime.last_stats
    print(
        "HAORI_THREAD_HASH",
        f"threads={os.environ.get('OMP_NUM_THREADS', 'default')}",
        f"state={state_hash(positions, velocities, seams)}",
        f"body_candidates={stats['body_candidate_count']}",
        f"maximum_displacement={stats['maximum_displacement']:.9g}",
    )
finally:
    haori.unregister()
