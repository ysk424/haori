# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless integration check for restart-safe animated Body simulation."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import bpy
import numpy as np
from _bpy_restrict_state import RestrictBlend


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from source_package import load_source_package  # noqa: E402


haori = load_source_package(ROOT)
simulation = sys.modules[f"{haori.__name__}.simulation"]
native = sys.modules[f"{haori.__name__}.cosserat_native"]
ui = sys.modules[f"{haori.__name__}.ui"]


def check_contact_clearance_bridge():
    topology = simulation.ClothTopology(
        np.empty((0, 2), dtype=np.int32),
        np.empty(0, dtype=np.float32),
        np.empty((0, 4), dtype=np.int32),
        np.empty((0, 3), dtype=np.float32),
        np.empty((0, 3), dtype=np.int32),
        np.empty((0, 2), dtype=np.float32),
    )
    body = SimpleNamespace(
        vertices=np.asarray(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            dtype=np.float32,
        ),
        faces=np.asarray([(0, 1, 2)], dtype=np.int32),
    )
    runtime = native.NativeCosseratRuntime(
        np.asarray([(0.1, 0.1, 0.006)], dtype=np.float32),
        np.zeros((1, 3), dtype=np.float32),
        np.empty((0, 2), dtype=np.int32),
        topology,
        body,
        np.zeros(1, dtype=np.int32),
        contact_thickness_m=0.01,
    )
    try:
        runtime.advance(np.asarray([(0, 0)], dtype=np.int32), 0.0, 1)
        positions, _velocities = runtime.state()
        assert float(positions[0, 2]) > 0.007
    finally:
        runtime.close()


def set_vector_attribute(mesh, name, values):
    attribute = mesh.attributes.new(name=name, type="FLOAT_VECTOR", domain="POINT")
    for item, value in zip(attribute.data, values):
        item.vector = value


def make_part(collection, name, vertices):
    mesh = bpy.data.meshes.new(f"{name}_MESH")
    mesh.from_pydata(vertices, [], [(0, 1, 2, 3)])
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj["yohsai_role"] = "part"
    obj["yohsai_gravity_state"] = "DONE"
    obj["yohsai_kitsuke_locked"] = False
    obj["yohsai_kitsuke_matrix"] = [value for row in obj.matrix_world for value in row]
    set_vector_attribute(mesh, "yohsai_pattern_position", vertices)
    set_vector_attribute(mesh, "yohsai_kitsuke_velocity", [(0.0, 0.0, 0.0)] * 4)
    rest = mesh.attributes.new(name="yohsai_pattern_edge_rest", type="FLOAT", domain="EDGE")
    for edge, item in zip(mesh.edges, rest.data):
        a, b = edge.vertices
        item.value = (mesh.vertices[a].co - mesh.vertices[b].co).length
    family = mesh.attributes.new(name="yohsai_grainline_family", type="INT", domain="EDGE")
    for item in family.data:
        item.value = 3
    quad = mesh.attributes.new(name="yohsai_grainline_quad", type="INT", domain="FACE")
    for item in quad.data:
        item.value = -1
    return obj


def make_armature_body(scene):
    mesh = bpy.data.meshes.new("BODY_MESH")
    mesh.from_pydata(
        [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update(calc_edges=True)
    body = bpy.data.objects.new("BODY", mesh)
    scene.collection.objects.link(body)

    armature = bpy.data.armatures.new("BODY_ARMATURE")
    rig = bpy.data.objects.new("BODY_RIG", armature)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature.edit_bones.new("Body")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")

    group = body.vertex_groups.new(name="Body")
    group.add(list(range(len(mesh.vertices))), 1.0, "REPLACE")
    modifier = body.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = rig
    pose_bone = rig.pose.bones["Body"]
    pose_bone.location = (0.0, 0.0, 0.0)
    pose_bone.keyframe_insert(data_path="location", frame=1)
    pose_bone.location = (0.0, 0.0, 0.02)
    pose_bone.keyframe_insert(data_path="location", frame=2)
    return body


scene = bpy.context.scene
check_contact_clearance_bridge()
scene.frame_start = 1
scene.frame_end = 2
source = bpy.data.collections.new("CLOTHES_TEST")
scene.collection.children.link(source)
source["yohsai_role"] = "clothes"

left = make_part(
    source,
    "LEFT",
    [(-0.2, -0.1, 0.1), (0.0, -0.1, 0.1), (0.0, 0.1, 0.1), (-0.2, 0.1, 0.1)],
)
right = make_part(
    source,
    "RIGHT",
    [(0.0, -0.1, 0.1), (0.2, -0.1, 0.1), (0.2, 0.1, 0.1), (0.0, 0.1, 0.1)],
)
source["yohsai_kitsuke_parts"] = [left.name, right.name]
source["yohsai_kitsuke_seams"] = [1, 4, 2, 7]
source["yohsai_kitsuke_seam_rest"] = [0.0, 0.0]
source["yohsai_kitsuke_revision"] = 1
source["yohsai_kitsuke_backend"] = "STABLE_COSSERAT"
body = make_armature_body(scene)

with RestrictBlend():
    haori.register()
try:
    assert bpy.app.timers.is_registered(ui._initialize_scenes_after_register)
    assert ui._initialize_scenes_after_register() is None
    props = scene.haori
    props.source_collection = source
    props.body_object = body
    props.start_frame = 1
    props.end_frame = 2
    props.performance_preset = "FAST"
    assert props.maximum_step_cm == 2.0
    assert props.contact_clearance_cm == 0.75
    assert props.solver_iterations == 10
    props.maximum_step_cm = 1.0
    props.contact_clearance_cm = 0.6
    props.solver_iterations = 12
    assert props.performance_preset == "CUSTOM"
    original_left = [tuple(vertex.co) for vertex in left.data.vertices]
    assert bpy.ops.haori.simulate() == {"FINISHED"}, props.status

    outputs = [
        collection
        for collection in bpy.data.collections
        if collection.get("haori_role") == "simulation"
    ]
    assert len(outputs) == 1
    output = outputs[0]
    assert bool(output["haori_cache_ready"])
    assert int(output["haori_maximum_substeps"]) == 2
    assert abs(float(output["haori_contact_clearance_cm"]) - 0.6) < 1.0e-6
    assert int(output["haori_solver_iterations"]) == 12
    assert int(output["haori_internal_substeps"]) == 8
    assert int(output["haori_maximum_contact_passes_per_frame"]) == 192
    assert len(output.objects) == 2
    assert [tuple(vertex.co) for vertex in left.data.vertices] == original_left
    for obj in output.objects:
        assert obj.get("haori_role") == "simulation_part"
        keys = obj.data.shape_keys
        assert keys is not None
        assert [key.name for key in keys.key_blocks] == ["Basis", "HAORI_0001", "HAORI_0002"]
        assert keys.animation_data is not None
        assert keys.animation_data.drivers
    assert scene.frame_current == 2
    assert props.progress == 1.0
    assert left.hide_get() and right.hide_get()
    assert bpy.ops.haori.bake_result() == {"FINISHED"}, props.status
    assert output.get("haori_role") == "baked"
    assert bool(output["haori_baked"])
    assert output.name.startswith("CLOTHES_TEST_HAORI_BAKED")
    assert all(obj.get("haori_role") == "baked_part" for obj in output.objects)
    assert all(not obj.data.shape_keys.animation_data.drivers for obj in output.objects)
    assert all(obj.data.shape_keys.animation_data.action is not None for obj in output.objects)
    scene.frame_set(1)
    assert all(
        abs(obj.data.shape_keys.eval_time - obj.data.shape_keys.key_blocks["HAORI_0001"].frame)
        < 1.0e-6
        for obj in output.objects
    )
    scene.frame_set(2)
    assert all(
        abs(obj.data.shape_keys.eval_time - obj.data.shape_keys.key_blocks["HAORI_0002"].frame)
        < 1.0e-6
        for obj in output.objects
    )

    rerun = simulation.SimulationRunner(
        bpy.context,
        source,
        body,
        1,
        2,
        1.0,
        0.5,
        20,
    )
    rerun.cancel()
    assert left.hide_get() and right.hide_get()
    assert not any(
        collection.get("haori_role") == "simulation"
        for collection in bpy.data.collections
    )
    assert output.name in bpy.data.collections
    print("HAORI_SIMULATION_OK", props.status)
finally:
    haori.unregister()
