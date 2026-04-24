### Added

- Hyperspectral annotation: upload ENVI BSQ/BIL/BIP cubes (as a `.bsq`/`.img`
  paired with a `.hdr`, or a `.zip` containing the pair) and annotate them as
  2D images. A new band-selection panel in the image-settings popover lets
  users change which three bands map to R/G/B and adjust per-band percentile
  stretch on the fly — the server re-composites and caches per-request. NaN
  and `data ignore value` pixels render transparent.

### Changed

- `.hdr` files are now classified as ENVI hyperspectral sidecars at
  task-creation time (previously treated as Radiance HDR imagery). A `.hdr`
  uploaded without a paired cube (`.bsq`/`.img`/`.bil`/`.bip`) is rejected.
