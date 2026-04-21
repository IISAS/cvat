---
title: 'Hyperspectral imagery'
linkTitle: 'Hyperspectral imagery'
weight: 60
description: 'Uploading ENVI BSQ/BIL/BIP cubes and choosing bands for RGB composite annotation.'
---

CVAT supports annotating hyperspectral scenes stored in the ENVI format. Each scene is presented to the canvas as an RGB composite of three chosen bands — the full spectral cube stays on the server, and analysts can change which bands map to red, green, and blue without re-uploading.

## Supported inputs

Each scene is a **cube file + ENVI header** pair:

- Cube: `.bsq` (band sequential), `.img`, `.bil`, or `.bip` (interleave read from the HDR).
- Header: `.hdr` with the same stem, placed alongside the cube.
- Alternatively: a `.zip` containing exactly one cube and one matching `.hdr`.

Multiple scenes can be uploaded in one task — each cube becomes a frame. An HDR without a paired cube (or a cube without an HDR) is rejected at upload time.

## Defaults from the HDR

When the task is created, CVAT reads the ENVI header and seeds the band picker from these fields (falling back to the defaults below if the field is absent):

| HDR field            | Used for                           | Fallback                                                                                          |
| -------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------- |
| `default bands`      | Initial R/G/B indices              | Bands closest to 650/550/450 nm if `wavelength` is present; otherwise bands at 3/4, 1/2, 1/4 of total |
| `default stretch`    | Recorded on the metadata row       | 2–98 % linear percentile stretch                                                                  |
| `wavelength`         | Labels in the band dropdown (nm)   | Band index only                                                                                   |
| `data ignore value`  | Nodata pixels → transparent        | Only NaN pixels are masked                                                                        |
| `coordinate system string` | Stored for later export       | _(optional)_                                                                                      |

## Changing bands during annotation

Open the **image-settings popover** (same place as brightness/contrast/grid). A **Hyperspectral bands** section appears for hyperspectral jobs:

- Three searchable band dropdowns (R, G, B) — labels show the band index and, when available, the center wavelength in nm.
- Two percentile stretch inputs (`lo`, `hi`, default 2 / 98). Stretch is applied per band using the chosen percentiles.
- A **Reset to HDR defaults** button restores the HDR-provided defaults and clears any local override.

Band and stretch choices are debounced (150 ms) and persisted to your browser's `localStorage` per job, so they survive page refreshes on the same machine.

## Notes

- Bands change server-side: when you pick a non-default combination, the server reads only the three requested bands from the cube and re-renders the chunk. Default-band chunks are built at task creation time and served from a warm cache.
- The annotation shapes and labels are independent of the band selection — they are stored in pixel coordinates and remain valid regardless of which bands are currently displayed.
- If you upload multiple scenes in one task and they have different band counts, pick bands within the range of the smallest scene; requests for out-of-range indices are rejected.
