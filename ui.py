# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Haori animation controls."""

from __future__ import annotations

import os
import tomllib

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Collection, Object, Operator, Panel, PropertyGroup

from .simulation import (
    HAORI_ROLE,
    HAORI_SIMULATION_ROLE,
    SimulationRunner,
    detect_yohsai_inputs,
)


_active_runner: SimulationRunner | None = None
_active_operator = None


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
    maximum_step_cm: FloatProperty(
        name="Maximum Body Step (cm)",
        description="Maximum evaluated Body vertex movement per Gravity call",
        default=1.0,
        min=0.01,
        soft_max=10.0,
        precision=3,
    )
    status: StringProperty(name="Status", default="Ready")
    progress: FloatProperty(name="Progress", default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    range_initialized: BoolProperty(default=False, options={"HIDDEN"})


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


@persistent
def _load_post(_unused) -> None:
    global _active_runner, _active_operator
    if _active_runner is not None and _active_runner.runtime is not None:
        _active_runner.runtime.close()
    _active_runner = None
    _active_operator = None
    for scene in bpy.data.scenes:
        _initialize_scene(scene)


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
        settings.prop(props, "maximum_step_cm")
        layout.separator(factor=0.5)
        row = layout.row()
        row.enabled = _active_runner is None
        row.scale_y = 1.4
        row.operator(HAORI_OT_simulate.bl_idname, icon="PLAY")
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


_CLASSES = (
    HAORI_PG_settings,
    HAORI_OT_detect_inputs,
    HAORI_OT_simulate,
    HAORI_PT_main,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.haori = PointerProperty(type=HAORI_PG_settings)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    for scene in bpy.data.scenes:
        _initialize_scene(scene)


def unregister() -> None:
    global _active_runner, _active_operator
    if _active_runner is not None:
        _active_runner.cancel()
    _active_runner = None
    _active_operator = None
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if hasattr(bpy.types.Scene, "haori"):
        del bpy.types.Scene.haori
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
