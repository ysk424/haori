"""Blender-background integration check for mirrored RING sleeve construction."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import bpy
import numpy as np


installed_check = os.environ.get("HAORI_INSTALLED_CHECK") == "1"
if installed_check:
    from bl_ext.user_default.haori import kitsuke, mesh_loader, ui  # noqa: E402
    for wheel in sorted((Path(mesh_loader.__file__).parent / "wheels").glob("*.whl")):
        sys.path.insert(0, str(wheel))
else:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "tests"))
    for wheel in sorted((repo / "wheels").glob("*.whl")):
        sys.path.insert(0, str(wheel))
    from source_package import load_source_package  # noqa: E402

    package = load_source_package(repo)
    kitsuke = sys.modules[f"{package.__name__}.kitsuke"]
    mesh_loader = sys.modules[f"{package.__name__}.mesh_loader"]
    ui = sys.modules[f"{package.__name__}.ui"]


source = Path.home() / "Desktop" / "test3.pdf"
if not source.is_file():
    raise RuntimeError("Missing integration input: Desktop/test3.pdf")

if not installed_check:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    package.register()
bpy.context.scene.haori.svg_path = str(source)
assert bpy.ops.haori.load_svg() == {"FINISHED"}
ui._parse_process.wait(timeout=30)
ui._poll_svg_parser()
assert bpy.context.scene.haori.parse_status.startswith("Loaded CLOTHES_001: 4 part(s)")
assert bpy.context.scene.haori.auto_lock
document = ui._loaded_pattern_json
collection = bpy.context.scene.haori.clothes_collection
parts = sorted(
    (obj for obj in collection.objects if obj.get("haori_role") == "part"),
    key=lambda obj: int(obj["haori_panel_index"]),
)
assert len(parts) == 4
assert all(mesh_loader.part_gravity_state(obj) == mesh_loader.GRAVITY_STATE_PLACED for obj in parts)
assert all(bool(obj[mesh_loader.LOCKED_OBJECT_KEY]) for obj in parts)
assert [obj["haori_panel_label"] for obj in parts] == ["OMOTE", "URA", "SODE", "SODE"]
assert [obj["haori_mirror_side"] for obj in parts] == ["", "", "LEFT", "RIGHT"]

sleeves = [obj for obj in parts if bool(obj["haori_ring_closed"])]
body = [obj for obj in parts if not bool(obj["haori_ring_closed"])]
assert len(sleeves) == 2 and len(body) == 2
for sleeve in sleeves:
    mesh = sleeve.data
    assert "sewing_C" in mesh.attributes
    assert "sewing_RING" not in mesh.attributes
    assert "haori_pattern_edge_rest" in mesh.attributes
    assert mesh_loader.GRAINLINE_EDGE_FAMILY_ATTRIBUTE in mesh.attributes
    assert mesh_loader.GRAINLINE_FACE_QUAD_ATTRIBUTE in mesh.attributes
    family = mesh.attributes[mesh_loader.GRAINLINE_EDGE_FAMILY_ATTRIBUTE]
    quad = mesh.attributes[mesh_loader.GRAINLINE_FACE_QUAD_ATTRIBUTE]
    quad_ids = {int(item.value) for item in quad.data if int(item.value) >= 0}
    proxy_count = sum(
        int(item.value) == mesh_loader.GRAINLINE_EDGE_PROXY for item in family.data
    )
    assert quad_ids and proxy_count == len(quad_ids)
    points = [sleeve.matrix_world @ vertex.co for vertex in mesh.vertices]
    assert max(point.y for point in points) - min(point.y for point in points) > 0.05
    assert max(point.z for point in points) - min(point.z for point in points) > 0.05
    assert len(mesh.vertices) - len(mesh.edges) + len(mesh.polygons) == 0

    adjacency: dict[int, set[int]] = {}
    marker = mesh.attributes["sewing_C"]
    for edge in mesh.edges:
        if not marker.data[edge.index].value:
            continue
        a, b = edge.vertices
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    assert adjacency and all(len(neighbors) == 2 for neighbors in adjacency.values())


def seam_length(obj, label):
    mesh = obj.data
    marker = mesh.attributes[f"sewing_{label}"]
    rest = mesh.attributes["haori_pattern_edge_rest"]
    return sum(rest.data[edge.index].value for edge in mesh.edges if marker.data[edge.index].value)


sleeve_c = [seam_length(obj, "C") for obj in sleeves]
body_c_per_side = sum(seam_length(obj, "C") for obj in body) / 2.0
assert all(length > body_c_per_side for length in sleeve_c), (sleeve_c, body_c_per_side)

object_pointers = [obj.as_pointer() for obj in parts]
sewing_changed, vertex_count = mesh_loader.update_clothes_mesh(bpy.context, collection, document)
assert not sewing_changed
assert vertex_count == sum(len(obj.data.vertices) for obj in parts)
assert [obj.as_pointer() for obj in parts] == object_pointers
assert [obj["haori_panel_instance"] for obj in parts] == ["OMOTE", "URA", "SODE:LEFT", "SODE:RIGHT"]

for obj in body:
    obj.location.x += 0.001
bpy.context.view_layer.update()
mesh_loader.mark_moved_parts_pending(collection)
body_plan = mesh_loader.build_sewing_plan(collection)
assert body_plan.parts == tuple(body)
assert body_plan.labels == ("A", "B")

sleeves[0].location.x += 0.001
bpy.context.view_layer.update()
mesh_loader.mark_moved_parts_pending(collection)
left_sleeve_plan = mesh_loader.build_sewing_plan(collection)
assert left_sleeve_plan.parts == tuple(parts[:3])
assert left_sleeve_plan.labels == ("A", "B", "C")
left_offsets = {}
offset = 0
for obj in left_sleeve_plan.parts:
    left_offsets[obj] = range(offset, offset + len(obj.data.vertices))
    offset += len(obj.data.vertices)
left_sleeve_range = left_offsets[sleeves[0]]
left_c_connections = [
    (a, b) for label, a, b in left_sleeve_plan.connections if label == "C"
]
assert left_c_connections
assert all(a in left_sleeve_range or b in left_sleeve_range for a, b in left_c_connections)

sleeves[1].location.x += 0.001
bpy.context.view_layer.update()
mesh_loader.mark_moved_parts_pending(collection)
plan = mesh_loader.build_sewing_plan(collection)
assert plan.labels == ("A", "B", "C")
c_connections = [(a, b) for label, a, b in plan.connections if label == "C"]
assert c_connections
offsets = {}
offset = 0
for obj in plan.parts:
    offsets[obj] = range(offset, offset + len(obj.data.vertices))
    offset += len(obj.data.vertices)
sleeve_ranges = [offsets[obj] for obj in sleeves]
assert all(
    any(a in sleeve_range or b in sleeve_range for sleeve_range in sleeve_ranges)
    for a, b in c_connections
)

# RING construction coordinates must remain finite under the same Body-independent
# lattice model.  Constant seam attraction is active even with gravity disabled.
preview = mesh_loader.create_sewn_mesh(bpy.context, collection)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(100.0, 100.0, 100.0))
fixed_body = bpy.context.object
session = kitsuke._KitsukeSession(
    bpy.context,
    collection,
    fixed_body,
    preview,
    kitsuke.KITSUKE_BACKEND_STABLE_COSSERAT,
)
ring_start = session.positions.copy()
try:
    session.advance(bpy.context, 0.0, 4)
    assert np.all(np.isfinite(session.positions))
    ring_drift = float(np.linalg.norm(session.positions - ring_start, axis=1).max())
    assert ring_drift < 0.25, ring_drift
finally:
    session.runtime.close()

print(
    "HAORI_SLEEVE_OK "
    f"parts={len(parts)} sleeve_C={sleeve_c[0]:.6f} body_C={body_c_per_side:.6f} "
    f"partial_connections={len(left_c_connections)} connections={len(c_connections)} "
    f"cosserat_drift={ring_drift:.6g}"
)
