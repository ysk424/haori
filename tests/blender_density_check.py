"""Report the production PDF mesh density without running Kitsuke."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import bpy


installed_check = os.environ.get("HAORI_INSTALLED_CHECK") == "1"
if installed_check:
    from bl_ext.user_default import haori  # noqa: E402
    from bl_ext.user_default.haori import mesh_loader, ui  # noqa: E402
else:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "tests"))
    from source_package import load_source_package  # noqa: E402

    haori = load_source_package(repo)
    mesh_loader = sys.modules[f"{haori.__name__}.mesh_loader"]
    ui = sys.modules[f"{haori.__name__}.ui"]

source = Path.home() / "Desktop" / "test2.pdf"
if not source.is_file():
    raise RuntimeError("Missing integration input: Desktop/test2.pdf")

if not installed_check:
    haori.register()
try:
    started = time.perf_counter()
    bpy.context.scene.haori.svg_path = str(source)
    assert bpy.ops.haori.load_svg() == {"FINISHED"}
    assert ui._parse_process is not None
    ui._parse_process.wait(timeout=30)
    ui._poll_svg_parser()

    collection = bpy.context.scene.haori.clothes_collection
    assert collection is not None, bpy.context.scene.haori.parse_status
    parts = [
        obj
        for obj in collection.objects
        if obj.type == "MESH" and obj.get("haori_role") == "part"
    ]
    vertices = sum(len(obj.data.vertices) for obj in parts)
    faces = sum(len(obj.data.polygons) for obj in parts)
    family_counts = {
        mesh_loader.GRAINLINE_EDGE_PROXY: 0,
        mesh_loader.GRAINLINE_EDGE_WARP: 0,
        mesh_loader.GRAINLINE_EDGE_WEFT: 0,
        mesh_loader.GRAINLINE_EDGE_TRANSITION: 0,
    }
    quad_count = 0
    grid_cell_count = 0
    for obj in parts:
        mesh = obj.data
        family = mesh.attributes.get(mesh_loader.GRAINLINE_EDGE_FAMILY_ATTRIBUTE)
        quad = mesh.attributes.get(mesh_loader.GRAINLINE_FACE_QUAD_ATTRIBUTE)
        pattern = mesh.attributes.get("haori_pattern_position")
        assert family is not None and family.domain == "EDGE" and family.data_type == "INT"
        assert quad is not None and quad.domain == "FACE" and quad.data_type == "INT"
        assert pattern is not None and pattern.domain == "POINT"

        groups = {}
        for polygon in mesh.polygons:
            quad_index = int(quad.data[polygon.index].value)
            if quad_index >= 0:
                groups.setdefault(quad_index, []).append(set(polygon.vertices))
        edge_lookup = {tuple(sorted(edge.vertices)): edge.index for edge in mesh.edges}
        proxy_edges = set()
        for triangles in groups.values():
            assert len(triangles) == 2
            assert len(triangles[0] | triangles[1]) == 4
            shared = tuple(sorted(triangles[0] & triangles[1]))
            assert len(shared) == 2
            edge_index = edge_lookup[shared]
            assert int(family.data[edge_index].value) == mesh_loader.GRAINLINE_EDGE_PROXY
            proxy_edges.add(edge_index)
        quad_count += len(groups)

        for edge in mesh.edges:
            value = int(family.data[edge.index].value)
            assert value in family_counts
            family_counts[value] += 1
            a, b = edge.vertices
            delta_x = float(pattern.data[b].vector[0] - pattern.data[a].vector[0])
            delta_y = float(pattern.data[b].vector[1] - pattern.data[a].vector[1])
            if value == mesh_loader.GRAINLINE_EDGE_WARP:
                assert abs(delta_x) < 1.0e-6 and abs(delta_y) > 1.0e-8
            elif value == mesh_loader.GRAINLINE_EDGE_WEFT:
                assert abs(delta_y) < 1.0e-6 and abs(delta_x) > 1.0e-8
        assert proxy_edges == {
            edge.index
            for edge in mesh.edges
            if int(family.data[edge.index].value) == mesh_loader.GRAINLINE_EDGE_PROXY
        }
        grid_coordinates = set()
        for item in pattern.data:
            scaled_x = float(item.vector[0]) / mesh_loader.MESH_SPACING_M
            scaled_y = float(item.vector[1]) / mesh_loader.MESH_SPACING_M
            grid_x, grid_y = round(scaled_x), round(scaled_y)
            if abs(scaled_x - grid_x) < 1.0e-4 and abs(scaled_y - grid_y) < 1.0e-4:
                grid_coordinates.add((grid_x, grid_y))
        grid_cell_count += sum(
            (grid_x + 1, grid_y) in grid_coordinates
            and (grid_x + 1, grid_y + 1) in grid_coordinates
            and (grid_x, grid_y + 1) in grid_coordinates
            for grid_x, grid_y in grid_coordinates
        )

    for obj in parts:
        obj.location.x += 0.001
    mesh_loader.mark_moved_parts_pending(collection)
    mesh_loader.create_sewn_mesh(bpy.context, collection)
    sewn = next(obj for obj in collection.objects if obj.get("haori_role") == "sewn")
    spring_edges = {
        edge_index
        for name in ("sewing_spring_A", "sewing_spring_B")
        for edge_index, item in enumerate(sewn.data.attributes[name].data)
        if item.value
    }
    elapsed = time.perf_counter() - started

    assert mesh_loader.MESH_SPACING_M == 0.005
    assert vertices == 19_692, (vertices, faces, quad_count, grid_cell_count, family_counts)
    assert faces == 38_468, (vertices, faces, quad_count, grid_cell_count, family_counts)
    assert quad_count == 17_767
    assert grid_cell_count == 18_032
    assert quad_count / grid_cell_count > 0.98
    assert family_counts[mesh_loader.GRAINLINE_EDGE_PROXY] == quad_count
    assert family_counts[mesh_loader.GRAINLINE_EDGE_WARP] == 18_302
    assert family_counts[mesh_loader.GRAINLINE_EDGE_WEFT] == 18_014
    assert family_counts[mesh_loader.GRAINLINE_EDGE_TRANSITION] == 4_075
    print(
        "HAORI_DENSITY_OK",
        f"parts={len(parts)}",
        f"verts={vertices}",
        f"faces={faces}",
        f"quads={quad_count}",
        f"grid_cells={grid_cell_count}",
        f"warp={family_counts[mesh_loader.GRAINLINE_EDGE_WARP]}",
        f"weft={family_counts[mesh_loader.GRAINLINE_EDGE_WEFT]}",
        f"transition={family_counts[mesh_loader.GRAINLINE_EDGE_TRANSITION]}",
        f"springs={len(spring_edges)}",
        f"spacing_mm={mesh_loader.MESH_SPACING_M * 1000.0:g}",
        f"load_seconds={elapsed:.3f}",
    )
finally:
    if not installed_check:
        haori.unregister()
