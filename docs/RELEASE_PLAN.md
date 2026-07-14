# Release Plan

## v1.8.0-alpha — implemented

- Reduced the public workflow to the six generation steps only.
- Made Real + High + Realistic the one-time maximum-accuracy defaults.
- Added Architectural Model as a dedicated presentation style.
- Reverse-geocodes the actual selected rectangle instead of retaining a stale search name.
- Uses red UI alerts only for genuine errors or unsafe generation conditions.
- Ignores legacy hidden-building correction records during normal six-step generation.

## v1.7.4-alpha — implemented

- Source-first height restoration.
- Preserve mapped building parts in Classic Voxel and Real.
- Keep simplified public controls and malformed-footprint protection.


## v1.7.3-alpha — implemented

- Safe high-level presets only.
- Always-on footprint and height validation.
- Reduced Advanced Options.
- Stable mapped-building defaults.

## v1.7.2-alpha — implemented

- On-demand Building Editor dialog.
- Direct viewport selection and placement.
- Simple/Advanced modes and practical building presets.
- Compact Map Ready panel.

## v1.6.2-alpha — implemented

- Fixed Blender 5.1 index-based polygon tessellation in editable prisms.
- Restored `Add at 3D Cursor`, curve-footprint buildings, and editable replacements.
- Preserved compatibility with Vector-like tessellation results.
- Removed duplicate ring preprocessing in editable prism generation.
- Forty-two passing core and regression tests.

## v1.6.x-alpha — Blender validation and stabilization

- Validate correction creation, regeneration, undo, save/reopen, and export in a
  real Blender 5.1 Windows UI on multiple maps and styles.
- Validate the new direct viewport picker across dense chunk meshes and custom buildings.
- Improve terrain-aware base snapping when elevation sources are introduced.
- Tune complex multipolygon and courtyard replacement roofs.

## Beta milestones

- Cached GIS downloads and resumable generation reports.
- Optional PBR texture library and texture baking for interchange export.
- Broader Windows installation/runtime validation.
- Example projects, final user guide, and migration tests.

## Later milestones

- Terrain elevation and terrain-aware foundations.
- Municipal/LiDAR/photogrammetry adapters.
- Unity package for geographic metadata, labels, chunks, and runtime LOD.
- Cross-platform packages after validation on each Blender platform.
