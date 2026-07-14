"""Runtime discovery and diagnostics for the optional Overture dependency stack.

Blender installs extension wheels into a shared extension ``site-packages``
location. On Windows, native packages such as NumPy, PyArrow, and Shapely
also need private DLL search directories to remain registered for the whole
process lifetime.

OVMG additionally carries a small extension-local copy of the private NumPy,
PyArrow, and Shapely DLL folders. This is intentional: Blender's shared
extension wheel cache may retain the Python package while losing a sibling
``*.libs`` folder, or may expose a package directory before Windows can resolve
its native dependency chain. The local native fallback keeps the binary stack
self-contained without replacing the Python modules installed from the
official wheels.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import importlib
import importlib.metadata
import os
from pathlib import Path
import site
import sys
import traceback
from typing import Any, Iterable


# ``os.add_dll_directory`` returns a handle whose lifetime controls whether the
# directory remains registered. Keeping these objects globally prevents the
# directory from being removed immediately by CPython reference counting.
_DLL_DIRECTORY_HANDLES: list[Any] = []
_PRELOADED_DLL_HANDLES: list[Any] = []
_REGISTERED_DLL_PATHS: set[str] = set()
_PRELOADED_DLL_PATHS: set[str] = set()


@dataclass(frozen=True, slots=True)
class OvertureRuntimeStatus:
    """Result of probing the packaged Overture Python dependency stack."""

    available: bool
    summary: str
    diagnostics: str
    overturemaps: Any | None = None
    shapely: Any | None = None


def _module_version(distribution_name: str, module: Any) -> str:
    """Return a useful package version without assuming ``__version__`` exists."""
    version = getattr(module, "__version__", "")
    if version:
        return str(version)
    try:
        return importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _addon_root() -> Path:
    """Return the installed extension root directory."""
    # runtime.py -> overture -> infrastructure -> add-on root
    return Path(__file__).resolve().parents[2]


def _local_native_root() -> Path:
    """Return the extension-local Windows native dependency fallback."""
    return _addon_root() / "native_windows"


def _candidate_site_packages() -> tuple[Path, ...]:
    """Return likely Blender extension wheel locations, nearest first."""
    python_tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    module_path = Path(__file__).resolve()
    candidates: list[Path] = []
    for parent in module_path.parents:
        candidates.extend(
            (
                parent / ".local" / "lib" / python_tag / "site-packages",
                parent / "lib" / python_tag / "site-packages",
                parent / "site-packages",
            )
        )

    # Blender's bundled site-packages remains a valid fallback and may contain
    # a NumPy build matched to Blender's embedded Python runtime.
    for prefix in (Path(sys.prefix), Path(sys.base_prefix)):
        candidates.extend(
            (
                prefix / "Lib" / "site-packages",
                prefix / "lib" / python_tag / "site-packages",
            )
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(candidate)
    return tuple(unique)


def _recover_extension_site_packages() -> tuple[str, ...]:
    """Add discovered Blender extension wheel directories to ``sys.path``.

    This does not install or modify packages. It only exposes directories that
    Blender has already created while installing the extension.
    """
    added: list[str] = []
    for candidate in _candidate_site_packages():
        if not candidate.is_dir():
            continue
        if not any(
            (candidate / name).exists()
            for name in ("numpy", "overturemaps", "pyarrow", "shapely")
        ):
            continue
        candidate_text = str(candidate)
        if candidate_text not in sys.path:
            site.addsitedir(candidate_text)
            added.append(candidate_text)
    if added:
        importlib.invalidate_caches()
    return tuple(added)


def _existing_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Return unique existing directories while preserving their order."""
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        normalized = os.path.normcase(str(resolved))
        if normalized in seen or not resolved.is_dir():
            continue
        seen.add(normalized)
        result.append(resolved)
    return tuple(result)


def _native_library_directories() -> tuple[Path, ...]:
    """Return native wheel and Blender DLL directories in priority order."""
    local_native = _local_native_root()
    candidates: list[Path] = [
        local_native / "numpy.libs",
        local_native / "pyarrow",
        local_native / "pyarrow.libs",
        local_native / "shapely.libs",
    ]
    for site_packages in _candidate_site_packages():
        if not site_packages.is_dir():
            continue
        candidates.extend(
            (
                site_packages,
                site_packages / "numpy.libs",
                site_packages / "pyarrow",
                site_packages / "pyarrow.libs",
                site_packages / "shapely.libs",
            )
        )

    executable = Path(sys.executable).resolve()
    candidates.append(executable.parent)
    # For Blender portable builds this normally includes:
    # .../5.1/python/bin, .../5.1/python, .../5.1, and the Blender root.
    candidates.extend(executable.parents[:4])
    candidates.extend((Path(sys.prefix), Path(sys.base_prefix)))
    return _existing_paths(candidates)


def _register_windows_dll_directory(path: Path) -> str | None:
    """Register one native library directory and retain its lifetime handle."""
    normalized = os.path.normcase(str(path))
    if normalized in _REGISTERED_DLL_PATHS:
        return None
    try:
        handle = os.add_dll_directory(str(path))
    except (AttributeError, FileNotFoundError, OSError) as exc:
        return f"DLL directory registration failed: {path} — {type(exc).__name__}: {exc}"
    _DLL_DIRECTORY_HANDLES.append(handle)
    _REGISTERED_DLL_PATHS.add(normalized)
    return f"DLL directory registered: {path}"


def _preload_windows_dll(path: Path) -> str:
    """Load one DLL by absolute path and retain the native module handle."""
    import ctypes

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    normalized = os.path.normcase(str(resolved))
    if normalized in _PRELOADED_DLL_PATHS:
        return f"DLL already loaded: {path.name}"
    try:
        handle = ctypes.WinDLL(str(resolved))
    except OSError as exc:
        return f"DLL preload failed: {path.name} — {exc}"
    _PRELOADED_DLL_HANDLES.append(handle)
    _PRELOADED_DLL_PATHS.add(normalized)
    return f"DLL preloaded: {path.name}"


def _shapely_private_dlls(directory: Path) -> tuple[Path, ...]:
    """Return Shapely private DLLs in dependency-safe load order."""
    ordered_patterns = (
        "msvcp140-*.dll",
        "geos-[0-9a-f]*.dll",
        "geos_c-*.dll",
    )
    ordered: list[Path] = []
    seen: set[str] = set()
    for pattern in ordered_patterns:
        for dll_path in sorted(directory.glob(pattern)):
            normalized = os.path.normcase(str(dll_path.resolve()))
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(dll_path)

    for dll_path in sorted(directory.glob("*.dll")):
        normalized = os.path.normcase(str(dll_path.resolve()))
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(dll_path)
    return tuple(ordered)


def _pyarrow_private_dlls(
    package_directory: Path,
    libs_directory: Path,
) -> tuple[Path, ...]:
    """Return PyArrow DLLs in dependency-safe load order.

    The Windows wheel stores Arrow's C++ libraries beside the Python package
    while hashed MSVC dependencies live in ``pyarrow.libs``. Importing
    ``pyarrow.lib`` first asks Windows to resolve ``arrow_python.dll`` and
    ``arrow.dll``; on embedded hosts this can fail even when both directories
    are present. Preloading the dependency graph by absolute path avoids that
    ambiguity.
    """
    ordered_patterns: tuple[tuple[Path, str], ...] = (
        (libs_directory, "msvcp140-*.dll"),
        (libs_directory, "msvcp140_atomic_wait-*.dll"),
        (package_directory, "arrow.dll"),
        (package_directory, "arrow_compute.dll"),
        (package_directory, "parquet.dll"),
        (package_directory, "arrow_acero.dll"),
        (package_directory, "arrow_dataset.dll"),
        (package_directory, "arrow_flight.dll"),
        (package_directory, "arrow_substrait.dll"),
        (package_directory, "arrow_python.dll"),
        (package_directory, "arrow_python_flight.dll"),
        (package_directory, "arrow_python_parquet_encryption.dll"),
    )
    ordered: list[Path] = []
    seen: set[str] = set()
    for directory, pattern in ordered_patterns:
        for dll_path in sorted(directory.glob(pattern)):
            normalized = os.path.normcase(str(dll_path.resolve()))
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(dll_path)

    for directory in (libs_directory, package_directory):
        for dll_path in sorted(directory.glob("*.dll")):
            normalized = os.path.normcase(str(dll_path.resolve()))
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(dll_path)
    return tuple(ordered)


def _probe_windows_runtime_dll(name: str) -> str:
    """Test whether a Microsoft runtime DLL is resolvable by the process."""
    import ctypes

    try:
        handle = ctypes.WinDLL(name)
    except OSError as exc:
        return f"Runtime DLL missing: {name} — {exc}"
    _PRELOADED_DLL_HANDLES.append(handle)
    return f"Runtime DLL OK: {name}"


def _matching_directories(name: str) -> tuple[Path, ...]:
    """Return registered native directories matching a basename."""
    folded = name.casefold()
    return tuple(
        directory
        for directory in _native_library_directories()
        if directory.name.casefold() == folded
    )


def _prepare_windows_base_runtime() -> tuple[str, ...]:
    """Prepare DLL search paths and NumPy dependencies before imports.

    Shapely is deliberately not preloaded here. Loading its bundled C++ runtime
    before NumPy can disturb resolution of NumPy's independently hashed native
    dependencies in embedded Python hosts. NumPy and PyArrow are imported first;
    Shapely's GEOS chain is prepared in a second stage.
    """
    if sys.platform != "win32":
        return ()

    diagnostics: list[str] = []
    directories = _native_library_directories()

    existing_path = os.environ.get("PATH", "")
    existing_entries = {
        os.path.normcase(entry)
        for entry in existing_path.split(os.pathsep)
        if entry
    }
    prepend: list[str] = []
    for directory in directories:
        message = _register_windows_dll_directory(directory)
        if message:
            diagnostics.append(message)
        text = str(directory)
        if os.path.normcase(text) not in existing_entries:
            prepend.append(text)
    if prepend:
        os.environ["PATH"] = os.pathsep.join((*prepend, existing_path))
        diagnostics.append(f"PATH fallback prepended {len(prepend)} directories.")

    for runtime_name in (
        "VCRUNTIME140.dll",
        "VCRUNTIME140_1.dll",
        "ucrtbase.dll",
    ):
        diagnostics.append(_probe_windows_runtime_dll(runtime_name))

    numpy_directories = _matching_directories("numpy.libs")
    diagnostics.append(f"NumPy private DLL directories: {len(numpy_directories)}")
    for directory in numpy_directories:
        diagnostics.append(f"NumPy DLL source: {directory}")
        for pattern in ("msvcp140-*.dll", "libscipy_openblas*.dll"):
            for dll_path in sorted(directory.glob(pattern)):
                diagnostics.append(_preload_windows_dll(dll_path))

    return tuple(diagnostics)


def _prepare_pyarrow_native_runtime() -> tuple[str, ...]:
    """Prepare PyArrow's Arrow/Parquet DLL chain before importing PyArrow."""
    if sys.platform != "win32":
        return ()

    diagnostics: list[str] = []
    package_directories = _matching_directories("pyarrow")
    libs_directories = _matching_directories("pyarrow.libs")
    diagnostics.append(
        f"PyArrow package DLL directories: {len(package_directories)}"
    )
    diagnostics.append(f"PyArrow private DLL directories: {len(libs_directories)}")

    package_by_parent = {
        os.path.normcase(str(path.parent.resolve())): path
        for path in package_directories
    }
    libs_by_parent = {
        os.path.normcase(str(path.parent.resolve())): path
        for path in libs_directories
    }
    parent_keys = tuple(dict.fromkeys((*package_by_parent, *libs_by_parent)))

    for parent_key in parent_keys:
        package_directory = package_by_parent.get(parent_key)
        libs_directory = libs_by_parent.get(parent_key)
        if package_directory is None or libs_directory is None:
            continue
        diagnostics.append(f"PyArrow DLL source: {package_directory}")
        diagnostics.append(f"PyArrow libs source: {libs_directory}")
        pyarrow_dlls = _pyarrow_private_dlls(package_directory, libs_directory)
        diagnostics.append(f"PyArrow private DLL candidates: {len(pyarrow_dlls)}")
        diagnostics.extend(_preload_windows_dll(path) for path in pyarrow_dlls)

    return tuple(diagnostics)


def _prepare_shapely_native_runtime() -> tuple[str, ...]:
    """Prepare Shapely's GEOS chain after NumPy and PyArrow are imported."""
    if sys.platform != "win32":
        return ()

    diagnostics: list[str] = []
    directories = _matching_directories("shapely.libs")
    diagnostics.append(f"Shapely private DLL directories: {len(directories)}")
    for directory in directories:
        diagnostics.append(f"Shapely DLL source: {directory}")
        shapely_dlls = _shapely_private_dlls(directory)
        diagnostics.append(f"Shapely private DLL candidates: {len(shapely_dlls)}")
        diagnostics.extend(_preload_windows_dll(path) for path in shapely_dlls)
    return tuple(diagnostics)


def _format_environment(
    added_paths: tuple[str, ...],
    native_diagnostics: tuple[str, ...],
) -> list[str]:
    """Build a compact, copyable environment report."""
    lines = [
        f"Python: {sys.version}",
        f"Executable: {sys.executable}",
        f"Prefix: {sys.prefix}",
        f"Platform: {sys.platform}",
        f"Extension module: {Path(__file__).resolve()}",
        f"Local native fallback: {_local_native_root()}",
    ]
    if added_paths:
        lines.append("Recovered site-packages paths:")
        lines.extend(f"  - {path}" for path in added_paths)
    if native_diagnostics:
        lines.append("Windows native runtime bootstrap:")
        lines.extend(f"  - {message}" for message in native_diagnostics)
    return lines


def _purge_partial_import(module_name: str) -> None:
    """Remove a failed module and its children before a controlled retry."""
    prefixes = (module_name, f"{module_name}.")
    for loaded_name in tuple(sys.modules):
        if loaded_name == prefixes[0] or loaded_name.startswith(prefixes[1]):
            sys.modules.pop(loaded_name, None)


def _import_required_module(module_name: str) -> Any:
    """Import one dependency and retry once after clearing partial state."""
    try:
        return importlib.import_module(module_name)
    except Exception:
        _purge_partial_import(module_name.split(".", maxsplit=1)[0])
        importlib.invalidate_caches()
        return importlib.import_module(module_name)


def _append_success(
    diagnostics: list[str],
    module_name: str,
    distribution_name: str,
    module: Any,
) -> None:
    """Append one successful dependency probe line."""
    module_file = getattr(module, "__file__", "built-in")
    diagnostics.append(
        f"OK {module_name} ({_module_version(distribution_name, module)}): {module_file}"
    )


def _failure_status(
    diagnostics: list[str],
    module_name: str,
    exc: Exception,
) -> OvertureRuntimeStatus:
    """Return a detailed failed probe result."""
    short_detail = f"{type(exc).__name__}: {exc}"
    diagnostics.append(f"FAILED {module_name}: {short_detail}")
    diagnostics.append(traceback.format_exc())
    if module_name == "numpy" and sys.platform == "win32":
        diagnostics.append(
            "NumPy private DLLs should be available from the extension-local "
            "native_windows/numpy.libs fallback. If the diagnostics show zero "
            "NumPy DLL directories, reinstall the complete OVMG package."
        )
    if module_name == "pyarrow" and sys.platform == "win32":
        diagnostics.append(
            "PyArrow's Arrow/Parquet DLLs should be available from the "
            "extension-local native_windows/pyarrow and pyarrow.libs fallback. "
            "If the diagnostics show zero PyArrow DLL directories, reinstall "
            "the complete OVMG Windows package."
        )
    return OvertureRuntimeStatus(
        available=False,
        summary=f"{module_name} could not load — {short_detail}",
        diagnostics="\n".join(diagnostics),
    )


def _probe_uncached() -> OvertureRuntimeStatus:
    """Import every required module in dependency-safe stages."""
    added_paths = _recover_extension_site_packages()
    base_native = _prepare_windows_base_runtime()
    diagnostics = _format_environment(added_paths, base_native)
    modules: dict[str, Any] = {}

    # Stage 1: import NumPy before any other wheel-specific native runtime.
    try:
        module = _import_required_module("numpy")
        modules["numpy"] = module
        _append_success(diagnostics, "numpy", "numpy", module)
    except Exception as exc:
        return _failure_status(diagnostics, "numpy", exc)

    # Stage 2: preload Arrow/Parquet C++ dependencies, then import PyArrow.
    pyarrow_native = _prepare_pyarrow_native_runtime()
    if pyarrow_native:
        diagnostics.append("Windows PyArrow runtime bootstrap:")
        diagnostics.extend(f"  - {message}" for message in pyarrow_native)
    try:
        module = _import_required_module("pyarrow")
        modules["pyarrow"] = module
        _append_success(diagnostics, "pyarrow", "pyarrow", module)
    except Exception as exc:
        return _failure_status(diagnostics, "pyarrow", exc)

    # Stage 3: now that NumPy and PyArrow are stable, load Shapely's GEOS chain.
    shapely_native = _prepare_shapely_native_runtime()
    if shapely_native:
        diagnostics.append("Windows Shapely runtime bootstrap:")
        diagnostics.extend(f"  - {message}" for message in shapely_native)

    for module_name, distribution_name in (
        ("shapely", "shapely"),
        ("orjson", "orjson"),
        ("overturemaps", "overturemaps"),
        ("overturemaps.core", "overturemaps"),
    ):
        try:
            module = _import_required_module(module_name)
            modules[module_name] = module
            _append_success(diagnostics, module_name, distribution_name, module)
        except Exception as exc:
            return _failure_status(diagnostics, module_name, exc)

    return OvertureRuntimeStatus(
        available=True,
        summary="Overture runtime is ready.",
        diagnostics="\n".join(diagnostics),
        overturemaps=modules["overturemaps"],
        shapely=modules["shapely"],
    )


@lru_cache(maxsize=1)
def probe_overture_runtime() -> OvertureRuntimeStatus:
    """Return a cached dependency probe suitable for Blender panel drawing."""
    return _probe_uncached()


def refresh_overture_runtime_probe() -> OvertureRuntimeStatus:
    """Clear cached imports and run the probe again."""
    probe_overture_runtime.cache_clear()
    return probe_overture_runtime()
