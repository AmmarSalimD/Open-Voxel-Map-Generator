# Third-Party Notices

This extension can download and transform OpenStreetMap and Overture Maps data.
Generated project metadata stores the required source attribution strings.

The Windows package bundles unmodified Python wheels downloaded from the Python
Package Index for:

- overturemaps
- click
- colorama
- numpy
- orjson
- pyarrow
- pyfiglet
- shapely
- tqdm

Each wheel contains its own package metadata and applicable license files.
The OVMG source code itself is licensed under GPL-3.0-or-later.
The optional Visual Map selector loads these pinned browser libraries at runtime
from UNPKG; they are not bundled inside the extension archive:

- Leaflet 1.9.4 — BSD-2-Clause
- Leaflet.draw 1.0.4 — MIT

The selector uses the standard OpenStreetMap raster tile endpoint for interactive
manual selection only and displays the required OpenStreetMap attribution.

