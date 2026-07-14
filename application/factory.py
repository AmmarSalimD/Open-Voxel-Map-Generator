"""Composition root for application services."""

from __future__ import annotations

import bpy

from ..core.constants import DEFAULT_USER_AGENT
from ..domain.enums import BuildingSource
from ..domain.models import VoxelSettings
from ..infrastructure.blender.scene_repository import BlenderSceneRepository
from ..infrastructure.geocoding.nominatim import NominatimGeocoder
from ..infrastructure.network.http_client import JsonHttpClient
from ..infrastructure.osm.classifier import OsmFeatureClassifier
from ..infrastructure.osm.overpass import OverpassDataSource
from ..infrastructure.overture.buildings import OvertureBuildingsDataSource
from ..infrastructure.overture.hybrid import HybridGeographicDataSource
from ..voxel.curved_details import CurvedDetailBuilder
from ..voxel.labels import MapLabelBuilder
from ..voxel.mesh_builder import GreedyChunkMesher
from ..voxel.rasterizer import FeatureRasterizer
from ..voxel.style_profiles import style_profile
from .generation_service import MapGenerationService


class ApplicationFactory:
    """Build fully configured use-case services from Blender preferences."""

    @staticmethod
    def create_generation_service(
        scene: bpy.types.Scene,
        preferences: object,
        voxel_settings: VoxelSettings,
    ) -> MapGenerationService:
        """Create a generation service using configured GIS endpoints."""
        user_agent = getattr(preferences, "user_agent", DEFAULT_USER_AGENT).strip()
        timeout_seconds = int(getattr(preferences, "network_timeout", 180))
        retry_count = int(getattr(preferences, "network_retries", 2))
        client = JsonHttpClient(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            timeout_seconds=timeout_seconds,
            retry_count=retry_count,
        )
        geocoder = NominatimGeocoder(
            endpoint=str(preferences.nominatim_endpoint),
            http_client=client,
        )
        osm_source = OverpassDataSource(
            endpoint=str(preferences.overpass_endpoint),
            http_client=client,
            classifier=OsmFeatureClassifier(),
            timeout_seconds=timeout_seconds,
            fallback_endpoints=(
                "https://overpass.kumi.systems/api/interpreter",
                "https://overpass.private.coffee/api/interpreter",
            ),
        )
        profile = style_profile(voxel_settings.model_style)
        effective_parts = (
            voxel_settings.use_building_parts and profile.allow_building_parts
        )
        overture_source = None
        if voxel_settings.building_source is not BuildingSource.OSM_ONLY:
            overture_source = OvertureBuildingsDataSource(
                include_parts=effective_parts,
                release=str(getattr(preferences, "overture_release", "")),
                connect_timeout=min(60, timeout_seconds),
                request_timeout=timeout_seconds,
                max_features=int(
                    getattr(preferences, "max_overture_features", 250_000)
                ),
            )
        data_source = HybridGeographicDataSource(
            osm_source=osm_source,
            overture_source=overture_source,
            building_source=voxel_settings.building_source,
            use_building_parts=effective_parts,
        )
        return MapGenerationService(
            geocoder=geocoder,
            data_source=data_source,
            rasterizer=FeatureRasterizer(),
            mesher=GreedyChunkMesher(),
            curved_detail_builder=CurvedDetailBuilder(),
            label_builder=MapLabelBuilder(),
            scene_repository=BlenderSceneRepository(scene),
        )
