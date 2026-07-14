"""Overpass QL query construction."""

from __future__ import annotations

from ...domain.models import BoundingBox


class OverpassQueryBuilder:
    """Build a bounded query for all feature families used by OVMG alpha."""

    @staticmethod
    def build(bounds: BoundingBox, timeout_seconds: int = 180) -> str:
        """Return Overpass QL using geometry output for standalone parsing."""
        bbox = bounds.as_overpass()
        selectors = (
            'node["building"]',
            'way["building"]',
            'relation["building"]',
            'way["building:part"]',
            'relation["building:part"]',
            'relation["type"="building"]',
            'node["amenity"="place_of_worship"]',
            'way["amenity"="place_of_worship"]',
            'relation["amenity"="place_of_worship"]',
            (
                'node["amenity"~'
                '"^(school|college|university|hospital|clinic|police|'
                "fire_station|townhall|courthouse|library|community_centre|"
                'marketplace)$"]'
            ),
            (
                'way["amenity"~'
                '"^(school|college|university|hospital|clinic|police|'
                "fire_station|townhall|courthouse|library|community_centre|"
                'marketplace)$"]'
            ),
            (
                'relation["amenity"~'
                '"^(school|college|university|hospital|clinic|police|'
                "fire_station|townhall|courthouse|library|community_centre|"
                'marketplace)$"]'
            ),
            'node["tourism"~"^(museum|hotel|attraction)$"]',
            'way["tourism"~"^(museum|hotel|attraction)$"]',
            'relation["tourism"~"^(museum|hotel|attraction)$"]',
            'node["office"="government"]',
            'way["office"="government"]',
            'relation["office"="government"]',
            'node["man_made"~"^(tower|minaret|water_tower)$"]',
            'way["man_made"~"^(tower|minaret|water_tower)$"]',
            'relation["man_made"~"^(tower|minaret|water_tower)$"]',
            'node["historic"~"^(monument|memorial|castle|fort|tower|ruins)$"]',
            'way["historic"~"^(monument|memorial|castle|fort|tower|ruins)$"]',
            ('relation["historic"~"^(monument|memorial|castle|fort|tower|ruins)$"]'),
            'way["highway"]',
            'relation["highway"]',
            'way["natural"="water"]',
            'relation["natural"="water"]',
            'way["water"="river"]',
            'relation["water"="river"]',
            'way["waterway"="riverbank"]',
            'relation["waterway"="riverbank"]',
            'way["waterway"]',
            'relation["waterway"]',
            'way["landuse"~"^(reservoir|basin)$"]',
            'relation["landuse"~"^(reservoir|basin)$"]',
            'way["leisure"~"^(park|garden|recreation_ground)$"]',
            'relation["leisure"~"^(park|garden|recreation_ground)$"]',
            (
                'way["landuse"~'
                '"^(grass|forest|meadow|recreation_ground|village_green)$"]'
            ),
            (
                'relation["landuse"~'
                '"^(grass|forest|meadow|recreation_ground|village_green)$"]'
            ),
            'way["natural"~"^(wood|scrub|grassland)$"]',
            'relation["natural"~"^(wood|scrub|grassland)$"]',
            'way["landuse"~"^(residential|commercial|retail|mixed_use)$"]',
            ('relation["landuse"~"^(residential|commercial|retail|mixed_use)$"]'),
            'way["man_made"="bridge"]',
            'relation["man_made"="bridge"]',
            'node["natural"="tree"]',
        )
        body = "\n".join(f"  {selector}({bbox});" for selector in selectors)
        return (
            f"[out:json][timeout:{int(timeout_seconds)}];\n"
            "(\n"
            f"{body}\n"
            ");\n"
            "out tags geom qt;"
        )
