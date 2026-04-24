# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

"""ENVI hyperspectral (BSQ / BIL / BIP) parsing and RGB compositing.

The reader in ``media_extractors.py`` relies on this module to: locate an HDR
sidecar (plain-file or inside a ``.zip`` bundle), parse it into a typed record,
and render an 8-bit RGBA composite from any 3 chosen bands with per-band
percentile stretch. Nodata pixels (``data ignore value`` from the HDR, or NaN)
render transparent.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import numpy as np
from PIL import Image as PILImage
from spectral.io import envi

# ENVI 'data type' field → NumPy dtype string. Full table per the ENVI spec.
ENVI_DTYPE_MAP: dict[int, str] = {
    1: "uint8",
    2: "int16",
    3: "int32",
    4: "float32",
    5: "float64",
    12: "uint16",
    13: "uint32",
    14: "int64",
    15: "uint64",
}

# File suffixes recognized as cube data (paired with a .hdr). Lowercase.
CUBE_SUFFIXES: tuple[str, ...] = (".bsq", ".img", ".bil", ".bip")

# Target wavelengths (nm) for RGB fallback when the HDR lacks `default bands`
# but does provide a `wavelength` array.
_FALLBACK_RGB_WAVELENGTHS_NM = (650.0, 550.0, 450.0)

# Default percentile stretch when the HDR lacks `default stretch`.
DEFAULT_STRETCH_PERCENTILES: tuple[float, float] = (2.0, 98.0)


@dataclass(frozen=True)
class HyperspectralHeaderInfo:
    """Normalized view of an ENVI header, ready to persist or drive rendering."""

    band_count: int
    lines: int
    samples: int
    interleave: str  # 'bsq' | 'bil' | 'bip'
    dtype: str  # numpy dtype string (e.g. 'float32')
    default_r_band: int  # 0-based band indices
    default_g_band: int
    default_b_band: int
    default_stretch: list[str] | None
    wavelengths: list[float] | None
    data_ignore_value: float | None
    crs_wkt: str | None
    map_info: list[str] | None


class HyperspectralError(ValueError):
    """Raised for malformed or unpairable hyperspectral inputs."""


def find_hdr_sibling(data_path: str | os.PathLike) -> Path | None:
    """Return the sidecar ``.hdr`` next to *data_path*, or ``None`` if absent.

    Looks for ``<stem>.hdr`` in the same directory, case-insensitive.
    """
    p = Path(data_path)
    candidate = p.with_suffix(".hdr")
    if candidate.is_file():
        return candidate
    # Case-insensitive fallback — HDR extensions are sometimes uppercased.
    for sibling in p.parent.iterdir():
        if sibling.stem.lower() == p.stem.lower() and sibling.suffix.lower() == ".hdr":
            return sibling
    return None


def find_pair_in_zip(zip_path: str | os.PathLike) -> tuple[str, str] | None:
    """Return ``(cube_name, hdr_name)`` for a matched pair inside *zip_path*.

    Scans the archive for exactly one cube file (by CUBE_SUFFIXES) and exactly
    one ``.hdr`` file. Returns ``None`` if the archive does not contain a
    resolvable pair — callers should raise with a clear message.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    cubes = [n for n in names if n.lower().endswith(CUBE_SUFFIXES)]
    hdrs = [n for n in names if n.lower().endswith(".hdr")]
    if len(cubes) == 1 and len(hdrs) == 1:
        return cubes[0], hdrs[0]
    # More than one pair is not supported in MVP: users should upload separate
    # zips per scene.
    return None


def _extract_zip_member_to_tempfile(
    zf: zipfile.ZipFile, member: str, suffix: str
) -> str:
    """Stream a zip member to a NamedTemporaryFile. Returns its absolute path.

    Caller is responsible for deletion.
    """
    with zf.open(member) as src, tempfile.NamedTemporaryFile(
        mode="wb", delete=False, suffix=suffix
    ) as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
        return dst.name


def read_header_from_path(hdr_path: str | os.PathLike) -> dict:
    """Parse an ENVI .hdr file from disk into a dict (via ``spectral``)."""
    return envi.read_envi_header(os.fspath(hdr_path))


def read_header_from_zip(zip_path: str | os.PathLike, hdr_member: str) -> dict:
    """Parse an ENVI .hdr file from inside a zip. Streams to a temp file first."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        tmp = _extract_zip_member_to_tempfile(zf, hdr_member, ".hdr")
    try:
        return envi.read_envi_header(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _as_list(value) -> list[str]:
    """ENVI multi-value fields may arrive as a list (already parsed by ``spectral``)
    or a brace-delimited string. Normalize to a list of stripped tokens.
    """
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") and s.endswith("}"):
            s = s[1:-1]
        return [t.strip() for t in s.split(",") if t.strip()]
    return [str(value).strip()]


def _pick_rgb_by_wavelength(wavelengths: list[float]) -> tuple[int, int, int]:
    """Pick bands whose center wavelengths are closest to 650/550/450 nm."""
    arr = np.asarray(wavelengths, dtype=np.float64)
    return tuple(int(np.argmin(np.abs(arr - target))) for target in _FALLBACK_RGB_WAVELENGTHS_NM)


def _pick_rgb_by_band_count(band_count: int) -> tuple[int, int, int]:
    """Fallback when no wavelength array is present: R/G/B at 3/4, 1/2, 1/4."""
    if band_count < 3:
        raise HyperspectralError(f"need at least 3 bands to composite, got {band_count}")
    r = max(0, min(band_count - 1, int(round(band_count * 0.75)) - 1))
    g = max(0, min(band_count - 1, int(round(band_count * 0.50)) - 1))
    b = max(0, min(band_count - 1, int(round(band_count * 0.25)) - 1))
    return r, g, b


def extract_header_info(hdr: dict) -> HyperspectralHeaderInfo:
    """Normalize a parsed ENVI header dict into a HyperspectralHeaderInfo.

    Raises HyperspectralError on missing required fields.
    """
    try:
        band_count = int(hdr["bands"])
        lines = int(hdr["lines"])
        samples = int(hdr["samples"])
    except KeyError as e:
        raise HyperspectralError(f"HDR missing required field: {e.args[0]}") from None

    dtype_code = int(hdr.get("data type", 4))
    if dtype_code not in ENVI_DTYPE_MAP:
        raise HyperspectralError(f"unsupported ENVI data type code: {dtype_code}")
    dtype = ENVI_DTYPE_MAP[dtype_code]

    interleave = str(hdr.get("interleave", "bsq")).strip().lower()
    if interleave not in ("bsq", "bil", "bip"):
        raise HyperspectralError(f"unsupported interleave: {interleave}")

    wavelengths: list[float] | None = None
    if "wavelength" in hdr:
        try:
            wavelengths = [float(w) for w in _as_list(hdr["wavelength"])]
        except ValueError:
            wavelengths = None

    if "default bands" in hdr:
        try:
            # ENVI 'default bands' can be 1-based in some exporters and 0-based
            # in others. Spectral passes through as written; we assume 1-based
            # (ENVI/ArcMap convention) and convert to 0-based. If a value is 0
            # we leave it — it's already 0-based from that producer.
            raw = [float(v) for v in _as_list(hdr["default bands"])]
            idxs = [int(v) - 1 if v >= 1 else int(v) for v in raw[:3]]
            if len(idxs) == 3 and all(0 <= i < band_count for i in idxs):
                default_r, default_g, default_b = idxs
            else:
                raise ValueError
        except (ValueError, TypeError):
            default_r, default_g, default_b = (
                _pick_rgb_by_wavelength(wavelengths) if wavelengths
                else _pick_rgb_by_band_count(band_count)
            )
    elif wavelengths:
        default_r, default_g, default_b = _pick_rgb_by_wavelength(wavelengths)
    else:
        default_r, default_g, default_b = _pick_rgb_by_band_count(band_count)

    default_stretch = _as_list(hdr["default stretch"]) if "default stretch" in hdr else None

    data_ignore_value: float | None = None
    if "data ignore value" in hdr:
        try:
            data_ignore_value = float(hdr["data ignore value"])
        except (ValueError, TypeError):
            data_ignore_value = None

    crs_wkt: str | None = None
    if "coordinate system string" in hdr:
        crs_wkt = ", ".join(_as_list(hdr["coordinate system string"]))

    map_info: list[str] | None = None
    if "map info" in hdr:
        map_info = _as_list(hdr["map info"])

    return HyperspectralHeaderInfo(
        band_count=band_count,
        lines=lines,
        samples=samples,
        interleave=interleave,
        dtype=dtype,
        default_r_band=default_r,
        default_g_band=default_g,
        default_b_band=default_b,
        default_stretch=default_stretch,
        wavelengths=wavelengths,
        data_ignore_value=data_ignore_value,
        crs_wkt=crs_wkt,
        map_info=map_info,
    )


def _apply_stretch(
    band: np.ndarray,
    stretch_lo_pct: float,
    stretch_hi_pct: float,
    valid_mask: np.ndarray,
) -> np.ndarray:
    """Per-band percentile linear stretch → uint8 [0, 255].

    Ignores masked-out pixels when computing percentiles.
    """
    if valid_mask.any():
        lo, hi = np.percentile(band[valid_mask], [stretch_lo_pct, stretch_hi_pct])
    else:
        lo, hi = 0.0, 1.0
    if hi <= lo:
        hi = lo + 1.0
    scaled = np.clip((band.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0)
    return (scaled * 255.0).astype(np.uint8)


def compose_rgba(
    cube_path: str | os.PathLike,
    hdr_path: str | os.PathLike,
    info: HyperspectralHeaderInfo,
    *,
    r_band: int | None = None,
    g_band: int | None = None,
    b_band: int | None = None,
    stretch_lo_pct: float | None = None,
    stretch_hi_pct: float | None = None,
) -> PILImage.Image:
    """Render an RGBA PIL image from 3 bands of the hyperspectral cube.

    Any of ``r_band`` / ``g_band`` / ``b_band`` that are ``None`` default to
    ``info.default_*_band``. Stretch percentiles default to
    ``DEFAULT_STRETCH_PERCENTILES``. ``data_ignore_value`` and NaN pixels become
    fully transparent.
    """
    r = info.default_r_band if r_band is None else r_band
    g = info.default_g_band if g_band is None else g_band
    b = info.default_b_band if b_band is None else b_band
    for name, idx in (("r", r), ("g", g), ("b", b)):
        if not (0 <= idx < info.band_count):
            raise HyperspectralError(
                f"{name}_band={idx} out of range [0, {info.band_count})"
            )

    lo = DEFAULT_STRETCH_PERCENTILES[0] if stretch_lo_pct is None else stretch_lo_pct
    hi = DEFAULT_STRETCH_PERCENTILES[1] if stretch_hi_pct is None else stretch_hi_pct
    if not (0.0 <= lo < hi <= 100.0):
        raise HyperspectralError(f"invalid stretch percentiles: lo={lo}, hi={hi}")

    # spectral.envi.open detects interleave from the HDR itself.
    img = envi.open(os.fspath(hdr_path), os.fspath(cube_path))
    # ``.read_band(i)`` reads a single band as (lines, samples); for BSQ that
    # is a contiguous slab and therefore cheap. For BIL/BIP spectral still
    # strides correctly, just with more I/O.
    r_arr = np.asarray(img.read_band(r))
    g_arr = np.asarray(img.read_band(g))
    b_arr = np.asarray(img.read_band(b))

    # Build validity mask: finite AND not equal to data_ignore_value.
    nodata = info.data_ignore_value
    valid = np.isfinite(r_arr) & np.isfinite(g_arr) & np.isfinite(b_arr)
    if nodata is not None:
        valid &= (r_arr != nodata) & (g_arr != nodata) & (b_arr != nodata)

    r8 = _apply_stretch(r_arr, lo, hi, valid)
    g8 = _apply_stretch(g_arr, lo, hi, valid)
    b8 = _apply_stretch(b_arr, lo, hi, valid)
    a8 = np.where(valid, np.uint8(255), np.uint8(0))

    rgba = np.dstack([r8, g8, b8, a8])
    return PILImage.fromarray(rgba, mode="RGBA")


def resolve_pair(
    data_path: str | os.PathLike,
) -> tuple[Path, Path, str | None]:
    """Resolve a data file to a ``(cube_path, hdr_path, zip_source)`` triple.

    For a plain BSQ/IMG/BIL/BIP, finds the sibling .hdr.
    For a .zip, extracts both cube and hdr to temp files and returns those
    paths alongside the original zip path (so the caller can clean up).

    Raises HyperspectralError with a user-facing message on failure.
    """
    p = Path(data_path)
    suffix = p.suffix.lower()

    if suffix == ".zip":
        pair = find_pair_in_zip(p)
        if pair is None:
            raise HyperspectralError(
                f"{p.name}: archive must contain exactly one cube file "
                f"(.bsq/.img/.bil/.bip) and one matching .hdr"
            )
        cube_name, hdr_name = pair
        with zipfile.ZipFile(p, "r") as zf:
            hdr_tmp = _extract_zip_member_to_tempfile(zf, hdr_name, ".hdr")
            cube_tmp = _extract_zip_member_to_tempfile(
                zf, cube_name, Path(cube_name).suffix
            )
        return Path(cube_tmp), Path(hdr_tmp), os.fspath(p)

    if suffix not in CUBE_SUFFIXES:
        raise HyperspectralError(f"{p.name}: not a recognized hyperspectral cube suffix")

    hdr = find_hdr_sibling(p)
    if hdr is None:
        raise HyperspectralError(f"{p.name}: no matching .hdr file found")
    return p, hdr, None


_SUFFIX_RE = re.compile(r"\.(bsq|img|bil|bip)$", re.IGNORECASE)


def is_hyperspectral_data_filename(name: str) -> bool:
    """True iff *name* looks like a hyperspectral cube file (by suffix)."""
    return bool(_SUFFIX_RE.search(name))


def is_hyperspectral_zip(path: str | os.PathLike) -> bool:
    """True iff *path* is a .zip containing exactly one cube + one HDR."""
    if Path(path).suffix.lower() != ".zip":
        return False
    try:
        return find_pair_in_zip(path) is not None
    except (zipfile.BadZipFile, OSError):
        return False
