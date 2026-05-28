# CAD

Mechanical source files for Lamp.

Large CAD binaries (`*.stp`, `*.step`, `*.stl`, `*.f3d`, `*.f3z`) are tracked
via **Git LFS**. See the repo-root `.gitattributes` for the filter rules.

## Files

| File | Format | Added |
|------|--------|-------|
| `lamp-v3.stp` | STEP AP214 | 2026-05-20 |

## Uploading a new revision

1. Install Git LFS once per machine: `brew install git-lfs && git lfs install`.
2. Drop the file in `hardware/cad/`.
3. Commit and push as normal:

   ```bash
   git add hardware/cad/lamp-v3.stp
   git commit -m "cad: bump lamp-v3"
   git push
   ```

   Git LFS handles the upload to GitHub's LFS storage automatically.
4. Update the table above (file + date) and commit `hardware/cad/README.md`.

## Cloning

A fresh clone needs LFS too. After `git clone`, run:

```bash
git lfs install
git lfs pull
```

to fetch the actual binaries (otherwise the working tree gets LFS pointer
stubs instead of the real files).

## Changelog

- **v3** (2026-05-20) — initial STEP export.
