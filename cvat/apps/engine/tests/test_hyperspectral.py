# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

"""Unit tests for the ENVI hyperspectral support.

These tests exercise the pure-Python paths (header parsing, pair
resolution, RGBA compositing, pair validation) against tiny synthetic
fixtures written on the fly. REST / upload / chunk-cache integration is
covered by tests/python/rest_api/ against a running stack.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image as PILImage

from cvat.apps.engine.hyperspectral import (
    DEFAULT_STRETCH_PERCENTILES,
    HyperspectralError,
    compose_rgba,
    extract_header_info,
    find_hdr_sibling,
    find_pair_in_zip,
    is_hyperspectral_zip,
    read_header_from_path,
    resolve_pair,
)


# Small fixture: 6 bands × 4 lines × 5 samples, float32, BSQ.
FIXTURE_BANDS = 6
FIXTURE_LINES = 4
FIXTURE_SAMPLES = 5


def _write_fixture(
    dirname: str,
    stem: str = "scene",
    *,
    include_default_bands: bool = True,
    include_wavelengths: bool = True,
    include_ignore_value: bool = True,
    include_default_stretch: bool = False,
) -> tuple[Path, Path]:
    """Write a synthetic BSQ + HDR pair. Returns (bsq_path, hdr_path).

    Cube values are chosen to be unique per (band, line, sample) so any
    swap in the composite logic is visible in the resulting pixel colors.
    """
    rng = np.random.default_rng(42)
    cube = (rng.random(
        (FIXTURE_BANDS, FIXTURE_LINES, FIXTURE_SAMPLES), dtype=np.float32
    ) * 10.0).astype(np.float32)
    # Inject a nodata sentinel in a known pixel for alpha-mask testing.
    if include_ignore_value:
        cube[:, 0, 0] = np.float32(2.0)

    bsq_path = Path(dirname) / f"{stem}.bsq"
    hdr_path = Path(dirname) / f"{stem}.hdr"
    cube.tofile(bsq_path)

    lines = [
        "ENVI",
        "description = {synthetic test scene}",
        f"samples = {FIXTURE_SAMPLES}",
        f"lines   = {FIXTURE_LINES}",
        f"bands   = {FIXTURE_BANDS}",
        "header offset = 0",
        "file type = ENVI Standard",
        "data type = 4",
        "interleave = BSQ",
        "byte order = 0",
    ]
    if include_default_bands:
        # 1-based ENVI convention; values 3/2/1 → 0-based 2/1/0.
        lines.append("default bands = {3, 2, 1}")
    if include_default_stretch:
        lines.append("default stretch = 2.0 % linear")
    if include_wavelengths:
        wls = ", ".join(f"{400.0 + i * 25.0:.2f}" for i in range(FIXTURE_BANDS))
        lines.append("wavelength = {" + wls + "}")
    if include_ignore_value:
        lines.append("data ignore value = 2")

    hdr_path.write_text("\n".join(lines) + "\n")
    return bsq_path, hdr_path


class TestHeaderParsing(unittest.TestCase):
    def test_basic_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, hdr = _write_fixture(tmp)
            info = extract_header_info(read_header_from_path(hdr))
        self.assertEqual(info.band_count, FIXTURE_BANDS)
        self.assertEqual(info.lines, FIXTURE_LINES)
        self.assertEqual(info.samples, FIXTURE_SAMPLES)
        self.assertEqual(info.dtype, "float32")
        self.assertEqual(info.interleave, "bsq")
        # 1-based {3,2,1} from HDR → 0-based (2,1,0).
        self.assertEqual(
            (info.default_r_band, info.default_g_band, info.default_b_band),
            (2, 1, 0),
        )
        self.assertEqual(info.data_ignore_value, 2.0)
        self.assertIsNotNone(info.wavelengths)
        self.assertEqual(len(info.wavelengths), FIXTURE_BANDS)

    def test_fallback_picks_by_wavelength(self):
        # No `default bands` — must pick via wavelength proximity.
        with tempfile.TemporaryDirectory() as tmp:
            _, hdr = _write_fixture(tmp, include_default_bands=False)
            info = extract_header_info(read_header_from_path(hdr))
        # Wavelengths: 400, 425, 450, 475, 500, 525 nm. R=650 → closest 525 (idx 5).
        # G=550 → closest 525 (idx 5). B=450 → idx 2. We just assert non-None
        # and that the picks are valid indices.
        for idx in (info.default_r_band, info.default_g_band, info.default_b_band):
            self.assertTrue(0 <= idx < info.band_count)

    def test_fallback_picks_by_band_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, hdr = _write_fixture(
                tmp, include_default_bands=False, include_wavelengths=False,
            )
            info = extract_header_info(read_header_from_path(hdr))
        for idx in (info.default_r_band, info.default_g_band, info.default_b_band):
            self.assertTrue(0 <= idx < info.band_count)
        # Should have different values (not all collapsed to one band).
        self.assertEqual(
            len({info.default_r_band, info.default_g_band, info.default_b_band}), 3,
        )

    def test_bad_data_type_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            hdr = Path(tmp) / "bad.hdr"
            hdr.write_text(
                "ENVI\nsamples = 1\nlines = 1\nbands = 1\n"
                "data type = 99\ninterleave = BSQ\nbyte order = 0\n"
            )
            with self.assertRaises(HyperspectralError):
                extract_header_info(read_header_from_path(hdr))


class TestPairResolution(unittest.TestCase):
    def test_sibling_hdr_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            found = find_hdr_sibling(bsq)
            self.assertEqual(Path(found), hdr)

    def test_sibling_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            # Rename HDR to uppercase suffix
            upper = hdr.with_suffix(".HDR")
            hdr.rename(upper)
            found = find_hdr_sibling(bsq)
            self.assertEqual(Path(found), upper)

    def test_no_sibling_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            hdr.unlink()
            self.assertIsNone(find_hdr_sibling(bsq))

    def test_resolve_pair_plain(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            cube_p, hdr_p, zip_src = resolve_pair(bsq)
            self.assertEqual(cube_p, bsq)
            self.assertEqual(hdr_p, hdr)
            self.assertIsNone(zip_src)

    def test_resolve_pair_missing_hdr_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            hdr.unlink()
            with self.assertRaises(HyperspectralError):
                resolve_pair(bsq)


class TestZipBundle(unittest.TestCase):
    def _make_zip(self, tmp: str, stem: str = "scene") -> Path:
        bsq, hdr = _write_fixture(tmp, stem=stem)
        zip_path = Path(tmp) / f"{stem}.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(bsq, arcname=bsq.name)
            zf.write(hdr, arcname=hdr.name)
        bsq.unlink()
        hdr.unlink()
        return zip_path

    def test_find_pair_in_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = self._make_zip(tmp)
            pair = find_pair_in_zip(zip_path)
            self.assertIsNotNone(pair)
            cube_name, hdr_name = pair
            self.assertTrue(cube_name.endswith(".bsq"))
            self.assertTrue(hdr_name.endswith(".hdr"))

    def test_is_hyperspectral_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = self._make_zip(tmp)
            self.assertTrue(is_hyperspectral_zip(zip_path))

    def test_is_not_hyperspectral_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "images.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("a.jpg", b"not really jpeg")
                zf.writestr("b.jpg", b"also not jpeg")
            self.assertFalse(is_hyperspectral_zip(zip_path))


class TestCompositor(unittest.TestCase):
    def test_default_compose_produces_rgba(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            info = extract_header_info(read_header_from_path(hdr))
            img = compose_rgba(bsq, hdr, info)
        self.assertIsInstance(img, PILImage.Image)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (FIXTURE_SAMPLES, FIXTURE_LINES))

    def test_nodata_pixel_renders_transparent(self):
        # Fixture writes `data ignore value = 2` at (line=0, sample=0) across
        # all bands, so the (0,0) pixel must end up with alpha == 0.
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            info = extract_header_info(read_header_from_path(hdr))
            img = compose_rgba(bsq, hdr, info)
            arr = np.array(img)
        self.assertEqual(arr[0, 0, 3], 0, "nodata pixel should be transparent")
        # A pixel that isn't nodata should be opaque.
        self.assertEqual(arr[FIXTURE_LINES - 1, FIXTURE_SAMPLES - 1, 3], 255)

    def test_custom_bands_differ_from_default(self):
        # Rendering with a non-default band triple should produce different
        # pixel values from the default composite.
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            info = extract_header_info(read_header_from_path(hdr))
            default = np.array(compose_rgba(bsq, hdr, info))
            custom = np.array(compose_rgba(
                bsq, hdr, info, r_band=5, g_band=4, b_band=3,
            ))
        # Alpha channel is identical (nodata mask is band-independent), but
        # RGB channels must differ somewhere.
        self.assertFalse(np.array_equal(default[..., :3], custom[..., :3]))

    def test_out_of_range_band_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            info = extract_header_info(read_header_from_path(hdr))
            with self.assertRaises(HyperspectralError):
                compose_rgba(bsq, hdr, info, r_band=FIXTURE_BANDS + 5)

    def test_invalid_stretch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            bsq, hdr = _write_fixture(tmp)
            info = extract_header_info(read_header_from_path(hdr))
            with self.assertRaises(HyperspectralError):
                compose_rgba(bsq, hdr, info, stretch_lo_pct=50, stretch_hi_pct=10)


class TestValidation(unittest.TestCase):
    def test_validate_pairs_rejects_lone_cube(self):
        from cvat.apps.engine.task import _validate_hyperspectral_pairs

        with self.assertRaisesRegex(ValueError, "without a matching .hdr"):
            _validate_hyperspectral_pairs(["scene.bsq"])

    def test_validate_pairs_accepts_paired(self):
        from cvat.apps.engine.task import _validate_hyperspectral_pairs

        # No raise expected.
        _validate_hyperspectral_pairs(["scene.bsq", "scene.hdr"])

    def test_validate_pairs_rejects_lone_hdr(self):
        from cvat.apps.engine.task import _validate_hyperspectral_pairs

        with self.assertRaisesRegex(ValueError, "without a matching cube"):
            _validate_hyperspectral_pairs(["scene.hdr"])

    def test_validate_pairs_tolerates_orphan_hdr_with_zip(self):
        # An orphan HDR might be matched by a cube inside a .zip — we don't
        # crack open every zip to check, so we let it pass in that case.
        from cvat.apps.engine.task import _validate_hyperspectral_pairs

        _validate_hyperspectral_pairs(["other_scene.hdr", "bundle.zip"])


if __name__ == "__main__":
    unittest.main()
