# SPDX-License-Identifier: GPL-3.0-or-later

import math
import os
import time

import blf
import bpy
import gpu
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator
from gpu_extras.batch import batch_for_shader


_FONT_CACHE = {"path": "", "font_id": 0}
_DRAW_HANDLES = {}
_LAYOUT_CACHE = {}
_LAYOUT_CACHE_LIMIT = 32
_BACKGROUND_BATCH_CACHE = {}
_BACKGROUND_BATCH_CACHE_LIMIT = 16
_BACKGROUND_SHADER = None
_RENDER_TRACKING = {
    "active": False,
    "gpu_cycles": False,
    "report_keys": set(),
}
_OVERLAY = {
    "active": False,
    "start": 0.0,
    "fade_in": 0.32,
    "hold_time": 1.86,
    "fade_out": 0.62,
    "message": "RENDER COMPLETED",
    "font_id": 0,
    "text_scale": 1.0,
    "text_color": (0.62, 0.025, 0.015, 1.0),
    "text_opacity": 1.0,
    "letter_spacing": 0.075,
    "vertical_position": 0.50,
    "font_size_min": 24.0,
    "font_size_max": 190.0,
    "background_color": (0.0, 0.0, 0.0, 1.0),
    "background_opacity": 0.65,
    "background_height": 1.65,
    "background_width_mode": "FULL_WIDTH",
    "background_width": 1.0,
    "outline_enabled": True,
    "outline_width": 2.0,
    "outline_color": (0.035, 0.0, 0.0, 0.95),
    "glow_enabled": True,
    "glow_strength": 0.75,
    "glow_radius": 6.0,
    "display_scope": "RENDER_RESULT",
}


def _smoothstep01(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _total_duration():
    return max(
        0.0,
        _OVERLAY["fade_in"] + _OVERLAY["hold_time"] + _OVERLAY["fade_out"],
    )


def _overlay_alpha(now):
    if not _OVERLAY["active"]:
        return 0.0

    elapsed = now - _OVERLAY["start"]
    fade_in = max(0.0, _OVERLAY["fade_in"])
    hold_time = max(0.0, _OVERLAY["hold_time"])
    fade_out = max(0.0, _OVERLAY["fade_out"])
    duration = fade_in + hold_time + fade_out

    if elapsed < 0.0 or elapsed >= duration:
        return 0.0

    if fade_in > 0.0 and elapsed < fade_in:
        return _smoothstep01(elapsed / fade_in)

    fade_out_start = fade_in + hold_time
    if fade_out > 0.0 and elapsed >= fade_out_start:
        return _smoothstep01((duration - elapsed) / fade_out)

    return 1.0


def _area_shows_render_result(area):
    if area.type != "IMAGE_EDITOR":
        return False

    space = area.spaces.active
    image = getattr(space, "image", None)
    return image is not None and image.type == "RENDER_RESULT"


def _area_matches_display_scope(area):
    scope = _OVERLAY["display_scope"]

    if area.type == "VIEW_3D":
        return scope in {"VIEWPORT", "BOTH"}

    if area.type == "IMAGE_EDITOR":
        return scope in {"RENDER_RESULT", "BOTH"} and _area_shows_render_result(area)

    return False


def _tag_supported_areas_for_redraw():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return

    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if _area_matches_display_scope(area):
                area.tag_redraw()


def _redraw_timer():
    if not _OVERLAY["active"]:
        _remove_draw_handlers()
        return None

    now = time.monotonic()
    if now >= _OVERLAY["start"] + _total_duration():
        _OVERLAY["active"] = False
        _remove_draw_handlers()
        _tag_supported_areas_for_redraw()
        return None

    _tag_supported_areas_for_redraw()
    elapsed = now - _OVERLAY["start"]
    fade_in = max(0.0, _OVERLAY["fade_in"])
    fade_out_start = fade_in + max(0.0, _OVERLAY["hold_time"])
    # A fully visible message is static. Let Blender redraw it naturally
    # while we sleep until the fade-out instead of refreshing the whole editor.
    if fade_in <= elapsed < fade_out_start:
        return max(0.001, fade_out_start - elapsed)
    return 1.0 / 30.0


def _ensure_redraw_timer():
    # Restart a timer sleeping through a previous message's hold phase.
    if bpy.app.timers.is_registered(_redraw_timer):
        bpy.app.timers.unregister(_redraw_timer)
    bpy.app.timers.register(_redraw_timer, first_interval=0.0)


def _get_preferences(context=None):
    preferences = getattr(context, "preferences", None) if context is not None else None
    if preferences is None:
        preferences = getattr(bpy.context, "preferences", None)
    if preferences is None:
        return None

    addon = preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


def _clear_layout_cache():
    _LAYOUT_CACHE.clear()


def _clear_gpu_cache():
    global _BACKGROUND_SHADER
    _BACKGROUND_BATCH_CACHE.clear()
    _BACKGROUND_SHADER = None


def _unload_cached_font():
    path = _FONT_CACHE["path"]
    if path:
        try:
            blf.unload(path)
        except (OSError, RuntimeError, ValueError):
            pass

    _FONT_CACHE["path"] = ""
    _FONT_CACHE["font_id"] = 0
    _clear_layout_cache()


def _resolve_font_id(use_custom_font, font_path):
    if not use_custom_font or not font_path.strip():
        # Keep an already loaded custom font cached while it is temporarily disabled.
        # It is unloaded if a different path is selected or the add-on unregisters.
        return 0

    absolute_path = os.path.normpath(bpy.path.abspath(font_path))

    if _FONT_CACHE["path"] == absolute_path and _FONT_CACHE["font_id"] > 0:
        return _FONT_CACHE["font_id"]

    _unload_cached_font()

    if not os.path.isfile(absolute_path):
        return 0

    try:
        font_id = blf.load(absolute_path)
    except (OSError, RuntimeError, ValueError):
        return 0

    if font_id < 0:
        return 0

    _FONT_CACHE["path"] = absolute_path
    _FONT_CACHE["font_id"] = font_id
    return font_id


def _show_overlay(context=None, *, message_override=None, display_scope="RENDER_RESULT"):
    settings = _get_preferences(context)
    if settings is None or not settings.enabled or bpy.app.background:
        return

    if message_override is None:
        message_source = settings.message
    else:
        message_source = message_override
    message = " ".join(message_source.split()) or "RENDER COMPLETED"
    font_id = _resolve_font_id(settings.use_custom_font, settings.font_path)
    font_size_min = min(settings.font_size_min, settings.font_size_max)
    font_size_max = max(settings.font_size_min, settings.font_size_max)

    _OVERLAY.update(
        active=True,
        start=time.monotonic(),
        fade_in=settings.fade_in,
        hold_time=settings.hold_time,
        fade_out=settings.fade_out,
        message=message,
        font_id=font_id,
        text_scale=settings.text_scale,
        text_color=tuple(settings.text_color),
        text_opacity=settings.text_opacity,
        letter_spacing=settings.letter_spacing,
        vertical_position=settings.vertical_position,
        font_size_min=font_size_min,
        font_size_max=font_size_max,
        background_color=tuple(settings.background_color),
        background_opacity=settings.background_opacity,
        background_height=settings.background_height,
        background_width_mode=settings.background_width_mode,
        background_width=settings.background_width,
        outline_enabled=settings.outline_enabled,
        outline_width=settings.outline_width,
        outline_color=tuple(settings.outline_color),
        glow_enabled=settings.glow_enabled,
        glow_strength=settings.glow_strength,
        glow_radius=settings.glow_radius,
        display_scope=display_scope,
    )

    # Event scopes can change between successive overlays. Reconcile handlers
    # immediately so only the editors needed by the current event are active.
    _remove_draw_handlers()
    _add_draw_handlers()
    _tag_supported_areas_for_redraw()
    _ensure_redraw_timer()


def _dismiss_overlay():
    needs_redraw = _OVERLAY["active"] or bool(_DRAW_HANDLES)
    _OVERLAY["active"] = False
    _remove_draw_handlers()

    if bpy.app.timers.is_registered(_redraw_timer):
        bpy.app.timers.unregister(_redraw_timer)

    if needs_redraw:
        _tag_supported_areas_for_redraw()


def _tracked_text_layout(font_id, text, font_size, tracking):
    # BLF font state is global per font id, so always restore the requested size
    # even when the expensive dimensions are served from cache.
    blf.size(font_id, font_size)

    key = (font_id, text, font_size, tracking)
    cached = _LAYOUT_CACHE.get(key)
    if cached is not None:
        return cached

    widths = tuple(blf.dimensions(font_id, char)[0] for char in text)
    offsets = []
    cursor = 0.0
    for index, width in enumerate(widths):
        offsets.append(cursor)
        cursor += width
        if index + 1 < len(widths):
            cursor += tracking

    total_width = cursor
    text_height = blf.dimensions(font_id, "Hg")[1]
    result = (tuple(offsets), total_width, text_height)

    if len(_LAYOUT_CACHE) >= _LAYOUT_CACHE_LIMIT:
        _LAYOUT_CACHE.pop(next(iter(_LAYOUT_CACHE)))
    _LAYOUT_CACHE[key] = result
    return result


def _get_background_shader():
    global _BACKGROUND_SHADER
    if _BACKGROUND_SHADER is None:
        _BACKGROUND_SHADER = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _BACKGROUND_SHADER


def _get_background_batch(x0, y0, x1, y1):
    shader = _get_background_shader()
    key = (x0, y0, x1, y1)
    batch = _BACKGROUND_BATCH_CACHE.get(key)
    if batch is not None:
        return shader, batch

    positions = (
        (x0, y0),
        (x1, y0),
        (x0, y1),
        (x1, y1),
    )
    indices = ((0, 1, 2), (2, 1, 3))
    batch = batch_for_shader(shader, "TRIS", {"pos": positions}, indices=indices)

    if len(_BACKGROUND_BATCH_CACHE) >= _BACKGROUND_BATCH_CACHE_LIMIT:
        _BACKGROUND_BATCH_CACHE.pop(next(iter(_BACKGROUND_BATCH_CACHE)))
    _BACKGROUND_BATCH_CACHE[key] = batch
    return shader, batch


def _draw_rectangle(x0, y0, x1, y1, color, alpha):
    if x1 <= x0 or y1 <= y0 or alpha <= 0.0:
        return

    rgba = (color[0], color[1], color[2], color[3] * alpha)
    shader, batch = _get_background_batch(x0, y0, x1, y1)
    shader.bind()
    shader.uniform_float("color", rgba)
    batch.draw(shader)


def _draw_tracked_text(font_id, text, x, y, offsets, color, alpha, offset=(0.0, 0.0)):
    if alpha <= 0.0:
        return

    blf.color(font_id, color[0], color[1], color[2], color[3] * alpha)
    base_x = x + offset[0]
    draw_y = y + offset[1]

    for char, character_x in zip(text, offsets):
        blf.position(font_id, base_x + character_x, draw_y, 0.0)
        blf.draw(font_id, char)


def _background_bounds(region_width, center_y, band_height, total_text_width, font_size):
    mode = _OVERLAY["background_width_mode"]
    width_scale = max(0.10, min(1.0, _OVERLAY["background_width"]))

    if mode == "FIT_TEXT":
        base_width = total_text_width + font_size * 1.5
        background_width = base_width * width_scale
        minimum_width = total_text_width + font_size * 0.5
        background_width = max(minimum_width, background_width)
        background_width = min(float(region_width), background_width)
    else:
        background_width = float(region_width) * width_scale

    center_x = region_width * 0.5
    half_width = background_width * 0.5
    half_height = band_height * 0.5
    return (
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


def _draw_glow(font_id, message, x, y, character_offsets, text_color, alpha, font_size):
    """Draw a visible halo using a bounded number of explicit text passes.

    BLF's native SHADOW blur can be visually imperceptible on large tracked
    text over a dark background. An explicit halo gives consistent results
    across supported font files and GPU backends.
    """
    if not _OVERLAY["glow_enabled"] or _OVERLAY["glow_strength"] <= 0.0 or alpha <= 0.0:
        return

    strength = max(0.0, min(2.0, _OVERLAY["glow_strength"]))
    configured_radius = max(0.0, _OVERLAY["glow_radius"])
    radius = max(1.0, min(configured_radius, font_size * 0.12))
    glow_color = (
        min(1.0, text_color[0] * 1.25 + 0.08),
        min(1.0, text_color[1] * 1.10 + 0.005),
        min(1.0, text_color[2] * 1.10 + 0.005),
        1.0,
    )
    # Eight passes overlap, so cap each pass to keep the halo controlled.
    glow_alpha = alpha * min(0.34, 0.14 + 0.10 * strength)
    diagonal = radius / math.sqrt(2.0)
    effect_offsets = (
        (-radius, 0.0),
        (radius, 0.0),
        (0.0, -radius),
        (0.0, radius),
        (-diagonal, -diagonal),
        (diagonal, -diagonal),
        (-diagonal, diagonal),
        (diagonal, diagonal),
    )
    for effect_offset in effect_offsets:
        _draw_tracked_text(
            font_id,
            message,
            x,
            y,
            character_offsets,
            glow_color,
            glow_alpha,
            effect_offset,
        )

def _draw_outline(font_id, message, x, y, character_offsets, alpha):
    if not _OVERLAY["outline_enabled"] or _OVERLAY["outline_width"] <= 0.0:
        return

    width = _OVERLAY["outline_width"]
    color = _OVERLAY["outline_color"]
    diagonal = width / math.sqrt(2.0)
    effect_offsets = (
        (-width, 0.0),
        (width, 0.0),
        (0.0, -width),
        (0.0, width),
        (-diagonal, -diagonal),
        (diagonal, -diagonal),
        (-diagonal, diagonal),
        (diagonal, diagonal),
    )
    for effect_offset in effect_offsets:
        _draw_tracked_text(
            font_id, message, x, y, character_offsets, color, alpha, effect_offset
        )


def _draw_overlay():
    if not _OVERLAY["active"]:
        return

    area = bpy.context.area
    if area is None or not _area_matches_display_scope(area):
        return

    now = time.monotonic()
    overlay_alpha = _overlay_alpha(now)
    if overlay_alpha <= 0.0:
        return

    region = bpy.context.region
    if region is None or region.width <= 1 or region.height <= 1:
        return

    width = region.width
    height = region.height
    message = _OVERLAY["message"]
    font_id = _OVERLAY["font_id"]

    font_min = max(8.0, _OVERLAY["font_size_min"])
    font_max = max(font_min, _OVERLAY["font_size_max"])

    # Region-relative sizing keeps the composition consistent across render-window sizes.
    font_size = min(width * 0.078, height * 0.145) * _OVERLAY["text_scale"]
    font_size = max(font_min, min(font_max, font_size))
    tracking = font_size * _OVERLAY["letter_spacing"]

    offsets, total_width, text_height = _tracked_text_layout(font_id, message, font_size, tracking)

    # Fit unusually long custom messages without exceeding the configured minimum size.
    available_width = width * 0.90
    if total_width > available_width and total_width > 0.0:
        fit = available_width / total_width
        font_size = max(font_min, font_size * fit)
        tracking = font_size * _OVERLAY["letter_spacing"]
        offsets, total_width, text_height = _tracked_text_layout(font_id, message, font_size, tracking)

    band_height = max(1.0, font_size * _OVERLAY["background_height"])
    vertical_position = max(0.0, min(1.0, _OVERLAY["vertical_position"]))
    if band_height < height:
        half_band = band_height * 0.5
        center_y = half_band + (height - band_height) * vertical_position
    else:
        center_y = height * 0.5

    background = _OVERLAY["background_color"]
    background_alpha = _OVERLAY["background_opacity"] * overlay_alpha
    x0, y0, x1, y1 = _background_bounds(
        width,
        center_y,
        band_height,
        total_width,
        font_size,
    )

    previous_blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set("ALPHA")
        _draw_rectangle(x0, y0, x1, y1, background, background_alpha)
    finally:
        gpu.state.blend_set(previous_blend)

    x = (width - total_width) * 0.5
    y = center_y - text_height * 0.48
    text_alpha = overlay_alpha * _OVERLAY["text_opacity"]
    main = _OVERLAY["text_color"]

    # Reset BLF shadow state in case another draw callback left it enabled.
    blf.disable(font_id, blf.SHADOW)
    _draw_glow(font_id, message, x, y, offsets, main, text_alpha, font_size)
    _draw_outline(font_id, message, x, y, offsets, text_alpha)
    _draw_tracked_text(font_id, message, x, y, offsets, main, text_alpha)


def _iter_window_manager_reports():
    for window_manager in getattr(bpy.data, "window_managers", ()):
        for report in window_manager.reports:
            yield report


def _report_key(report):
    session_uid = getattr(report, "session_uid", None)
    if session_uid is not None:
        return ("UID", int(session_uid))
    try:
        return ("PTR", int(report.as_pointer()))
    except (AttributeError, ReferenceError):
        return ("TEXT", getattr(report, "type", ""), getattr(report, "message", ""))


def _snapshot_report_keys():
    return {_report_key(report) for report in _iter_window_manager_reports()}


def _new_render_reports():
    baseline = _RENDER_TRACKING["report_keys"]
    return (
        report
        for report in _iter_window_manager_reports()
        if _report_key(report) not in baseline
    )


def _scene_uses_cycles_gpu(scene):
    if scene is None or getattr(getattr(scene, "render", None), "engine", None) != "CYCLES":
        return False
    cycles = getattr(scene, "cycles", None)
    return getattr(cycles, "device", None) == "GPU"


def _report_indicates_out_of_memory(report):
    report_type = getattr(report, "type", "")
    message = getattr(report, "message", "").casefold()

    if report_type == "ERROR_OUT_OF_MEMORY":
        return True

    oom_phrases = (
        "out of memory",
        "out-of-memory",
        "insufficient memory",
        "failed to allocate memory",
        "memory allocation failed",
        "cuda_error_out_of_memory",
        "hiperroroutofmemory",
        "system is out of gpu memory",
    )
    if not any(phrase in message for phrase in oom_phrases):
        return False

    gpu_phrases = ("gpu", "cuda", "optix", "hip", "metal", "oneapi", "device")
    return any(phrase in message for phrase in gpu_phrases)


def _current_render_hit_gpu_oom(scene):
    if not (_RENDER_TRACKING["active"] and _RENDER_TRACKING["gpu_cycles"]):
        return False
    return any(_report_indicates_out_of_memory(report) for report in _new_render_reports())


def _reset_render_tracking():
    _RENDER_TRACKING["active"] = False
    _RENDER_TRACKING["gpu_cycles"] = False
    _RENDER_TRACKING["report_keys"].clear()


@persistent
def _on_render_init(scene):
    _reset_render_tracking()
    settings = _get_preferences()
    if (bpy.app.background or settings is None or not settings.enabled
            or not settings.vram_enabled or not _scene_uses_cycles_gpu(scene)):
        return
    _RENDER_TRACKING["active"] = True
    _RENDER_TRACKING["gpu_cycles"] = _scene_uses_cycles_gpu(scene)
    _RENDER_TRACKING["report_keys"] = _snapshot_report_keys()


@persistent
def _on_render_complete(scene):
    settings = _get_preferences()
    if bpy.app.background or settings is None or not settings.enabled:
        _reset_render_tracking()
        _dismiss_overlay()
        return
    if settings is not None and settings.vram_enabled and _current_render_hit_gpu_oom(scene):
        _show_overlay(message_override=settings.vram_message, display_scope="VIEWPORT")
    elif settings is not None and settings.render_enabled:
        _show_overlay(message_override=settings.message, display_scope="RENDER_RESULT")
    else:
        _dismiss_overlay()
    _reset_render_tracking()


@persistent
def _on_render_cancel(scene):
    settings = _get_preferences()
    if bpy.app.background or settings is None or not settings.enabled:
        _reset_render_tracking()
        _dismiss_overlay()
        return
    if settings is not None and settings.vram_enabled and _current_render_hit_gpu_oom(scene):
        _show_overlay(message_override=settings.vram_message, display_scope="VIEWPORT")
    else:
        _dismiss_overlay()
    _reset_render_tracking()


@persistent
def _on_save_post(filepath):
    if not filepath or bpy.app.background:
        return
    settings = _get_preferences()
    if settings is None or not settings.save_enabled:
        return
    _show_overlay(message_override=settings.save_message, display_scope="BOTH")


class RCO_Preferences(AddonPreferences):
    bl_idname = __package__
    enabled: BoolProperty(
        name="Enabled",
        description="Master switch for cinematic status overlays",
        default=True,
    )
    render_enabled: BoolProperty(
        name="Render Completed",
        description="Show an overlay after a successful render job",
        default=True,
    )
    message: StringProperty(
        name="Render Message",
        description="Text shown when rendering completes",
        default="RENDER COMPLETED",
    )
    save_enabled: BoolProperty(
        name="File Saved",
        description="Show an overlay after the current .blend file is saved",
        default=True,
    )
    save_message: StringProperty(
        name="Save Message",
        description="Text shown after saving a .blend file",
        default="FILE SAVED",
    )
    vram_enabled: BoolProperty(
        name="GPU Out of Memory",
        description="Show an overlay when a Cycles GPU render is cancelled by an out-of-memory error",
        default=True,
    )
    vram_message: StringProperty(
        name="VRAM Message",
        description="Text shown after a detected Cycles GPU out-of-memory failure",
        default="VRAM EXCEEDED",
    )

    use_custom_font: BoolProperty(
        name="Custom Font",
        description="Use an external font file for the completion message",
        default=False,
    )
    font_path: StringProperty(
        name="Font File",
        description="Font file loaded through Blender's BLF text drawing API; invalid files fall back to Blender's default font",
        subtype="FILE_PATH",
        default="",
    )

    text_scale: FloatProperty(
        name="Text Scale",
        description="Scale of the completion message relative to the Render Result region",
        default=1.0,
        min=0.35,
        max=2.0,
        soft_min=0.6,
        soft_max=1.4,
    )
    text_color: FloatVectorProperty(
        name="Text Color",
        description="Main overlay text color",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.62, 0.025, 0.015, 1.0),
    )
    text_opacity: FloatProperty(
        name="Text Opacity",
        description="Opacity of the main completion text",
        default=1.0,
        min=0.0,
        max=1.0,
    )
    letter_spacing: FloatProperty(
        name="Letter Spacing",
        description="Additional spacing between characters as a fraction of the current font size",
        default=0.075,
        min=-0.05,
        max=0.50,
        soft_min=0.0,
        soft_max=0.20,
    )
    vertical_position: FloatProperty(
        name="Vertical Position",
        description="Vertical position of the overlay: 0 is bottom, 0.5 center, 1 top",
        default=0.50,
        min=0.0,
        max=1.0,
    )
    font_size_min: FloatProperty(
        name="Font Size Min",
        description="Minimum text size in pixels when adapting to the Render Result region",
        default=24.0,
        min=8.0,
        max=400.0,
        soft_max=96.0,
    )
    font_size_max: FloatProperty(
        name="Font Size Max",
        description="Maximum text size in pixels when adapting to the Render Result region",
        default=190.0,
        min=8.0,
        max=600.0,
        soft_min=72.0,
        soft_max=260.0,
    )

    background_color: FloatVectorProperty(
        name="Background Color",
        description="Color of the rectangle behind the completion text",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.0, 0.0, 0.0, 1.0),
    )
    background_opacity: FloatProperty(
        name="Background Opacity",
        description="Opacity of the background rectangle",
        default=0.65,
        min=0.0,
        max=1.0,
    )
    background_height: FloatProperty(
        name="Background Height",
        description="Background height as a multiple of the current font size",
        default=1.65,
        min=0.50,
        max=5.0,
        soft_min=1.0,
        soft_max=2.5,
    )
    background_width_mode: EnumProperty(
        name="Background Width Mode",
        description="Choose whether the background spans the render region or fits the text",
        items=(
            ("FULL_WIDTH", "Full Width", "Base the background width on the Render Result region"),
            ("FIT_TEXT", "Fit Text", "Base the background width on the text width plus padding"),
        ),
        default="FULL_WIDTH",
    )
    background_width: FloatProperty(
        name="Background Width",
        description="Width multiplier for the selected background width mode",
        default=1.0,
        min=0.10,
        max=1.0,
    )

    fade_in: FloatProperty(
        name="Fade In",
        description="Time for the overlay to fade from transparent to fully visible",
        default=0.32,
        min=0.0,
        max=5.0,
        soft_max=1.5,
        subtype="TIME",
        unit="TIME",
    )
    hold_time: FloatProperty(
        name="Hold Time",
        description="Time the overlay remains fully visible",
        default=1.86,
        min=0.0,
        max=15.0,
        soft_max=6.0,
        subtype="TIME",
        unit="TIME",
    )
    fade_out: FloatProperty(
        name="Fade Out",
        description="Time for the overlay to fade from fully visible to transparent",
        default=0.62,
        min=0.0,
        max=5.0,
        soft_max=2.0,
        subtype="TIME",
        unit="TIME",
    )

    outline_enabled: BoolProperty(
        name="Outline",
        description="Draw a dark outline around the completion text",
        default=True,
    )
    outline_width: FloatProperty(
        name="Outline Width",
        description="Outline radius in pixels",
        default=2.0,
        min=0.0,
        max=12.0,
        soft_max=6.0,
    )
    outline_color: FloatVectorProperty(
        name="Outline Color",
        description="Color of the text outline",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.035, 0.0, 0.0, 0.95),
    )
    glow_enabled: BoolProperty(
        name="Glow",
        description="Draw a subtle dark-red halo around the completion text",
        default=True,
    )
    glow_strength: FloatProperty(
        name="Glow Strength",
        description="Strength of the text halo",
        default=0.75,
        min=0.0,
        max=2.0,
        soft_max=1.25,
    )
    glow_radius: FloatProperty(
        name="Glow Radius",
        description="Radius of the visible glow halo in pixels",
        default=6.0,
        min=1.0,
        max=30.0,
        soft_max=14.0,
    )


    def draw(self, context):
        layout = self.layout

        layout.prop(self, "enabled")

        content = layout.column()
        content.active = self.enabled

        event_box = content.box()
        event_box.label(text="Events")
        event_box.prop(self, "render_enabled")
        render_message_col = event_box.column(align=True)
        render_message_col.active = self.render_enabled
        render_message_col.prop(self, "message")

        event_box.prop(self, "save_enabled")
        save_message_col = event_box.column(align=True)
        save_message_col.active = self.save_enabled
        save_message_col.prop(self, "save_message")

        event_box.prop(self, "vram_enabled")
        vram_message_col = event_box.column(align=True)
        vram_message_col.active = self.vram_enabled
        vram_message_col.prop(self, "vram_message")
        event_box.label(text="Render: Render Result | Saved: Viewport + Render Result | VRAM: Viewport")

        text_box = content.box()
        text_box.label(text="Text")
        text_box.prop(self, "use_custom_font")
        font_col = text_box.column(align=True)
        font_col.active = self.use_custom_font
        font_col.prop(self, "font_path")
        if self.use_custom_font:
            resolved_path = os.path.normpath(bpy.path.abspath(self.font_path)) if self.font_path.strip() else ""
            if not resolved_path:
                font_col.label(text="No font selected; Blender default font will be used.")
            elif not os.path.isfile(resolved_path):
                font_col.label(text="Font file not found; Blender default font will be used.")
            else:
                font_col.label(text=f"Selected: {os.path.basename(resolved_path)}")
        text_box.prop(self, "text_scale")
        text_box.prop(self, "text_color")
        text_box.prop(self, "text_opacity")
        text_box.prop(self, "letter_spacing")
        text_box.prop(self, "vertical_position")
        row = text_box.row(align=True)
        row.prop(self, "font_size_min")
        row.prop(self, "font_size_max")

        background_box = content.box()
        background_box.label(text="Background")
        background_box.prop(self, "background_color")
        background_box.prop(self, "background_opacity")
        background_box.prop(self, "background_height")
        background_box.prop(self, "background_width_mode")
        background_box.prop(self, "background_width")

        animation_box = content.box()
        animation_box.label(text="Animation")
        animation_box.prop(self, "fade_in")
        animation_box.prop(self, "hold_time")
        animation_box.prop(self, "fade_out")
        total = self.fade_in + self.hold_time + self.fade_out
        animation_box.label(text=f"Total Duration: {total:.2f} s")

        effects_box = content.box()
        effects_box.label(text="Effects")
        effects_box.prop(self, "outline_enabled")
        outline_col = effects_box.column(align=True)
        outline_col.active = self.outline_enabled
        outline_col.prop(self, "outline_width")
        outline_col.prop(self, "outline_color")
        effects_box.prop(self, "glow_enabled")
        glow_col = effects_box.column(align=True)
        glow_col.active = self.glow_enabled
        glow_col.prop(self, "glow_strength")
        glow_col.prop(self, "glow_radius")

        preview_box = content.box()
        preview_box.label(text="Preview")
        row = preview_box.row(align=True)
        op = row.operator(RCO_OT_preview.bl_idname, text="Render", icon="PLAY")
        op.event = "RENDER"
        op = row.operator(RCO_OT_preview.bl_idname, text="Saved")
        op.event = "SAVE"
        op = row.operator(RCO_OT_preview.bl_idname, text="VRAM")
        op.event = "VRAM"
        preview_box.operator(RCO_OT_dismiss.bl_idname, icon="X")


class RCO_OT_preview(Operator):
    bl_idname = "render.render_completed_overlay_preview"
    bl_label = "Preview Overlay"
    bl_description = "Preview one of the configured status overlays"
    bl_options = {"INTERNAL"}

    event: EnumProperty(
        items=(
            ("RENDER", "Render", "Preview the render-completed message"),
            ("SAVE", "Saved", "Preview the file-saved message"),
            ("VRAM", "VRAM", "Preview the GPU out-of-memory message"),
        ),
        default="RENDER",
        options={"HIDDEN"},
    )

    def execute(self, context):
        settings = _get_preferences(context)
        if settings is None:
            return {"CANCELLED"}
        message = {
            "RENDER": settings.message,
            "SAVE": settings.save_message,
            "VRAM": settings.vram_message,
        }.get(self.event, settings.message)
        display_scope = {
            "RENDER": "RENDER_RESULT",
            "SAVE": "BOTH",
            "VRAM": "VIEWPORT",
        }.get(self.event, "RENDER_RESULT")
        _show_overlay(context, message_override=message, display_scope=display_scope)
        return {"FINISHED"}


class RCO_OT_dismiss(Operator):
    bl_idname = "render.render_completed_overlay_dismiss"
    bl_label = "Dismiss"
    bl_description = "Dismiss the current overlay"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        _dismiss_overlay()
        return {"FINISHED"}


_CLASSES = (
    RCO_Preferences,
    RCO_OT_preview,
    RCO_OT_dismiss,
)


def _remove_handler_if_present(handler_list, function):
    while function in handler_list:
        handler_list.remove(function)


def _add_draw_handlers():
    scope = _OVERLAY["display_scope"]

    if scope in {"RENDER_RESULT", "BOTH"} and "IMAGE_EDITOR" not in _DRAW_HANDLES:
        _DRAW_HANDLES["IMAGE_EDITOR"] = bpy.types.SpaceImageEditor.draw_handler_add(
            _draw_overlay, (), "WINDOW", "POST_PIXEL"
        )

    if scope in {"VIEWPORT", "BOTH"} and "VIEW_3D" not in _DRAW_HANDLES:
        _DRAW_HANDLES["VIEW_3D"] = bpy.types.SpaceView3D.draw_handler_add(
            _draw_overlay, (), "WINDOW", "POST_PIXEL"
        )


def _remove_draw_handlers():
    handle = _DRAW_HANDLES.pop("IMAGE_EDITOR", None)
    if handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(handle, "WINDOW")

    handle = _DRAW_HANDLES.pop("VIEW_3D", None)
    if handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    _remove_handler_if_present(bpy.app.handlers.render_init, _on_render_init)
    _remove_handler_if_present(bpy.app.handlers.render_complete, _on_render_complete)
    _remove_handler_if_present(bpy.app.handlers.render_cancel, _on_render_cancel)
    _remove_handler_if_present(bpy.app.handlers.save_post, _on_save_post)
    bpy.app.handlers.render_init.append(_on_render_init)
    bpy.app.handlers.render_complete.append(_on_render_complete)
    bpy.app.handlers.render_cancel.append(_on_render_cancel)
    bpy.app.handlers.save_post.append(_on_save_post)



def unregister():
    _dismiss_overlay()

    if bpy.app.timers.is_registered(_redraw_timer):
        bpy.app.timers.unregister(_redraw_timer)

    _remove_handler_if_present(bpy.app.handlers.render_init, _on_render_init)
    _remove_handler_if_present(bpy.app.handlers.render_complete, _on_render_complete)
    _remove_handler_if_present(bpy.app.handlers.render_cancel, _on_render_cancel)
    _remove_handler_if_present(bpy.app.handlers.save_post, _on_save_post)
    _reset_render_tracking()
    _remove_draw_handlers()
    _unload_cached_font()
    _clear_layout_cache()
    _clear_gpu_cache()

    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
