# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Haori animation controls."""

from __future__ import annotations

import os
import tomllib

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Collection, Object, Operator, Panel, PropertyGroup

from .simulation import (
    HAORI_BAKED_ROLE,
    HAORI_ROLE,
    HAORI_SIMULATION_ROLE,
    INTERNAL_SUBSTEPS,
    SimulationRunner,
    bake_output_collection,
    detect_yohsai_inputs,
    ready_output_for_source,
)


_active_runner: SimulationRunner | None = None
_active_operator = None

_PERFORMANCE_PRESETS = {
    "FAST": (2.0, 0.75, 10),
    "STANDARD": (1.0, 0.5, 20),
    "QUALITY": (0.5, 0.5, 30),
}


def _version() -> str:
    try:
        path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
        with open(path, "rb") as handle:
            return str(tomllib.load(handle).get("version", "?"))
    except Exception:
        return "?"


def _mesh_poll(_properties, obj: Object) -> bool:
    return obj is not None and obj.type == "MESH"


def _clothes_poll(_properties, collection: Collection) -> bool:
    return collection is not None and collection.get("yohsai_role") == "clothes"


def _apply_performance_preset(properties, _context) -> None:
    values = _PERFORMANCE_PRESETS.get(properties.performance_preset)
    if values is None:
        return
    properties.preset_updating = True
    try:
        properties.maximum_step_cm = values[0]
        properties.contact_clearance_cm = values[1]
        properties.solver_iterations = values[2]
    finally:
        properties.preset_updating = False


def _mark_custom_preset(properties, _context) -> None:
    if not properties.preset_updating and properties.performance_preset != "CUSTOM":
        properties.performance_preset = "CUSTOM"


class HAORI_PG_settings(PropertyGroup):
    source_collection: PointerProperty(
        name="Yohsai Clothes",
        description="Completed Yohsai Clothes collection used as the initial cloth state",
        type=Collection,
        poll=_clothes_poll,
    )
    body_object: PointerProperty(
        name="Body",
        description="Armature-deformed mesh used for animated collision",
        type=Object,
        poll=_mesh_poll,
    )
    start_frame: IntProperty(
        name="Start Frame",
        description="First cached cloth frame",
        default=1,
    )
    end_frame: IntProperty(
        name="End Frame",
        description="Last cached cloth frame",
        default=250,
    )
    performance_preset: EnumProperty(
        name="Performance",
        description="Choose a speed/quality starting point or edit the values directly",
        items=(
            ("FAST", "Fast", "Fewer Body steps and solver iterations for weak CPUs"),
            ("STANDARD", "Standard", "Balanced preview settings"),
            ("QUALITY", "Quality", "Smaller Body steps and stronger convergence"),
            ("CUSTOM", "Custom", "Use manually edited settings"),
        ),
        default="STANDARD",
        update=_apply_performance_preset,
    )
    maximum_step_cm: FloatProperty(
        name="Maximum Body Step (cm)",
        description="Maximum evaluated Body vertex movement per Gravity call",
        default=1.0,
        min=0.01,
        soft_max=10.0,
        precision=3,
        update=_mark_custom_preset,
    )
    contact_clearance_cm: FloatProperty(
        name="Contact Clearance (cm)",
        description="Body surface distance maintained by contact; larger values reduce visible penetration but make clothes float",
        default=0.5,
        min=0.05,
        max=4.0,
        soft_max=1.0,
        precision=3,
        update=_mark_custom_preset,
    )
    solver_iterations: IntProperty(
        name="Solver Iterations",
        description="Material and Body-contact convergence per internal substep",
        default=20,
        min=1,
        max=128,
        soft_max=40,
        update=_mark_custom_preset,
    )
    status: StringProperty(name="Status", default="Ready")
    progress: FloatProperty(name="Progress", default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    range_initialized: BoolProperty(default=False, options={"HIDDEN"})
    preset_updating: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})


def _initialize_scene(scene: bpy.types.Scene) -> None:
    if not hasattr(scene, "haori"):
        return
    props = scene.haori
    if not props.range_initialized:
        props.start_frame = int(scene.frame_start)
        props.end_frame = int(scene.frame_end)
        props.range_initialized = True
    collection, body = detect_yohsai_inputs(scene)
    if props.source_collection is None and collection is not None:
        props.source_collection = collection
    if props.body_object is None and body is not None:
        props.body_object = body


def _initialize_scenes_after_register():
    """Initialize Scene settings after Blender releases registration restrictions."""
    if not hasattr(bpy.types.Scene, "haori"):
        return None
    try:
        scenes = bpy.data.scenes
    except AttributeError:
        # Extension registration temporarily replaces bpy.data with
        # _RestrictData. Retry once the normal Blender context is restored.
        return 0.1
    for scene in scenes:
        _initialize_scene(scene)
    return None


@persistent
def _load_post(_unused) -> None:
    global _active_runner, _active_operator
    if _active_runner is not None and _active_runner.runtime is not None:
        _active_runner.runtime.close()
    _active_runner = None
    _active_operator = None
    _initialize_scenes_after_register()


class HAORI_OT_detect_inputs(Operator):
    bl_idname = "haori.detect_inputs"
    bl_label = "Detect Yohsai Inputs"
    bl_description = "Use the completed Yohsai Clothes and Body selected in the current scene"

    def execute(self, context):
        collection, body = detect_yohsai_inputs(context.scene)
        if collection is None or body is None:
            self.report({"ERROR"}, "Could not find both completed Yohsai Clothes and Body.")
            return {"CANCELLED"}
        props = context.scene.haori
        props.source_collection = collection
        props.body_object = body
        props.status = f"Detected {collection.name} and {body.name}"
        return {"FINISHED"}


class HAORI_OT_simulate(Operator):
    bl_idname = "haori.simulate"
    bl_label = "Simulate Animation"
    bl_description = "Cache one Normal Gravity result per automatically divided Body pose"
    bl_options = {"REGISTER", "UNDO"}

    _timer = None

    def _stop_timer(self, context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def _clear_active(self) -> None:
        global _active_runner, _active_operator
        _active_runner = None
        _active_operator = None

    def _fail(self, context, message: str):
        global _active_runner
        if _active_runner is not None:
            _active_runner.cancel()
        self._stop_timer(context)
        self._clear_active()
        context.scene.haori.status = f"Failed: {message[:220]}"
        context.scene.haori.progress = 0.0
        self.report({"ERROR"}, message)
        return {"CANCELLED"}

    def execute(self, context):
        global _active_runner, _active_operator
        if _active_runner is not None:
            self.report({"WARNING"}, "A Haori simulation is already running.")
            return {"CANCELLED"}
        props = context.scene.haori
        if props.source_collection is None or props.body_object is None:
            collection, body = detect_yohsai_inputs(context.scene)
            if props.source_collection is None:
                props.source_collection = collection
            if props.body_object is None:
                props.body_object = body
        try:
            runner = SimulationRunner(
                context,
                props.source_collection,
                props.body_object,
                props.start_frame,
                props.end_frame,
                props.maximum_step_cm,
                props.contact_clearance_cm,
                props.solver_iterations,
            )
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            props.status = f"Failed: {message[:220]}"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        _active_runner = runner
        _active_operator = self
        props.status = runner.status
        props.progress = runner.progress
        if bpy.app.background:
            try:
                summary = runner.run_to_completion(context)
            except Exception as exc:
                return self._fail(context, str(exc).strip() or type(exc).__name__)
            props.status = summary
            props.progress = 1.0
            self._clear_active()
            self.report({"INFO"}, summary)
            return {"FINISHED"}

        self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global _active_runner
        if event.type == "ESC":
            if _active_runner is not None:
                _active_runner.cancel()
            self._stop_timer(context)
            self._clear_active()
            context.scene.haori.status = "Cancelled"
            context.scene.haori.progress = 0.0
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        runner = _active_runner
        if runner is None:
            return self._fail(context, "The active Haori simulation was lost.")
        try:
            has_more = runner.advance_one(context)
            context.scene.haori.status = runner.status
            context.scene.haori.progress = runner.progress
            if has_more:
                return {"RUNNING_MODAL"}
            summary = runner.finish()
        except Exception as exc:
            return self._fail(context, str(exc).strip() or type(exc).__name__)
        self._stop_timer(context)
        self._clear_active()
        context.scene.haori.status = summary
        context.scene.haori.progress = 1.0
        self.report({"INFO"}, summary)
        return {"FINISHED"}

    def cancel(self, context):
        global _active_runner
        if _active_runner is not None:
            _active_runner.cancel()
        self._stop_timer(context)
        self._clear_active()


class HAORI_OT_bake_result(Operator):
    bl_idname = "haori.bake_result"
    bl_label = "Bake HAORI Result"
    bl_description = (
        "Finalize the completed Shape Key cache so later HAORI simulations do not replace it"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if _active_runner is not None or not hasattr(context.scene, "haori"):
            return False
        return ready_output_for_source(context.scene.haori.source_collection) is not None

    def execute(self, context):
        props = context.scene.haori
        output = ready_output_for_source(props.source_collection)
        try:
            name = bake_output_collection(output)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            props.status = f"Bake failed: {message[:220]}"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        props.status = f"Baked: {name}"
        self.report({"INFO"}, props.status)
        return {"FINISHED"}


class HAORI_PT_main(Panel):
    bl_label = "Haori"
    bl_idname = "HAORI_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Haori"

    def draw(self, context):
        layout = self.layout
        props = context.scene.haori
        layout.label(text=f"Haori v{_version()}")
        inputs = layout.column(align=True)
        inputs.enabled = _active_runner is None
        inputs.prop(props, "source_collection")
        inputs.prop(props, "body_object")
        inputs.operator(HAORI_OT_detect_inputs.bl_idname, icon="EYEDROPPER")
        layout.separator(factor=0.5)
        settings = layout.column(align=True)
        settings.enabled = _active_runner is None
        settings.prop(props, "start_frame")
        settings.prop(props, "end_frame")
        settings.prop(props, "performance_preset")
        settings.prop(props, "maximum_step_cm")
        settings.prop(props, "contact_clearance_cm")
        settings.prop(props, "solver_iterations")
        settings.label(
            text=(
                f"Per Body step: {INTERNAL_SUBSTEPS} × {props.solver_iterations} = "
                f"{INTERNAL_SUBSTEPS * props.solver_iterations} contact passes"
            )
        )
        layout.separator(factor=0.5)
        row = layout.row()
        row.enabled = _active_runner is None
        row.scale_y = 1.4
        row.operator(HAORI_OT_simulate.bl_idname, icon="PLAY")
        bake_row = layout.row()
        bake_row.enabled = (
            _active_runner is None
            and ready_output_for_source(props.source_collection) is not None
        )
        bake_row.operator(HAORI_OT_bake_result.bl_idname, icon="REC")
        if _active_runner is not None:
            layout.label(text="Press Esc to cancel", icon="EVENT_ESC")
        layout.prop(props, "progress", text="")
        layout.label(text=props.status, icon="INFO")
        outputs = [
            collection
            for collection in bpy.data.collections
            if collection.get(HAORI_ROLE) == HAORI_SIMULATION_ROLE
            and bool(collection.get("haori_cache_ready", False))
        ]
        if outputs:
            layout.label(text=f"Cache: {outputs[-1].name}", icon="OUTLINER_COLLECTION")
        baked = [
            collection
            for collection in bpy.data.collections
            if collection.get(HAORI_ROLE) == HAORI_BAKED_ROLE
        ]
        if baked:
            layout.label(text=f"Baked: {baked[-1].name}", icon="CHECKMARK")


_CLASSES = (
    HAORI_PG_settings,
    HAORI_OT_detect_inputs,
    HAORI_OT_simulate,
    HAORI_OT_bake_result,
    HAORI_PT_main,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.haori = PointerProperty(type=HAORI_PG_settings)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    if not bpy.app.timers.is_registered(_initialize_scenes_after_register):
        bpy.app.timers.register(_initialize_scenes_after_register, first_interval=0.0)


def unregister() -> None:
    global _active_runner, _active_operator
    if _active_runner is not None:
        _active_runner.cancel()
    _active_runner = None
    _active_operator = None
    if bpy.app.timers.is_registered(_initialize_scenes_after_register):
        bpy.app.timers.unregister(_initialize_scenes_after_register)
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if hasattr(bpy.types.Scene, "haori"):
        del bpy.types.Scene.haori
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
