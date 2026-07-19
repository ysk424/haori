# SPDX-License-Identifier: GPL-3.0-or-later
"""Restart-safe Yohsai cloth animation on an evaluated, moving Body."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from .cosserat_native import NativeCosseratError, NativeCosseratRuntime


GRAVITY_M_PER_SECOND_SQUARED = 9.81
SOLVER_ITERATIONS = 20
INTERNAL_SUBSTEPS = 8
COLLISION_SEARCH_M = 0.04
MAX_BODY_SUBSTEPS = 512

YOHSAI_ROLE = "yohsai_role"
YOHSAI_PART_ROLE = "part"
YOHSAI_CLOTHES_ROLE = "clothes"
YOHSAI_GRAVITY_STATE = "yohsai_gravity_state"
YOHSAI_LOCKED = "yohsai_kitsuke_locked"
YOHSAI_MATRIX = "yohsai_kitsuke_matrix"
YOHSAI_PART_NAMES = "yohsai_kitsuke_parts"
YOHSAI_SEAMS = "yohsai_kitsuke_seams"
YOHSAI_SEAM_STATE = "yohsai_kitsuke_seam_rest"
YOHSAI_REVISION = "yohsai_kitsuke_revision"
YOHSAI_BACKEND = "yohsai_kitsuke_backend"
YOHSAI_VELOCITY = "yohsai_kitsuke_velocity"
YOHSAI_PATTERN_POSITION = "yohsai_pattern_position"
YOHSAI_EDGE_REST = "yohsai_pattern_edge_rest"
YOHSAI_EDGE_FAMILY = "yohsai_grainline_family"
YOHSAI_FACE_QUAD = "yohsai_grainline_quad"

EDGE_PROXY = 0
EDGE_WARP = 1
EDGE_WEFT = 2

HAORI_ROLE = "haori_role"
HAORI_SIMULATION_ROLE = "simulation"
HAORI_PART_ROLE = "simulation_part"
HAORI_BAKED_ROLE = "baked"
HAORI_BAKED_PART_ROLE = "baked_part"


class HaoriSimulationError(RuntimeError):
    """The selected saved state cannot be simulated safely."""


@dataclass(frozen=True)
class PartRange:
    source: bpy.types.Object
    output: bpy.types.Object | None
    start: int
    count: int
    locked: bool


@dataclass(frozen=True)
class BodySnapshot:
    vertices: np.ndarray
    faces: np.ndarray
    bvh: BVHTree | None
    ray_distance: float
    bounds_minimum: np.ndarray
    bounds_maximum: np.ndarray


@dataclass(frozen=True)
class ClothTopology:
    edges: np.ndarray
    edge_rest_lengths: np.ndarray
    quads: np.ndarray
    quad_rest_metrics: np.ndarray
    bends: np.ndarray
    bend_rest_lengths: np.ndarray


@dataclass(frozen=True)
class SourceState:
    parts: tuple[PartRange, ...]
    positions: np.ndarray
    velocities: np.ndarray
    locked: np.ndarray
    seams: np.ndarray
    seam_state: np.ndarray
    topology: ClothTopology


@dataclass(frozen=True)
class PendingBodyStep:
    time: float
    snapshot: BodySnapshot
    movement_m: float


def _matrix_tuple(matrix: Matrix) -> tuple[float, ...]:
    return tuple(float(value) for row in matrix for value in row)


def _mesh_vertices(mesh: bpy.types.Mesh) -> np.ndarray:
    result = np.empty((len(mesh.vertices), 3), dtype=np.float32)
    mesh.vertices.foreach_get("co", result.ravel())
    return result


def _transform_points(points: np.ndarray, matrix: Matrix) -> np.ndarray:
    transform = np.asarray([tuple(row) for row in matrix], dtype=np.float32)
    result = np.asarray(points, dtype=np.float32) @ transform[:3, :3].T
    result += transform[:3, 3]
    return np.ascontiguousarray(result, dtype=np.float32)


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    return _transform_points(_mesh_vertices(obj.data), obj.matrix_world)


def _read_vector_attribute(
    mesh: bpy.types.Mesh,
    name: str,
    expected_count: int,
) -> np.ndarray:
    attribute = mesh.attributes.get(name)
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "FLOAT_VECTOR"
        or len(attribute.data) != expected_count
    ):
        raise HaoriSimulationError(f"{mesh.name} has no valid {name} data.")
    result = np.empty((expected_count, 3), dtype=np.float32)
    attribute.data.foreach_get("vector", result.ravel())
    if not np.all(np.isfinite(result)):
        raise HaoriSimulationError(f"{mesh.name} contains non-finite {name} data.")
    return result


def _source_objects(collection: bpy.types.Collection) -> tuple[bpy.types.Object, ...]:
    if collection is None or collection.get(YOHSAI_ROLE) != YOHSAI_CLOTHES_ROLE:
        raise HaoriSimulationError("Select a Yohsai Clothes collection.")
    names = [str(value) for value in collection.get(YOHSAI_PART_NAMES, [])]
    if len(names) < 2 or len(set(names)) != len(names):
        raise HaoriSimulationError(
            "The saved Yohsai Gravity state has no unambiguous ordered part list."
        )
    by_name = {
        obj.name: obj
        for obj in collection.objects
        if obj.type == "MESH" and obj.get(YOHSAI_ROLE) == YOHSAI_PART_ROLE
    }
    missing = [name for name in names if name not in by_name]
    if missing:
        raise HaoriSimulationError(
            f"The saved Yohsai Gravity parts are missing: {', '.join(missing)}"
        )
    parts = tuple(by_name[name] for name in names)
    if any(obj.get(YOHSAI_GRAVITY_STATE) != "DONE" for obj in parts):
        raise HaoriSimulationError("Every Yohsai part must have a completed Gravity state.")
    if int(collection.get(YOHSAI_REVISION, 0)) <= 0:
        raise HaoriSimulationError("The Yohsai collection has no completed Gravity revision.")
    backend = str(collection.get(YOHSAI_BACKEND, "STABLE_COSSERAT"))
    if backend != "STABLE_COSSERAT":
        raise HaoriSimulationError(f"The saved Yohsai solver {backend!r} is not supported.")
    return parts


def _cloth_topology(parts: Iterable[PartRange]) -> ClothTopology:
    edges: list[tuple[int, int]] = []
    edge_rest_lengths: list[float] = []
    quads: list[tuple[int, int, int, int]] = []
    quad_rest_metrics: list[tuple[float, float, float]] = []
    bends: list[tuple[int, int, int]] = []
    bend_rest_lengths: list[tuple[float, float]] = []

    for part in parts:
        mesh = part.source.data
        pattern_attribute = mesh.attributes.get(YOHSAI_PATTERN_POSITION)
        rest_attribute = mesh.attributes.get(YOHSAI_EDGE_REST)
        family_attribute = mesh.attributes.get(YOHSAI_EDGE_FAMILY)
        quad_attribute = mesh.attributes.get(YOHSAI_FACE_QUAD)
        if (
            pattern_attribute is None
            or pattern_attribute.domain != "POINT"
            or pattern_attribute.data_type != "FLOAT_VECTOR"
            or len(pattern_attribute.data) != len(mesh.vertices)
        ):
            raise HaoriSimulationError(f"{part.source.name} has no valid pattern coordinates.")
        if (
            rest_attribute is None
            or rest_attribute.domain != "EDGE"
            or rest_attribute.data_type != "FLOAT"
            or len(rest_attribute.data) != len(mesh.edges)
        ):
            raise HaoriSimulationError(f"{part.source.name} has no valid material edge lengths.")
        if (
            family_attribute is None
            or family_attribute.domain != "EDGE"
            or family_attribute.data_type != "INT"
            or len(family_attribute.data) != len(mesh.edges)
        ):
            raise HaoriSimulationError(f"{part.source.name} has no valid grainline edge map.")
        if (
            quad_attribute is None
            or quad_attribute.domain != "FACE"
            or quad_attribute.data_type != "INT"
            or len(quad_attribute.data) != len(mesh.polygons)
        ):
            raise HaoriSimulationError(f"{part.source.name} has no valid grainline quad map.")

        pattern = np.asarray(
            [tuple(float(value) for value in item.vector) for item in pattern_attribute.data],
            dtype=np.float64,
        )
        families = np.empty(len(mesh.edges), dtype=np.int32)
        family_attribute.data.foreach_get("value", families)
        local_rest = np.asarray(
            [float(item.value) for item in rest_attribute.data],
            dtype=np.float64,
        )
        if not np.all(np.isfinite(pattern)) or not np.all(np.isfinite(local_rest)):
            raise HaoriSimulationError(f"{part.source.name} contains non-finite material data.")

        axial_adjacency: dict[tuple[int, int], list[int]] = {}
        for edge in mesh.edges:
            family = int(families[edge.index])
            if family == EDGE_PROXY:
                continue
            a, b = (int(value) for value in edge.vertices)
            rest_length = float(local_rest[edge.index])
            if not rest_length > 1.0e-8:
                raise HaoriSimulationError(f"{part.source.name} has a zero-length material edge.")
            edges.append((part.start + a, part.start + b))
            edge_rest_lengths.append(rest_length)
            if family in (EDGE_WARP, EDGE_WEFT):
                axial_adjacency.setdefault((family, a), []).append(b)
                axial_adjacency.setdefault((family, b), []).append(a)

        quad_groups: dict[int, set[int]] = {}
        for polygon in mesh.polygons:
            quad_index = int(quad_attribute.data[polygon.index].value)
            if quad_index >= 0:
                quad_groups.setdefault(quad_index, set()).update(
                    int(value) for value in polygon.vertices
                )
        for quad_index in sorted(quad_groups):
            corners = quad_groups[quad_index]
            if len(corners) != 4:
                raise HaoriSimulationError(
                    f"{part.source.name} grainline quad {quad_index} is invalid."
                )
            center = pattern[list(corners), :2].mean(axis=0)
            ordered = sorted(
                corners,
                key=lambda vertex: float(
                    np.arctan2(
                        pattern[vertex, 1] - center[1],
                        pattern[vertex, 0] - center[0],
                    )
                ),
            )
            p0, p1, p2, p3 = (pattern[vertex] for vertex in ordered)
            u = 0.5 * ((p1 - p0) + (p2 - p3))
            v = 0.5 * ((p3 - p0) + (p2 - p1))
            uu = float(np.dot(u, u))
            vv = float(np.dot(v, v))
            uv = float(np.dot(u, v))
            if uu <= 1.0e-16 or vv <= 1.0e-16:
                raise HaoriSimulationError(
                    f"{part.source.name} grainline quad {quad_index} is degenerate."
                )
            quads.append(tuple(part.start + vertex for vertex in ordered))
            quad_rest_metrics.append((uu, vv, uv))

        for (_family, center_vertex), neighbors in sorted(axial_adjacency.items()):
            if len(neighbors) < 2:
                continue
            best: tuple[float, int, int] | None = None
            for left_index, left in enumerate(neighbors[:-1]):
                left_direction = pattern[left, :2] - pattern[center_vertex, :2]
                left_length = float(np.linalg.norm(left_direction))
                if left_length <= 1.0e-8:
                    continue
                for right in neighbors[left_index + 1 :]:
                    right_direction = pattern[right, :2] - pattern[center_vertex, :2]
                    right_length = float(np.linalg.norm(right_direction))
                    if right_length <= 1.0e-8:
                        continue
                    cosine = float(
                        np.dot(left_direction, right_direction) / (left_length * right_length)
                    )
                    if cosine <= -0.95 and (best is None or cosine < best[0]):
                        best = (cosine, left, right)
            if best is None:
                continue
            _cosine, left, right = best
            left_length = float(np.linalg.norm(pattern[left] - pattern[center_vertex]))
            right_length = float(np.linalg.norm(pattern[right] - pattern[center_vertex]))
            bends.append((part.start + left, part.start + center_vertex, part.start + right))
            bend_rest_lengths.append((left_length, right_length))

    return ClothTopology(
        np.asarray(edges, dtype=np.int32).reshape((-1, 2)),
        np.asarray(edge_rest_lengths, dtype=np.float32),
        np.asarray(quads, dtype=np.int32).reshape((-1, 4)),
        np.asarray(quad_rest_metrics, dtype=np.float32).reshape((-1, 3)),
        np.asarray(bends, dtype=np.int32).reshape((-1, 3)),
        np.asarray(bend_rest_lengths, dtype=np.float32).reshape((-1, 2)),
    )


def read_source_state(collection: bpy.types.Collection) -> SourceState:
    """Read a completed Yohsai state without trusting its process-local epoch."""
    objects = _source_objects(collection)
    parts: list[PartRange] = []
    position_blocks: list[np.ndarray] = []
    velocity_blocks: list[np.ndarray] = []
    locked_blocks: list[np.ndarray] = []
    offset = 0
    for obj in objects:
        count = len(obj.data.vertices)
        if count <= 0:
            raise HaoriSimulationError(f"{obj.name} has no cloth vertices.")
        positions = _world_vertices(obj)
        if not np.all(np.isfinite(positions)):
            raise HaoriSimulationError(f"{obj.name} contains non-finite world positions.")
        velocities = _read_vector_attribute(obj.data, YOHSAI_VELOCITY, count)
        stored_matrix = tuple(float(value) for value in obj.get(YOHSAI_MATRIX, []))
        if len(stored_matrix) != 16:
            raise HaoriSimulationError(f"{obj.name} has no saved Yohsai transform.")
        if not np.allclose(
            stored_matrix,
            _matrix_tuple(obj.matrix_world),
            rtol=0.0,
            atol=1.0e-7,
        ):
            velocities.fill(0.0)
        locked = bool(obj.get(YOHSAI_LOCKED, False))
        parts.append(PartRange(obj, None, offset, count, locked))
        position_blocks.append(positions)
        velocity_blocks.append(velocities)
        locked_blocks.append(np.full(count, 1 if locked else 0, dtype=np.int32))
        offset += count

    seam_values = np.asarray(collection.get(YOHSAI_SEAMS, []), dtype=np.int32)
    if not seam_values.size or seam_values.ndim != 1 or seam_values.size % 2:
        raise HaoriSimulationError("The saved Yohsai sewing pairs are missing or malformed.")
    seams = seam_values.reshape((-1, 2))
    if (
        np.any(seams < 0)
        or np.any(seams >= offset)
        or np.any(seams[:, 0] == seams[:, 1])
        or len({tuple(sorted((int(a), int(b)))) for a, b in seams}) != len(seams)
    ):
        raise HaoriSimulationError("The saved Yohsai sewing pairs do not match the cloth vertices.")
    seam_state = np.asarray(collection.get(YOHSAI_SEAM_STATE, []), dtype=np.float32)
    if seam_state.shape != (len(seams),) or not np.all(np.isfinite(seam_state)):
        raise HaoriSimulationError("The saved Yohsai seam state does not match the sewing pairs.")

    positions = np.concatenate(position_blocks).astype(np.float32, copy=False)
    velocities = np.concatenate(velocity_blocks).astype(np.float32, copy=False)
    locked = np.concatenate(locked_blocks)
    topology = _cloth_topology(parts)
    return SourceState(tuple(parts), positions, velocities, locked, seams, seam_state, topology)


def set_scene_time(scene: bpy.types.Scene, time: float) -> None:
    base = math.floor(float(time))
    scene.frame_set(base, subframe=float(time) - base)


def body_snapshot(
    context,
    body: bpy.types.Object,
    *,
    build_cpu_bvh: bool = True,
) -> BodySnapshot:
    if body is None or body.type != "MESH":
        raise HaoriSimulationError("Select the armature-deformed mesh Body.")
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = body.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        vertices = _transform_points(_mesh_vertices(mesh), evaluated.matrix_world)
        faces = np.asarray(
            [triangle.vertices[:] for triangle in mesh.loop_triangles],
            dtype=np.int32,
        ).reshape((-1, 3))
        if evaluated.matrix_world.to_3x3().determinant() < 0.0:
            faces = np.ascontiguousarray(faces[:, (0, 2, 1)])
    finally:
        evaluated.to_mesh_clear()
    if not len(vertices) or not len(faces):
        raise HaoriSimulationError("The evaluated Body has no collision triangles.")
    if not np.all(np.isfinite(vertices)):
        raise HaoriSimulationError("The evaluated Body contains non-finite vertices.")
    bvh = None
    if build_cpu_bvh:
        bvh = BVHTree.FromPolygons(
            [Vector(tuple(float(value) for value in vertex)) for vertex in vertices],
            [tuple(int(value) for value in face) for face in faces],
            all_triangles=True,
        )
    bounds_minimum = vertices.min(axis=0)
    bounds_maximum = vertices.max(axis=0)
    diagonal = float(np.linalg.norm(bounds_maximum - bounds_minimum))
    return BodySnapshot(
        vertices,
        faces,
        bvh,
        max(diagonal * 2.0, 1.0),
        bounds_minimum,
        bounds_maximum,
    )


_PARITY_DIRECTIONS = tuple(
    Vector(direction).normalized()
    for direction in (
        (1.0, 0.371, 0.529),
        (-0.417, 1.0, 0.263),
        (0.193, -0.487, 1.0),
    )
)


def _ray_intersection_count(body: BodySnapshot, point: Vector, direction: Vector) -> int:
    if body.bvh is None:
        raise HaoriSimulationError("The requested CPU Body BVH was not constructed.")
    count = 0
    origin = point.copy()
    remaining = body.ray_distance
    epsilon = max(body.ray_distance * 1.0e-7, 1.0e-7)
    while remaining > epsilon:
        location, _normal, face_index, distance = body.bvh.ray_cast(
            origin, direction, remaining
        )
        if face_index is None or location is None or distance is None:
            break
        count += 1
        advance = float(distance) + epsilon
        origin += direction * advance
        remaining -= advance
        if count > 1024:
            break
    return count


def _inside_body(body: BodySnapshot, point: np.ndarray) -> bool:
    origin = Vector(tuple(float(value) for value in point))
    odd_votes = sum(
        _ray_intersection_count(body, origin, direction) % 2
        for direction in _PARITY_DIRECTIONS
    )
    return odd_votes >= 2


def body_collision_candidates(
    positions: np.ndarray,
    body: BodySnapshot,
    unlocked: np.ndarray,
) -> np.ndarray:
    if body.bvh is None:
        raise HaoriSimulationError("The requested CPU Body BVH was not constructed.")
    pairs: list[tuple[int, int]] = []
    padding = COLLISION_SEARCH_M + 1.0e-6
    candidate_mask = np.all(
        (positions >= body.bounds_minimum - padding)
        & (positions <= body.bounds_maximum + padding),
        axis=1,
    )
    candidate_mask &= unlocked
    for vertex_index in np.flatnonzero(candidate_mask):
        point = positions[vertex_index]
        _location, _normal, face_index, _distance = body.bvh.find_nearest(
            Vector(tuple(float(value) for value in point)),
            COLLISION_SEARCH_M,
        )
        if face_index is None and _inside_body(body, point):
            _location, _normal, face_index, _distance = body.bvh.find_nearest(
                Vector(tuple(float(value) for value in point))
            )
        if face_index is not None:
            pairs.append((int(vertex_index), int(face_index)))
    return np.asarray(pairs, dtype=np.int32).reshape((-1, 2))


def maximum_body_movement(first: BodySnapshot, second: BodySnapshot) -> tuple[float, int]:
    if first.vertices.shape != second.vertices.shape or first.faces.shape != second.faces.shape:
        raise HaoriSimulationError(
            "The evaluated Body topology changes between frames. Armature deformation must preserve it."
        )
    distances = np.linalg.norm(second.vertices - first.vertices, axis=1)
    if not len(distances) or not np.all(np.isfinite(distances)):
        raise HaoriSimulationError("Could not measure finite Body movement.")
    index = int(np.argmax(distances))
    return float(distances[index]), index


def required_body_substeps(maximum_movement_m: float, limit_m: float) -> int:
    if not math.isfinite(limit_m) or limit_m <= 0.0:
        raise HaoriSimulationError("Maximum Body Step must be greater than zero.")
    if not math.isfinite(maximum_movement_m) or maximum_movement_m < 0.0:
        raise HaoriSimulationError("Body movement is invalid.")
    count = max(1, int(math.ceil(maximum_movement_m / limit_m - 1.0e-9)))
    if count > MAX_BODY_SUBSTEPS:
        raise HaoriSimulationError(
            f"One frame requires {count} Body steps; the safety limit is {MAX_BODY_SUBSTEPS}."
        )
    return count


def _remove_output_collection(collection: bpy.types.Collection) -> None:
    meshes: list[bpy.types.Mesh] = []
    for obj in list(collection.objects):
        if obj.type == "MESH" and obj.data is not None:
            meshes.append(obj.data)
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)
    for mesh in meshes:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def remove_previous_outputs(source_collection: bpy.types.Collection) -> bool:
    removed = False
    for collection in list(bpy.data.collections):
        if (
            collection.get(HAORI_ROLE) == HAORI_SIMULATION_ROLE
            and collection.get("haori_source_collection") == source_collection.name
        ):
            _remove_output_collection(collection)
            removed = True
    return removed


def create_output_parts(
    scene: bpy.types.Scene,
    source_collection: bpy.types.Collection,
    source_parts: tuple[PartRange, ...],
    start_frame: int,
    end_frame: int,
    maximum_step_cm: float,
    contact_clearance_cm: float,
    solver_iterations: int,
) -> tuple[bpy.types.Collection, tuple[PartRange, ...], bool]:
    replaced_previous = remove_previous_outputs(source_collection)
    collection = bpy.data.collections.new(f"{source_collection.name}_HAORI")
    scene.collection.children.link(collection)
    collection[HAORI_ROLE] = HAORI_SIMULATION_ROLE
    collection["haori_source_collection"] = source_collection.name
    collection["haori_start_frame"] = int(start_frame)
    collection["haori_end_frame"] = int(end_frame)
    collection["haori_maximum_body_step_cm"] = float(maximum_step_cm)
    collection["haori_contact_clearance_cm"] = float(contact_clearance_cm)
    collection["haori_solver_iterations"] = int(solver_iterations)
    collection["haori_internal_substeps"] = INTERNAL_SUBSTEPS
    collection["haori_backend"] = "CUDA_RESIDENT"
    output_parts: list[PartRange] = []
    try:
        for part in source_parts:
            source = part.source
            output = source.copy()
            output.data = source.data.copy()
            output.name = f"{source.name}_HAORI"
            output.data.name = f"{source.data.name}_HAORI"
            output.animation_data_clear()
            output.parent = None
            output.matrix_world = source.matrix_world.copy()
            output.modifiers.clear()
            output.constraints.clear()
            collection.objects.link(output)
            if output.data.shape_keys is not None:
                output.shape_key_clear()
            for key in list(output.keys()):
                del output[key]
            output[HAORI_ROLE] = HAORI_PART_ROLE
            output["haori_source_object"] = source.name
            output["haori_part_index"] = len(output_parts)
            output.hide_set(False)
            output.hide_render = False
            output_parts.append(
                PartRange(source, output, part.start, part.count, part.locked)
            )
    except Exception:
        _remove_output_collection(collection)
        raise
    return collection, tuple(output_parts), replaced_previous


def ready_output_for_source(
    source_collection: bpy.types.Collection | None,
) -> bpy.types.Collection | None:
    if source_collection is None:
        return None
    return next(
        (
            collection
            for collection in reversed(tuple(bpy.data.collections))
            if collection.get(HAORI_ROLE) == HAORI_SIMULATION_ROLE
            and collection.get("haori_source_collection") == source_collection.name
            and bool(collection.get("haori_cache_ready", False))
        ),
        None,
    )


def _set_eval_time_interpolation_linear(keys: bpy.types.Key) -> None:
    animation = keys.animation_data
    action = None if animation is None else animation.action
    if action is None:
        raise HaoriSimulationError(f"{keys.name} did not create a Bake action.")
    if hasattr(action, "fcurves"):
        fcurves = tuple(action.fcurves)
    else:
        fcurves = tuple(
            fcurve
            for layer in action.layers
            for strip in layer.strips
            if hasattr(strip, "channelbags")
            for channelbag in strip.channelbags
            for fcurve in channelbag.fcurves
        )
    eval_fcurves = tuple(fcurve for fcurve in fcurves if fcurve.data_path == "eval_time")
    if not eval_fcurves:
        raise HaoriSimulationError(f"{keys.name} did not create an eval_time F-Curve.")
    for fcurve in eval_fcurves:
        for point in fcurve.keyframe_points:
            point.interpolation = "LINEAR"


def bake_output_collection(collection: bpy.types.Collection) -> str:
    """Finalize a ready cache so later simulations never replace it."""
    if (
        collection is None
        or collection.get(HAORI_ROLE) != HAORI_SIMULATION_ROLE
        or not bool(collection.get("haori_cache_ready", False))
    ):
        raise HaoriSimulationError("No completed HAORI simulation is ready to bake.")
    parts = tuple(
        obj
        for obj in collection.objects
        if obj.type == "MESH" and obj.get(HAORI_ROLE) == HAORI_PART_ROLE
    )
    if not parts:
        raise HaoriSimulationError("The completed HAORI simulation has no output parts.")
    start_frame = int(collection.get("haori_start_frame", 0))
    end_frame = int(collection.get("haori_end_frame", 0))
    if end_frame <= start_frame:
        raise HaoriSimulationError("The completed HAORI simulation has an invalid frame range.")
    bake_data: list[tuple[bpy.types.Object, bpy.types.Key, tuple[tuple[int, float], ...]]] = []
    for obj in parts:
        keys = obj.data.shape_keys
        if keys is None or len(keys.key_blocks) < end_frame - start_frame + 2:
            raise HaoriSimulationError(f"{obj.name} has no complete HAORI Shape Key cache.")
        frame_values: list[tuple[int, float]] = []
        for frame in range(start_frame, end_frame + 1):
            shape = keys.key_blocks.get(f"HAORI_{frame:04d}")
            if shape is None:
                raise HaoriSimulationError(
                    f"{obj.name} is missing the HAORI Shape Key for frame {frame}."
                )
            frame_values.append((frame, float(shape.frame)))
        bake_data.append((obj, keys, tuple(frame_values)))

    for _obj, keys, frame_values in bake_data:
        keys.driver_remove("eval_time")
        for frame, value in frame_values:
            keys.eval_time = value
            if not keys.keyframe_insert(data_path="eval_time", frame=frame, group="HAORI Bake"):
                raise HaoriSimulationError(
                    f"Could not keyframe {keys.name} eval_time at frame {frame}."
                )
        _set_eval_time_interpolation_linear(keys)

    source_name = str(collection.get("haori_source_collection", "HAORI"))
    collection[HAORI_ROLE] = HAORI_BAKED_ROLE
    collection["haori_baked"] = True
    collection.name = f"{source_name}_HAORI_BAKED"
    for obj, _keys, _frame_values in bake_data:
        source_object = str(obj.get("haori_source_object", obj.name))
        obj[HAORI_ROLE] = HAORI_BAKED_PART_ROLE
        obj["haori_baked"] = True
        obj.name = f"{source_object}_HAORI_BAKED"
        obj.data.name = f"{source_object}_HAORI_BAKED_MESH"
    return collection.name


def _scatter_positions(parts: Iterable[PartRange], positions: np.ndarray) -> None:
    for part in parts:
        if part.output is None:
            continue
        selection = positions[part.start : part.start + part.count]
        local = _transform_points(selection, part.output.matrix_world.inverted_safe())
        part.output.data.vertices.foreach_set("co", local.ravel())
        part.output.data.update()


def _install_shape_key_cache(
    parts: Iterable[PartRange],
    frame_cache: dict[int, np.ndarray],
) -> None:
    frames = sorted(frame_cache)
    if not frames:
        raise HaoriSimulationError("The simulation produced no animation frames.")
    for part in parts:
        output = part.output
        if output is None:
            continue
        if output.data.shape_keys is not None:
            output.shape_key_clear()
        output.shape_key_add(name="Basis", from_mix=False)
        keys = output.data.shape_keys
        keys.use_relative = False
        key_frames: list[float] = []
        for frame in frames:
            shape = output.shape_key_add(name=f"HAORI_{frame:04d}", from_mix=False)
            selection = frame_cache[frame][part.start : part.start + part.count]
            local = _transform_points(selection, output.matrix_world.inverted_safe())
            shape.data.foreach_set("co", local.ravel())
            shape.interpolation = "KEY_LINEAR"
            key_frames.append(float(shape.frame))
        if len(key_frames) < 2:
            raise HaoriSimulationError("At least two cached frames are required.")
        spacing = key_frames[1] - key_frames[0]
        if not spacing > 0.0 or any(
            abs((right - left) - spacing) > 1.0e-6
            for left, right in zip(key_frames, key_frames[1:])
        ):
            raise HaoriSimulationError("Blender assigned an unexpected absolute Shape Key scale.")
        keys.eval_time = key_frames[-1]
        fcurve = keys.driver_add("eval_time")
        fcurve.driver.type = "SCRIPTED"
        fcurve.driver.expression = (
            f"{key_frames[0]:.9g} + (frame - ({frames[0]})) * {spacing:.9g}"
        )
        output["haori_cache_start"] = int(frames[0])
        output["haori_cache_end"] = int(frames[-1])


def detect_yohsai_inputs(
    scene: bpy.types.Scene,
) -> tuple[bpy.types.Collection | None, bpy.types.Object | None]:
    collection = None
    body = None
    if hasattr(scene, "yohsai"):
        collection = scene.yohsai.clothes_collection
        body = scene.yohsai.body_object
    if collection is None:
        collection = next(
            (
                item
                for item in bpy.data.collections
                if item.get(YOHSAI_ROLE) == YOHSAI_CLOTHES_ROLE
                and int(item.get(YOHSAI_REVISION, 0)) > 0
            ),
            None,
        )
    return collection, body


class SimulationRunner:
    """Advance one Body pose per call and cache integer-frame cloth results."""

    def __init__(
        self,
        context,
        source_collection: bpy.types.Collection,
        body: bpy.types.Object,
        start_frame: int,
        end_frame: int,
        maximum_step_cm: float,
        contact_clearance_cm: float = 1.0,
        solver_iterations: int = SOLVER_ITERATIONS,
    ):
        if end_frame <= start_frame:
            raise HaoriSimulationError("End Frame must be greater than Start Frame.")
        if not math.isfinite(maximum_step_cm) or maximum_step_cm <= 0.0:
            raise HaoriSimulationError("Maximum Body Step must be greater than zero.")
        if (
            not math.isfinite(contact_clearance_cm)
            or contact_clearance_cm <= 0.0
            or contact_clearance_cm > COLLISION_SEARCH_M * 100.0
        ):
            raise HaoriSimulationError(
                f"Contact Clearance must be greater than zero and at most "
                f"{COLLISION_SEARCH_M * 100.0:g} cm."
            )
        if not 1 <= int(solver_iterations) <= 128:
            raise HaoriSimulationError("Solver Iterations must be between 1 and 128.")
        self.scene = context.scene
        self.source_collection = source_collection
        self.body_object = body
        self.start_frame = int(start_frame)
        self.end_frame = int(end_frame)
        self.maximum_step_cm = float(maximum_step_cm)
        self.maximum_step_m = self.maximum_step_cm / 100.0
        self.contact_clearance_cm = float(contact_clearance_cm)
        self.contact_clearance_m = self.contact_clearance_cm / 100.0
        self.solver_iterations = int(solver_iterations)
        self.initial_frame = int(self.scene.frame_current)
        self.initial_subframe = float(self.scene.frame_subframe)
        self.current_frame = self.start_frame
        self.current_time = float(self.start_frame)
        self.pending_steps: list[PendingBodyStep] = []
        self.frame_cache: dict[int, np.ndarray] = {}
        self.maximum_observed_body_movement_m = 0.0
        self.maximum_movement_vertex = -1
        self.maximum_substeps = 1
        self.last_maximum_vertex = -1
        self.last_interval_substeps = 1
        self.output_collection: bpy.types.Collection | None = None
        self.output_parts: tuple[PartRange, ...] = ()
        self.runtime: NativeCosseratRuntime | None = None
        self.finished = False
        self.source_visibility: list[tuple[bpy.types.Object, bool, bool]] = []

        set_scene_time(self.scene, float(self.start_frame))
        context.view_layer.update()
        source = read_source_state(source_collection)
        initial_body = body_snapshot(context, body, build_cpu_bvh=False)
        try:
            runtime = NativeCosseratRuntime(
                source.positions,
                source.velocities,
                source.seams,
                source.topology,
                initial_body,
                source.locked,
                contact_thickness_m=self.contact_clearance_m,
            )
            runtime.replace_seam_state(source.seam_state)
            output_collection, output_parts, replaced_previous = create_output_parts(
                self.scene,
                source_collection,
                source.parts,
                self.start_frame,
                self.end_frame,
                self.maximum_step_cm,
                self.contact_clearance_cm,
                self.solver_iterations,
            )
        except Exception:
            if "runtime" in locals():
                runtime.close()
            set_scene_time(
                self.scene,
                float(self.initial_frame) + self.initial_subframe,
            )
            raise
        self.runtime = runtime
        self.output_collection = output_collection
        self.output_parts = output_parts
        self.positions = source.positions.copy()
        self.velocities = source.velocities.copy()
        self.locked = source.locked.copy()
        self.body = initial_body
        try:
            self.source_visibility = [
                (
                    part.source,
                    False if replaced_previous else part.source.hide_get(),
                    False if replaced_previous else bool(part.source.hide_render),
                )
                for part in source.parts
            ]
            for source_object, _hidden, _hide_render in self.source_visibility:
                source_object.hide_set(True)
                source_object.hide_render = True
            _scatter_positions(self.output_parts, self.positions)
            self.frame_cache[self.start_frame] = self.positions.copy()
        except Exception:
            runtime.close()
            _remove_output_collection(output_collection)
            for source_object, hidden, hide_render in self.source_visibility:
                source_object.hide_set(hidden)
                source_object.hide_render = hide_render
            set_scene_time(
                self.scene,
                float(self.initial_frame) + self.initial_subframe,
            )
            raise

    @property
    def progress(self) -> float:
        span = self.end_frame - self.start_frame
        return min(1.0, max(0.0, (self.current_time - self.start_frame) / span))

    @property
    def status(self) -> str:
        if self.finished:
            return "Simulation complete"
        if self.pending_steps:
            completed = self.last_interval_substeps - len(self.pending_steps)
            return (
                f"Frame {self.current_frame}->{self.current_frame + 1}: "
                f"Body step {completed}/{self.last_interval_substeps}"
            )
        return f"Preparing frame {self.current_frame}->{self.current_frame + 1}"

    def _validate_body_topology(self, snapshot: BodySnapshot) -> None:
        if (
            snapshot.vertices.shape != self.body.vertices.shape
            or snapshot.faces.shape != self.body.faces.shape
        ):
            raise HaoriSimulationError(
                "The evaluated Body topology changed during the frame range."
            )

    def _prepare_interval(self, context) -> None:
        start_time = float(self.current_frame)
        end_time = float(self.current_frame + 1)
        set_scene_time(self.scene, end_time)
        context.view_layer.update()
        end_snapshot = body_snapshot(context, self.body_object, build_cpu_bvh=False)
        self._validate_body_topology(end_snapshot)
        maximum, vertex_index = maximum_body_movement(self.body, end_snapshot)
        count = required_body_substeps(maximum, self.maximum_step_m)

        while True:
            snapshots: list[PendingBodyStep] = []
            previous = self.body
            worst = 0.0
            for index in range(1, count + 1):
                time = start_time + index / count
                if index == count:
                    snapshot = end_snapshot
                else:
                    set_scene_time(self.scene, time)
                    context.view_layer.update()
                    snapshot = body_snapshot(context, self.body_object, build_cpu_bvh=False)
                    self._validate_body_topology(snapshot)
                movement, _moving_vertex = maximum_body_movement(previous, snapshot)
                worst = max(worst, movement)
                snapshots.append(PendingBodyStep(time, snapshot, movement))
                previous = snapshot
            if worst <= self.maximum_step_m * (1.0 + 1.0e-5):
                break
            count = max(count + 1, int(math.ceil(count * worst / self.maximum_step_m)))
            if count > MAX_BODY_SUBSTEPS:
                raise HaoriSimulationError(
                    f"Frame {self.current_frame} requires more than "
                    f"{MAX_BODY_SUBSTEPS} Body steps."
                )

        self.pending_steps = snapshots
        self.last_interval_substeps = count
        self.last_maximum_vertex = vertex_index
        self.maximum_substeps = max(self.maximum_substeps, count)
        if maximum > self.maximum_observed_body_movement_m:
            self.maximum_observed_body_movement_m = maximum
            self.maximum_movement_vertex = vertex_index

    def advance_one(self, context) -> bool:
        """Advance one automatic Body subdivision. Return True while work remains."""
        if self.finished:
            return False
        if self.current_frame >= self.end_frame:
            return False
        if not self.pending_steps:
            self._prepare_interval(context)

        step = self.pending_steps.pop(0)
        set_scene_time(self.scene, step.time)
        context.view_layer.update()
        if self.runtime is None:
            raise HaoriSimulationError("The native simulation runtime is closed.")
        try:
            self.runtime.replace_body(step.snapshot.vertices, step.snapshot.faces)
            self.runtime.advance_resident(
                GRAVITY_M_PER_SECOND_SQUARED,
                self.solver_iterations,
            )
        except NativeCosseratError as exc:
            if self.runtime is not None:
                self.runtime.replace_state(
                    self.positions,
                    self.velocities,
                    self.locked,
                )
            raise HaoriSimulationError(str(exc)) from exc
        self.body = step.snapshot
        self.current_time = step.time

        if not self.pending_steps:
            try:
                positions, velocities = self.runtime.state()
            except NativeCosseratError as exc:
                self.runtime.replace_state(self.positions, self.velocities, self.locked)
                raise HaoriSimulationError(str(exc)) from exc
            if not np.all(np.isfinite(positions)) or not np.all(np.isfinite(velocities)):
                self.runtime.replace_state(self.positions, self.velocities, self.locked)
                raise HaoriSimulationError("The solver produced a non-finite cloth state.")
            self.positions = positions
            self.velocities = velocities
            _scatter_positions(self.output_parts, self.positions)
            self.current_frame += 1
            self.current_time = float(self.current_frame)
            self.frame_cache[self.current_frame] = self.positions.copy()
        return self.current_frame < self.end_frame

    def finish(self) -> str:
        if self.current_frame < self.end_frame or self.pending_steps:
            raise HaoriSimulationError("The simulation has not reached End Frame.")
        _install_shape_key_cache(self.output_parts, self.frame_cache)
        if self.output_collection is not None:
            self.output_collection["haori_cache_ready"] = True
            self.output_collection["haori_maximum_substeps"] = int(self.maximum_substeps)
            self.output_collection["haori_maximum_body_movement_cm"] = float(
                self.maximum_observed_body_movement_m * 100.0
            )
            self.output_collection["haori_maximum_body_vertex"] = int(
                self.maximum_movement_vertex
            )
            self.output_collection["haori_maximum_contact_passes_per_frame"] = int(
                self.maximum_substeps * INTERNAL_SUBSTEPS * self.solver_iterations
            )
            self.output_collection["haori_body_object"] = self.body_object.name
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None
        set_scene_time(self.scene, float(self.end_frame))
        self.finished = True
        return (
            f"CUDA cached frames {self.start_frame}-{self.end_frame}; "
            f"maximum Body motion {self.maximum_observed_body_movement_m * 100.0:.3f} cm; "
            f"up to {self.maximum_substeps} Gravity call(s) and "
            f"{self.maximum_substeps * INTERNAL_SUBSTEPS * self.solver_iterations} "
            f"contact pass(es) per frame"
        )

    def cancel(self) -> None:
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None
        if self.output_collection is not None and self.output_collection.name in bpy.data.collections:
            _remove_output_collection(self.output_collection)
            self.output_collection = None
        for source_object, hidden, hide_render in self.source_visibility:
            if source_object.name in bpy.data.objects:
                source_object.hide_set(hidden)
                source_object.hide_render = hide_render
        set_scene_time(
            self.scene,
            float(self.initial_frame) + self.initial_subframe,
        )
        self.finished = True

    def run_to_completion(self, context) -> str:
        while self.advance_one(context):
            pass
        return self.finish()
