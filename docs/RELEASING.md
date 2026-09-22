# Releasing (maintainer notes)

How a release is cut in this repository. Versions are date-labelled tags
`v<year>.<month>.<day>` (e.g. `v2026.09.02`, `v2026.09.22`), and every tag gets a GitHub
Release carrying a packaged archive.

## What a release contains

`.github/scripts/release_package.py` owns the packaging rules, so a local build and a CI
build hold the same content:

- **Source of truth:** the *tagged commit* (`git ls-tree` + `git cat-file`), never the
  working tree — a release therefore cannot ship uncommitted or local-only files.
- **Excluded:** the repository's own `.github/` at the root (CI plumbing — the vendored
  `wexpect-4.0.0/.github/` from upstream stays), `__pycache__/`, `*.py[co]`, `.venv*/`,
  `archive/`, `*.log`, `hc.log`, `horseshoe_cache.npy`, OS metadata.
- **Required:** `app.py`, `requirements.txt`, both READMEs and CHANGELOGs, `LICENSE`,
  `bin/*.exe`, `data/`, `i18n/`, `logs/`, `resources/`, `tools/`, `wexpect-4.0.0/`.
- Entries are written sorted, with a fixed timestamp and no platform-specific metadata, so an
  archive is reproducible for a given zlib version.
- **Verified before publishing:** every entry is re-read out of the zip and compared byte for
  byte with the tagged blob, required files must be present, and forbidden paths (caches,
  virtualenvs, logs, `archive/`, `.git/`) must be absent.

## Cutting a release

1. Check the working tree is clean and that the version's changes are written up in
   `CHANGELOG.md` and `CHANGELOG_zh.md` (newest section first, dated `YYYY-MM-DD`).
2. Add the release body as `docs/release-notes/<tag>.md`. The workflow uses that file as the
   GitHub Release notes (falling back to the matching CHANGELOG section, then to a stub).
3. Build and inspect the package locally — optional, but it catches problems before the
   workflow runs:

   ```powershell
   python .github/scripts/release_package.py --tag v2026.09.22 --out dist --notes dist/RELEASE_NOTES.md
   ```

   It prints the file count, sizes, sha256, what it excluded, and confirms verification.
   `dist/` is git-ignored.
4. Commit, tag, push:

   ```powershell
   git add -A
   git commit -m "Release v2026.09.22"
   git tag -a v2026.09.22 -m "rwhc v2026.09.22"
   git push origin master
   git push origin v2026.09.22
   ```

   `.github/workflows/release.yml` then packages the tag, publishes the GitHub Release named
   after the tag, and attaches `rwhc-v2026.09.22.zip` plus its `.sha256`.
5. Watch the run under **Actions → Release package**. It ends by downloading the asset it just
   uploaded and checking it against the local `.sha256`. If the run fails nothing is published;
   fix and re-run (Actions UI, or `gh run rerun`). Re-running is safe — an existing release is
   updated and assets are overwritten (`--clobber`).

### Tags that predate the workflow

GitHub only runs a workflow that exists **at the pushed ref**, so `v2026.09.02` (tagged before
`.github/workflows/release.yml` existed) cannot be published by pushing that tag. Use
**Actions → Release package → Run workflow** and pass `tag: v2026.09.02`: the run checks out
`master`, packages the tag itself, and publishes that release. The local command in step 3
produces the identical archive if you would rather upload it by hand.

## Version map for this repository

| tag | date | contents |
| --- | --- | --- |
| `v2026.09.02` | 2026-09-02 | first tagged release: 24-colour card generation, historical gray data, system SDR paper white, ICC conformance fixes |
| `v2026.09.22` | 2026-09-22 | CLUT (`A2B0`/`B2A0`) ICC output, historical colour data, calibration without a colorimeter, MHC2 LUT inverse rewrite, "Save ICC" freeze fix, this packaging workflow |
