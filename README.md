

# Open Voxel Map Generator — OVMG 1.8.6-alpha

A Blender 5.1 Windows x64 extension that converts a precisely selected geographic rectangle into an optimized 3D city using OpenStreetMap and optional Overture building data.

## Six-step workflow

The public interface is intentionally limited to six steps:

1. Choose a rectangle on the interactive map or reuse a previous selection.
2. Review the thumbnail, dimensions, actual detected place name, and load estimate.
3. Choose the 3D style.
4. Choose Low, Medium, or High quality.
5. Choose Simple or Realistic materials.
6. Review, generate, then export or start a new map.

No building editor or technical Advanced Options are shown in the public panel.

## Accurate defaults

New and migrated scenes start with:

- **Style:** Real
- **Quality:** High
- **Materials:** Realistic
- **Building source:** Hybrid OSM + Overture
- **Horizontal resolution:** 1.5 m
- **Vertical resolution:** 0.5 m
- Source heights, floor counts, mapped building parts, roof shapes, facade hints, and curved landmarks enabled.
- Approximate missing buildings and landmark proxy blocks disabled.
- Malformed-footprint validation enabled.

If a selected area exceeds the safe budget, generation is blocked before meshing and the user can choose a smaller area or Medium quality.

## Styles

- **Classic Voxel:** accurate map footprints converted to optimized voxel chunks.
- **Minecraft Style:** coarse blocks, stepped heights, flat roofs, no curves.
- **Low Poly:** simplified direct polygon geometry and faceted landmarks.
- **Real:** direct source footprints, source heights/floors, mapped parts, roofs, curved details, and source-aware facade profiles.
- **Architectural Model:** the same source-accurate direct geometry presented as a clean urban model with sand buildings, light roads, blue water, and green parks.

## Area naming

The browser selector reverse-geocodes the center of the rectangle when the preview is sent. Moving the rectangle therefore updates the confirmed area name instead of retaining the initial search hint such as Al-Adhamiya.

## Button colors

Normal confirmation and generation actions use Blender's regular theme styling. Red alert styling is reserved for actual errors, unavailable required runtimes, or unsafe oversized selections.

## Installation

Install `Open_Voxel_Map_Generator_v1.8.6-alpha_WINDOWS_INSTALL.zip` from **Edit > Preferences > Get Extensions > Install from Disk**, restart Blender, enable Online Access, and open the **Voxel Maps** tab.

## Creator

Created by **Ammar Salim**
- Instagram: [@ammar_salim_d](https://www.instagram.com/ammar_salim_d/)
- ## Demo Video

[![Watch OVMG in action](docs/images/demo-thumbnail.jpg)](https://www.instagram.com/p/DaySfxKOfU8/)

- X / Twitter: [@surrealism19](https://x.com/surrealism19)
- 


