[README.md](https://github.com/user-attachments/files/34701074/README.md)
# Souls-Like Messages

A Blender 5.2+ extension that shows customizable cinematic status messages in the Render Result and/or 3D Viewport depending on the event.

## Events

The extension can display independent messages for:

- Successful render completion: `RENDER COMPLETED`
- Successful `.blend` save: `FILE SAVED`
- Detected Cycles GPU out-of-memory render failure: `VRAM EXCEEDED`

All three event types can be enabled/disabled separately and their message text can be customized. Display routing is event-specific: render completion uses Render Result only, file saved uses both the 3D Viewport and Render Result, and VRAM exceeded uses the 3D Viewport only.

## VRAM / GPU OOM detection

Blender does not expose a dedicated VRAM-exceeded handler. The extension snapshots Blender's Window Manager reports when a render starts, then checks reports produced by that render when it completes or is cancelled.

A failure is classified as GPU OOM only when:

- the render is a Cycles GPU render, and
- a new report is `ERROR_OUT_OF_MEMORY`, or a new error message contains a recognized GPU-memory failure phrase.

This avoids treating a normal Esc/cancel as a VRAM failure. It is still a report-based detector, so unusual third-party render engines or future error wording may require additional matching.

## Settings

Open:

`Edit > Preferences > Add-ons > Souls-Like Messages`

The add-on exposes:

- Master enable/disable
- Per-event enable switches and messages
- Optional custom font file
- Text scale, color, opacity, letter spacing, vertical position, and min/max font size
- Background color, opacity, height, width mode, and width
- Fade-in, hold, and fade-out timing
- Outline enable, width, and color
- Glow enable, strength, and radius
- Render / Saved / VRAM preview buttons
- Dismiss control

## v1.5.1 changes

- Added event-specific display routing.
- `RENDER COMPLETED` draws only in an Image Editor displaying `Render Result`.
- `FILE SAVED` draws in both 3D Viewports and Render Result Image Editors.
- `VRAM EXCEEDED` draws only in 3D Viewports.
- Preview buttons now use the same routing as their real events.
- Draw handlers remain event-scoped, so only the required editor types are registered while an overlay is active.

## Notes

- `RENDER COMPLETED` remains restricted to Image Editors whose image type is `RENDER_RESULT`.
- `FILE SAVED` appears in visible 3D Viewports and visible Render Result Image Editors.
- `VRAM EXCEEDED` appears only in visible 3D Viewports.
- Custom fonts are referenced from disk and are not bundled in the extension.
- Invalid or missing custom font files fall back to Blender's default font.


## Distribution and permissions

Maintainer: Nirved. Licensed under GPL-3.0-or-later; see LICENSE.
File access is used only to load an optional, user-selected font. No fonts, images, game assets, bytecode, network access, updater, or third-party dependencies are bundled. The default font works without external files.

## v1.5.2 changes

- Updated the listing name and maintainer.
- Declared optional font-file access.
- Removed legacy add-on metadata in favor of the extension manifest.

## v1.5.3 performance changes

- No forced editor refreshes during the static hold phase; 30 FPS fades remain.
- A new message restarts the timer immediately, including during a prior hold.
- Skip render-report snapshots for disabled notifications, non-GPU-Cycles renders, and background sessions.
- Stop searching new reports at the first matching GPU-memory error.
- Avoid redraw requests when dismissing an already inactive overlay.

A deterministic comparison using default timings (0.32 s fade-in, 1.86 s hold, 0.62 s fade-out) reduced timer callbacks from 86 to 31 and forced redraw requests from 87 to 31. These are scheduling counts, not measured GPU timings or viewport FPS. Blender may redraw naturally for other reasons. There is no recurring timer or draw handler while no message is active. Text-layout and rectangle caches remain bounded at 32 and 16 entries respectively.

The overlay still has a temporary rendering cost during display, especially with outline, glow, long custom text, multiple windows, or expensive rendered viewports. Turn off Glow and Outline in preferences if needed. Registration, background-mode handlers, and cleanup passed a Blender 4.3.2 smoke test in the build environment; the declared supported minimum remains 5.2. Blender 5.3 visual/runtime testing of this revision remains pending on the target machine.
