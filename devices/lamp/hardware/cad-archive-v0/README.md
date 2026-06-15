# cad-archive-v0 (old, no longer in use)

This folder archived the original v0 lamp CAD (`lamp-v3.stp`). The active,
current CAD lives in `../cad/` (per-part STEP in `cad/step/`, printable STL in
`cad/stl/`).

## Why `lamp-v3.stp` was removed

`lamp-v3.stp` was stored via Git LFS, but its LFS binary no longer exists on the
remote (server returns `404 Object does not exist`). The remaining pointer broke
`git pull` / checkout for everyone, so it was removed.

If the original v0 STEP is still needed, recover it from the original CAD source
(e.g. the Fusion project or the earlier Mega.nz backup) and re-add it.
