# Changelog

## 2.0.4

- Added a visible generation progress bar, percentage, and live stage message.
- Shows tile progress during long tiled jobs and forces periodic safe viewport
  redraws so Blender does not appear frozen.
- Expanded right-to-left detection to Arabic presentation forms and other RTL
  Unicode ranges, replacing unsupported area names with English coordinates.
- Migrates existing scenes to English area and label display.
- English label mode now omits RTL-only fallback text instead of creating
  visually broken Blender text.

## 2.0.3

- Added an explicit choice for oversized maps: resize the selected area or
  split it automatically into aligned generation tiles.
- Enabled the Generate Map action after tiling is selected and clearly reports
  that all tiles will be assembled in one project.
- Made tile sizing quality-aware so High quality uses smaller, safer tiles than
  Medium or Low quality.
- Starts tiled generation when the complete-map estimate exceeds the safety
  budget, even if the raw terrain surface alone appears below the old limit.

## 2.0.2

- Restored English-only area labels in Blender and requested English place
  names from Nominatim to avoid broken Arabic shaping.
- Restored the green confirmation action in the visual map selector.
- Added a clear pre-generation readiness notice for safe, caution, and blocked
  map sizes, with direct advice to reduce area or quality.
- Moved About into a compact header button with the add-on summary and Ammar
  Salim's Instagram link.
- Restored the simplified checkbox-only export panel and larger export button.

## 2.0.1

- Added a conservative preflight check before map generation.
- Large maps now show a clear warning asking the user to reduce the selected
  area or choose a lower quality.
- Disabled generation when the estimated complete map exceeds the configured
  safety budget, before GIS downloads or scene generation begin.
- Included quality-specific headroom for buildings, trees, roofs, and vertical
  detail so the review screen no longer reports a misleading safe estimate.

## 1.8.6-alpha

- Added a dedicated Export Map panel that appears after successful generation.
- Added All Map, Buildings, Roads, Environment, and Custom export scopes.
- Added individual category toggles for buildings, roads, bridges, terrain, water, green areas, and trees.
- Preserved independent material, label-metadata, and transform options.
- Added category-aware automatic filenames and validation for empty selections.
- Clarified that BLEND copies the complete scene while GLB, FBX, OBJ, and USD support filtered category export.

## 1.8.5-alpha

- Preserve mapped building parts in Minecraft and Low Poly instead of dropping towers and multi-part landmarks.
- Preserve direct source heights and mapped floor-derived heights in every visual style.
- Restrict Minecraft block quantization and Low Poly height simplification to inferred heights only.
- Keep building-source content consistent across Real, Architectural, Low Poly, Minecraft, and Classic Voxel styles.

## 1.8.4-alpha

- Added automatic Overpass server failover for HTTP 504 and temporary service outages.
- Replaced stale deletion summaries after failed generation with an explicit guarantee that scene replacement did not start.
- Preserved the existing generated project until downloading, validation, voxelization, and mesh preparation all succeed.

## 1.8.3-alpha

- Added strict real-facade mode: untagged buildings remain neutral and are recorded as `NO_SOURCE_DATA` instead of receiving invented facade detail.
- Extended the building accuracy report with unavailable-facade counts and per-building facade provenance.
- Improved Hybrid conflation so geometry-matched Overture footprints inherit authoritative OSM height, level, roof, material, colour, type, and landmark attributes even when no direct source id is present.
- Restricted Real-style facade relief to buildings with mapped material or colour evidence.

## 1.8.2-alpha

- Expose the Building Data Source selector in wizard step 4 instead of keeping
  the recommended Hybrid source as an invisible internal default.
- Show the selected source in the final review summary before generation.

## 1.8.1-alpha

- Preserve Overture footprint geometry while fusing matching, manually surveyed
  OSM height, level, roof, facade, colour, material, and landmark attributes.
- Record fused provenance explicitly instead of silently discarding the richer
  OSM duplicate.
- Add stable restrained facade-colour variation for realistic buildings that
  have no source colour, reducing repeated city-block appearance without
  pretending the generated colour is surveyed data.
- Make Blender extension registration recover safely from partial failed
  installs and defer scene migration until Blender exposes scene data.

## 1.8.0-alpha

- Reduced the public interface to the original six-step generation workflow.
- Removed the post-generation building editor, correction workspace, and Advanced Options from the public panel.
- Keeps only a compact success/export area inside step 6 after generation.
- Changed the recommended defaults to Real + High + Realistic Materials + Hybrid building data.
- High quality now uses 1.5 m horizontal resolution and 0.5 m vertical resolution, with oversized-area blocking retained.
- Added **Architectural Model** as a fifth presentation style with direct source geometry, mapped building parts, roof shapes, curved landmarks, sand-colored buildings, light roads, blue water, and green parks.
- Added automatic reverse geocoding of the actual selected rectangle center so the area name no longer remains fixed to Al-Adhamiya after moving the selection.
- Automatically derives a project name from the confirmed selected area.
- Removed red alert styling from normal area confirmation; red is reserved for actual errors and unsafe selections.
- Ignores legacy hidden-building correction records during normal six-step generation so older project corrections cannot silently remove real buildings.
- Added one-time migration to the new accurate defaults for saved Blender scenes.
- Added regression coverage for the architectural style, accurate defaults, six-step-only panel, non-error confirmation styling, and reverse-geocoded area naming.

## 1.7.4-alpha

- Restored source-first building heights after the 1.7.3 safety regression.
- Direct `height`, `building:levels`, Overture `num_floors`, and building-part heights are preserved instead of being clamped to generic neighborhood limits.
- Re-enabled mapped building parts for Classic Voxel and Real styles so high-rise massing and stepped landmark details are retained.
- Kept the simplified interface and always-on malformed-footprint rejection that prevents giant slabs.
- Height estimation remains only a fallback when source height and floor data are absent.
- Updated the recommended preset to preserve source geometry while keeping approximate buildings and landmark proxy blocks disabled.


## 1.7.3-alpha

- Simplified Advanced Options to only safe, understandable controls.
- Locked generation to conservative style/quality presets, ignoring stale risky values from older `.blend` files.
- Disabled approximate missing buildings and landmark proxy blocks in normal generation.
- Added always-on building footprint validation to reject malformed, oversized, or off-area polygons before analysis and meshing.
- Added always-on semantic height validation to clamp implausible metadata outliers.
- Restored Classic Voxel recommended defaults to Medium quality, simple materials, no building parts, and mapped buildings only.
- Fixed duplicate feature grouping in the voxel rasterizer.
- Corrected area-load estimation so it follows the selected Quality preset rather than stale manual resolution values.
- Added regression tests for giant footprints, optional proxy filtering, height outliers, and the reduced interface.

## 1.7.2-alpha

- Fixed extension startup and UI failure caused by `OVMG_OT_RestoreRecommendedDefaults` being referenced in the registration tuple without being imported.
- Added a registration integrity test that verifies every class referenced by `_CLASSES` is bound in the registration module.
- Preserved generated maps, selected areas, and building corrections; this is a registration-only corrective release.

## 1.7.1-alpha

- Fixed duplicate nested Building Editor dialogs after viewport picking or placement.
- Added **Reset Editor Defaults** inside the Building Editor.
- Added **Restore Recommended Settings** to the main panel and Advanced Options.
- Recommended reset preserves the selected area, generated map, and manual corrections.
- Approximate Missing Buildings now shows a clear real-world accuracy warning when enabled.

## 1.7.0-alpha

- Replaced the crowded post-generation correction panel with one **Edit Buildings** button.
- Added an on-demand Building Editor dialog with Simple and Advanced modes.
- Added direct viewport building picking; users no longer need to position the 3D Cursor manually.
- Added one-click viewport placement for missing-building presets.
- Added quick edit, replace, delete, restore, duplicate, move, resize, rotate, and snap actions.
- Kept the complete correction toolkit available only in Advanced mode.
- Preserved non-destructive source suppression, persistent replacements, and export support.
- Included the Blender 5.1 tessellation fix from 1.6.2.


## 1.6.2-alpha

- Fixed `Add at 3D Cursor` and editable replacement creation on Blender 5.1.
- Updated polygon tessellation handling for Blender 5.1 index-based results.
- Preserved compatibility with Vector-like tessellation results.
- Removed a duplicate ring-core assignment in editable prism generation.

## 1.6.1-alpha

- Fixed PyArrow native DLL loading in Blender 5.1 portable Windows builds.
- Added an extension-local fallback for Arrow, Arrow Compute, Arrow Dataset, Arrow Acero, Parquet, Arrow Python, Flight, Substrait, and the hashed MSVC runtime DLLs shipped by the official PyArrow wheel.
- Registers PyArrow package and private DLL directories for the full Blender process lifetime.
- Preloads the Arrow/Parquet dependency graph in dependency-safe order after NumPy and before importing PyArrow.
- Added precise PyArrow diagnostics showing package directories, private library directories, selected sources, candidate count, and each preload result.
- Added regression tests for the local PyArrow fallback and dependency-safe load order.
- Keeps NumPy 2.3.5, PyArrow 25.0.0, Shapely 2.1.2, Overture Maps 1.0.1, and the Building Correction Studio unchanged.

## 1.6.0-alpha

- Added a persistent, non-destructive **Building Correction Studio** to the Map Ready workspace.
- Added source-building inspection followed by exact-footprint editable replacements.
- Stores projected outer rings, courtyard holes, source height, roof, facade, part, and parent metadata for correction workflows.
- Added persistent source suppression so replaced or removed generated buildings are omitted on regeneration, including linked building parts.
- Added restoration of original source buildings and automatic removal of linked replacements.
- Added missing-building creation using Box, L-shape, U-shape, Cylinder, and Dome footprints.
- Added exact irregular footprints from selected closed Blender Curve splines.
- Added editable Flat, Gabled, Hipped, Pyramid, Dome, Onion, and Cone roofs.
- Added numeric rebuilding of OVMG footprint buildings so width, depth, height, base elevation, rotation, roof form, roof height, facade profile, and colors are applied to real mesh geometry rather than metadata only.
- Added fallback transform/material editing for imported meshes registered as user buildings.
- Added duplication, ground snapping, selection, bulk deletion, full correction reset, and registration of imported custom meshes.
- Stores correction state in the `.blend` file and keeps corrections separate from optimized chunks, persistent across regeneration, and included in export.
- Added correction-aware source filtering before building analysis, voxelization, direct meshing, curved details, labels, and statistics.
- Added correction registry and editable-building regression coverage; the core suite now contains 40 passing tests.

## 1.5.3-alpha

- Fixed repeated NumPy/Shapely DLL failures after Blender extension remove/reinstall cycles.
- Added extension-local fallback copies of the official NumPy and Shapely private DLL folders, avoiding reliance on Blender's shared wheel cache for native dependencies.
- Imports NumPy and PyArrow before loading Shapely's bundled GEOS chain, preventing the Shapely C++ runtime from disturbing NumPy dependency resolution in embedded Python.
- Registers and retains every local and shared DLL directory for the entire Blender process lifetime.
- Preloads NumPy OpenBLAS and C++ runtime first, then Shapely MSVC, GEOS, and GEOS C API libraries in a second stage.
- Adds diagnostic lines showing the exact native fallback path and the selected NumPy/Shapely DLL sources.
- Keeps NumPy 2.3.5, PyArrow 25.0.0, Shapely 2.1.2, and Overture Maps 1.0.1 unchanged.
- Core suite remains fully passing with 34 tests.

## 1.5.1-alpha

- Fixed Windows native DLL discovery for NumPy, PyArrow, and Shapely inside Blender 5.1 portable and installed builds.
- Retains `os.add_dll_directory` handles for the Blender process lifetime.
- Registers Blender, Python, wheel, and private `.libs` directories before native imports.
- Preloads NumPy's bundled MSVC C++ runtime and OpenBLAS DLLs by absolute path.
- Adds direct diagnostics for `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, and `ucrtbase.dll`.
- Pins NumPy 2.3.5, a stable Python 3.13 Windows build released before Blender 5.1, instead of NumPy 2.5.1.
- Keeps OSM-only fallback available when the optional Overture runtime is unavailable.

## 1.5.0-alpha

- Separated Classic Voxel, Minecraft, Low Poly, and Real at the geometry-pipeline level instead of changing labels only.
- Added direct footprint-preserving building meshes for Low Poly and Real styles.
- Added Minecraft macro-block footprints, stepped 3 m height quantization, flat roofs, no parts, and block-specific materials.
- Added source-first building height resolution with type-aware floor heights and local known-height medians.
- Preserved direct source heights in Real style and added conservative deterministic inference for missing values.
- Added building footprint, height, roof, facade, and dataset provenance with confidence scoring.
- Added optional confidence overlay meshes and a 3D-Cursor-based Building Inspector.
- Added source-aware facade colors/material profiles and procedural brick, stone, plaster, concrete, and glass responses.
- Added separate source-aware roof colors and roof geometry for direct building meshes.
- Fixed the visual map selector warning caused by an undefined `stats` variable.
- Enforced style-owned building-part and roof rules after Quality presets are applied.
- Added five new building/style regression tests; the core suite now contains 32 passing tests.

## 1.4.3-alpha

- Prevented Hybrid mode from aborting when the optional Overture runtime cannot load.
- Added automatic OpenStreetMap-only fallback with a visible generation warning.
- Added exact NumPy/PyArrow/Shapely/overturemaps runtime diagnostics.
- Added a safe recovery scan for Blender extension wheel site-packages paths.
- Added Review-step actions to use OSM only or copy diagnostics.

## 1.4.2-alpha

- Fixed the Materials wizard step failing with `NameError: name 'cls' is not defined`.
- Added a source-level guard that verifies static UI helpers do not reference an undefined `cls`.
- No changes were made to map generation, GIS sources, or previously confirmed area data.

## 1.4.1-alpha

- Replaced the long settings panel with a six-step guided workflow: Area, Confirm, Style, Quality, Materials, and Review.
- Reduced area input to two user-facing choices: draw a new rectangle on the visual map or reuse a previously confirmed rectangle.
- Added automatic persistent history for prior visual selections in add-on preferences.
- Added four high-level city styles: Classic Voxel, Minecraft Style, Low Poly, and Real.
- Added Simple and Realistic material modes; Realistic uses self-contained procedural PBR color, roughness, bump, and water response.
- Kept technical controls and optional map labels under Advanced Options.
- Added a Map Ready workspace with editing, export, save, regeneration, and new-map actions.
- Added persistent user-building tools: create box/cylinder/dome buildings at the 3D cursor, register selected custom meshes, select them, and delete them.
- User buildings remain separate from generated chunks, survive regeneration, and are included in project export.
- Added history and style regression tests, bringing the core test total to 24.

## 1.3.2-alpha

- Fixed a native Blender crash that could occur immediately after the browser sent a map thumbnail.
- Removed automatic popup invocation from the application-timer callback.
- The timer now performs plain state updates and stages the JPEG/PNG preview as a temporary file only.
- The map preview is loaded through Blender custom previews only after the user explicitly presses `Review and Confirm` from the sidebar.
- Removed legacy `Image Texture` datablocks and `template_preview` usage from the asynchronous selection path.
- Added a safe external-image fallback when Blender cannot create a custom preview icon.
- Added broader exception containment so a malformed preview cannot terminate the timer or disable the extension.
- Preserved the two-step confirmation workflow, draggable resize handles, and unchanged active bounds until confirmation.

## 1.3.1-alpha

- Added an explicit two-step visual selection workflow: browser proposal, then Blender confirmation.
- Added a browser-generated map thumbnail to the Blender confirmation dialog and persistent sidebar card.
- Active geographic bounds remain unchanged until the user presses `Confirm Selection`.
- Added visible draggable corner, side, and center handles for resizing and moving the rectangle.
- Added `Back to Map` and `Discard Proposal` controls while a selection is pending.
- Kept the localhost selector session alive until confirmation so the rectangle can be adjusted and resent.
- Disabled generation while an unconfirmed proposal is pending.
- Expanded selector payload validation to safely accept bounded JPEG/PNG previews.

## 1.3.0-alpha

- Added a browser-based Visual Map input mode.
- Added an exact editable rectangle selector built with Leaflet and Leaflet.draw.
- Added place/address search through the configured Nominatim service.
- Added live width, height, area, surface-cell, and load estimates before generation.
- Added a secure tokenized localhost bridge that transfers selected bounds back to Blender.
- Added a temporary non-exportable Blender outline and north indicator for the selected area.
- Added Frame Preview and Clear controls.
- Generation is disabled until a visual rectangle has been selected and when its preflight estimate is too large.
- The visual selector remains optional; Search Area and Manual Bounding Box workflows are preserved.
- Added optional chunk-aligned subdivision for oversized rectangles, assembled into one correctly aligned Blender project.

## 1.2.1-alpha

- Rebalanced High quality from 1.5 m / 0.5 m to 2.25 m / 1.0 m so a typical district-sized map is less likely to exceed the default 12 million-cell safety budget.
- Added temporary export-safe Principled BSDF materials for GLB, FBX, OBJ, and USD.
- Preserved category colors, roughness, metallic response, and water coat settings during interchange export.
- Restored the original enhanced Blender materials immediately after export.
- Added GLB post-export validation to confirm that material definitions and primitive assignments are actually present.
- Clarified that exact procedural Noise/Color Ramp appearance requires texture baking and is not embedded directly in GLB.

## 1.2.0-alpha

- Reorganized the main interface around Quality, Enhanced Materials, Geometry
  Style, optional Map Labels, and a simplified Export section.
- Added Low, Medium, and High generation presets with automatic advanced values.
- Added Custom state when preset-controlled values are manually edited.
- Added optional procedural enhanced materials without external image textures.
- Added optional curved landmark geometry for domes, minarets, towers, and
  water towers, with segment and object safety limits.
- Added optional street, area, and landmark label extraction.
- Added lightweight multilingual label metadata and optional Blender Text output.
- Added Unity-ready `.labels.json` sidecar export.
- Added GLB, FBX, OBJ, USD, and BLEND export from the sidebar.
- Added material-free export while restoring the original Blender material slots.
- Added Blender-version-safe operator argument filtering through RNA inspection.
- Added curved-detail and street-label regression tests, raising the total to 16.
- Verified registration, scene generation, enhanced materials, label storage,
  cleanup, and real GLB/FBX/OBJ/USD export with Blender `bpy 5.1.2`.

## 1.1.0-alpha

- Added Hybrid, Overture-only, and OSM-only building-source modes.
- Added Overture `building` and `building_part` streaming by bounding box.
- Added source-ID and geometry-overlap de-duplication between Overture and OSM.
- Split horizontal voxel resolution from the vertical height step.
- Added height, floor-count, building-part, minimum-height, and roof processing.
- Disabled procedural approximate missing buildings by default.

## 1.0.3-alpha

- Added stronger road recess, major-river minimum width, optional residential
  recovery, and expanded landmark retrieval.

## 1.0.2-alpha

- Added thin semantic surfaces, landmark proxies, building parts, deterministic
  missing-height inference, and tolerant relation-ring assembly.

## 1.0.1-alpha

- Added multi-candidate area-name resolution and practical point-result extent.

## 1.0.0-alpha

- Added Blender extension packaging, OSM ingestion, sparse voxels, chunking,
  greedy meshing, organized scene output, metadata, and deletion.
