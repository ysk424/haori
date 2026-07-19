# SPDX-License-Identifier: GPL-3.0-or-later
"""Export XZ and YZ silhouette SVGs for the active Blender mesh.

Run this file from Blender's Scripting workspace. See UTIL/README.md.
"""

from __future__ import annotations

import os
from collections import defaultdict
from html import escape

import bpy
from mathutils import Vector


# Leave empty to write beside the saved .blend file, or to the home directory
# when the .blend file has not been saved. A Blender // relative path is valid.
OUTPUT_DIRECTORY = ""


def projection_data(obj: bpy.types.Object, axis: str):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        matrix = eval_obj.matrix_world
        normal_matrix = matrix.to_3x3().inverted().transposed()
        view_direction = Vector((0.0, 1.0, 0.0)) if axis == "Y" else Vector((1.0, 0.0, 0.0))
        edge_faces = defaultdict(list)
        world_vertices = [matrix @ vertex.co for vertex in mesh.vertices]

        for polygon in mesh.polygons:
            facing = (normal_matrix @ polygon.normal).normalized().dot(view_direction)
            for edge_key in polygon.edge_keys:
                edge_faces[tuple(sorted(edge_key))].append(facing)

        segments = []
        points = []
        for edge_key, facings in edge_faces.items():
            if len(facings) > 1 and not (min(facings) <= 0.0 <= max(facings)):
                continue
            a = world_vertices[edge_key[0]]
            b = world_vertices[edge_key[1]]
            segment = (a.x, a.z, b.x, b.z) if axis == "Y" else (a.y, a.z, b.y, b.z)
            segments.append(segment)
            points.extend(((segment[0], segment[1]), (segment[2], segment[3])))

        if not points:
            raise RuntimeError("No silhouette edges were found.")
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return segments, (min(xs), min(ys), max(xs), max(ys))
    finally:
        eval_obj.to_mesh_clear()


def svg_path_data(segments, min_x: float, max_y: float, mm_per_unit: float) -> str:
    commands = []
    for x1, y1, x2, y2 in segments:
        commands.append(
            "M {:.3f} {:.3f} L {:.3f} {:.3f}".format(
                (x1 - min_x) * mm_per_unit,
                (max_y - y1) * mm_per_unit,
                (x2 - min_x) * mm_per_unit,
                (max_y - y2) * mm_per_unit,
            )
        )
    return " ".join(commands)


def write_silhouette_svg(obj: bpy.types.Object, axis: str, filepath: str) -> None:
    segments, (min_x, min_y, max_x, max_y) = projection_data(obj, axis)
    mm_per_unit = max(bpy.context.scene.unit_settings.scale_length, 0.000001) * 1000.0
    padding = 10.0
    width = max(max_x - min_x, 0.001) * mm_per_unit + padding * 2.0
    height = max(max_y - min_y, 0.001) * mm_per_unit + padding * 2.0
    path_data = svg_path_data(segments, min_x, max_y, mm_per_unit)
    label = "XZ shadow" if axis == "Y" else "YZ shadow"
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="{width:.3f}mm" height="{height:.3f}mm" viewBox="0 0 {width:.3f} {height:.3f}">
  <title>{escape(obj.name)} {label}</title>
  <g id="{escape(obj.name)}-{axis}-silhouette" transform="translate({padding:.3f} {padding:.3f})">
    <path d="{path_data}" fill="none" stroke="#000000" stroke-width="0.25" stroke-linecap="round" stroke-linejoin="round"/>
  </g>
</svg>
"""
    with open(filepath, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def output_directory() -> str:
    if OUTPUT_DIRECTORY:
        return bpy.path.abspath(OUTPUT_DIRECTORY)
    if bpy.data.filepath:
        return os.path.dirname(bpy.data.filepath)
    return os.path.expanduser("~")


def main() -> None:
    obj = bpy.context.view_layer.objects.active
    if obj is None or obj.type != "MESH":
        raise RuntimeError("Select one mesh character and make it the active object.")

    directory = output_directory()
    os.makedirs(directory, exist_ok=True)
    safe_name = bpy.path.clean_name(obj.name)
    xz_path = os.path.join(directory, f"{safe_name}_shadow_xz.svg")
    yz_path = os.path.join(directory, f"{safe_name}_shadow_yz.svg")
    write_silhouette_svg(obj, "Y", xz_path)
    write_silhouette_svg(obj, "X", yz_path)
    message = f"Silhouette export complete:\n{xz_path}\n{yz_path}"
    print(message)
    if not bpy.app.background:
        bpy.context.window_manager.popup_menu(
            lambda self, _context: self.layout.label(text=f"Exported to {directory}"),
            title="Haori Silhouette",
            icon="CHECKMARK",
        )


if __name__ == "__main__":
    main()
