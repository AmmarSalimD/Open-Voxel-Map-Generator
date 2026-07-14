# OVMG Architecture — v1.8.0-alpha

## Layers

- `domain`: immutable GIS, generation, provenance, accuracy, and correction inputs.
- `application`: generation, correction-aware filtering, and deletion use cases.
- `infrastructure`: OSM, Overture, networking, map selector, Blender scene IO,
  materials, editable-building geometry, correction persistence, and export.
- `voxel`: projection, rasterization, sparse world, greedy meshing, direct
  building meshing, style profiles, curved details, labels, and accuracy overlay.
- `presentation`: Blender properties, operators, registration, guided wizard,
  Map Ready workspace, Building Inspector, and Building Correction Studio.

## Generated building pipeline

1. OSM and Overture features are normalized to `GeoFeature`.
2. Hybrid de-duplication prefers Overture geometry and preserves OSM IDs/tags.
3. Confirmed correction exclusions are applied before analysis and all geometry.
4. `BuildingAnalyzer` resolves projected footprint rings, height, minimum height,
   roof, facade, provenance, confidence, building-part, and parent metadata.
5. Style routing selects:
   - Classic Voxel/Minecraft: sparse rasterization and greedy meshing.
   - Low Poly/Real: direct polygon extrusion batched by chunk/material.
6. Optional curved details, labels, and confidence overlays are built separately.
7. `BlenderSceneRepository` writes optimized geometry and per-building JSON metadata.

## Non-destructive correction architecture

Generated chunk objects are never split to edit one source building. Instead:

- `BuildingMetadataReader` resolves the inspected source record.
- `BuildingCorrectionStore` persists suppressed source IDs, source-to-replacement
  links, and added editable objects in a project-specific Blender Text datablock.
- `editable_buildings.py` creates standalone footprint prisms and roof components.
- Exact projected source rings and holes are stored on editable roots for
  deterministic numeric rebuilding.
- Regeneration omits suppressed sources before voxelization/direct meshing and
  preserves the independent user-building collection.
- Restoring a source removes its linked replacement and re-enables the source on
  the next regeneration.
- Imported meshes can be registered in the same persistent/exportable layer but
  use transform editing because no source footprint metadata exists.

## Style invariants

- Minecraft: macro blocks, flat roofs, stepped heights, no parts or curves.
- Low Poly: simplified direct footprints, no parts, low-segment curves.
- Real: direct source footprints, parts, source heights/roofs, curved landmarks,
  and source-aware facades.
- Classic Voxel: optimized voxel path with source roof information.

## Performance

- Sparse occupancy and greedy face merging.
- XY chunking and direct-building batching.
- No Blender object per generated source building.
- Per-building records live in compact JSON metadata.
- Only corrected/added buildings become separate editable objects.
- Optional overlays, labels, curved details, realistic materials, and facade
  detail remain independently controlled.


## Simplified Building Editor

The post-generation UI is an adapter over the existing correction operators. The main panel exposes one on-demand dialog, while modal viewport pick/place operators ray-cast into the scene and translate clicks into source-metadata selection or editable-object placement. The persistent correction registry and generated chunk pipeline remain unchanged.
