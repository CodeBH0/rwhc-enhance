#!/usr/bin/env python3
"""Build the downloadable source package for a release tag.

This script is the single source of truth for what a release contains. It is used
both locally and by `.github/workflows/release.yml`, so a package built on a
workstation and one built by CI hold exactly the same files: entries are taken from
the tagged commit (never from the working tree), filtered by the rules below, and
written deterministically (sorted, fixed timestamp, no platform-specific metadata).

Usage::

    python .github/scripts/release_package.py --tag v2026.09.22 --out dist
    python .github/scripts/release_package.py --tag v2026.09.22 --out dist \
        --notes dist/RELEASE_NOTES.md

What is packaged (the "runnable application tree") and what is not is defined by
INCLUDE_RULES/EXCLUDE_RULES; the package is then verified before it is left on disk:
every included file is re-read out of the zip and compared byte for byte with the
tagged blob, required files must be present, and local-only artifacts must be absent.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import zipfile

# --------------------------------------------------------------------------------------
# Packaging rules
# --------------------------------------------------------------------------------------

# Excluded from the package: paths matching any of these patterns are dropped.
# (pattern, human readable reason)
EXCLUDE_RULES: list[tuple[str, str]] = [
    (r"^\.github/", "CI plumbing (workflows, this script)"),
    (r"(^|/)__pycache__/", "byte-compiled caches"),
    (r"\.py[co]$", "byte-compiled caches"),
    (r"(^|/)\.venv[^/]*/", "virtual environments"),
    (r"^archive/", "local-only version archive (see .gitignore)"),
    (r"\.log$", "runtime measurement logs"),
    (r"(^|/)horseshoe_cache\.npy$", "regenerable numpy cache"),
    (r"(^|/)\.DS_Store$", "OS metadata"),
    (r"(^|/)Thumbs\.db$", "OS metadata"),
]

# Must be present in every release package, whatever else changes.
REQUIRED_ENTRIES: list[str] = [
    "app.py",
    "requirements.txt",
    "README.md",
    "README_zh.md",
    "CHANGELOG.md",
    "CHANGELOG_zh.md",
    "LICENSE",
    "bin/dogegen.exe",
    "bin/oeminst.exe",
    "bin/spotread.exe",
    "data/hdr_empty.icc",
    "i18n/i18n_loader.py",
    "i18n/locales/messages_en.po",
    "i18n/locales/messages_zh.po",
    "logs/readme.txt",
    "resources/ui.png",
    "resources/ui_zh.png",
    "tools/__init__.py",
    "wexpect-4.0.0/wexpect/__init__.py",
]

# Never acceptable in a package, even if a rule above is edited by mistake.
FORBIDDEN_PATTERNS: list[str] = [
    r"\.py[co]$",
    r"(^|/)__pycache__/",
    r"(^|/)\.venv",
    r"^archive/",
    r"\.log$",
    r"^\.git/",
]

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # fixed: keeps builds reproducible


def run_git(args: list[str], repo: str, binary: bool = False, stdin_bytes: bytes | None = None):
    """Run a git command, raising a readable error on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"git {' '.join(args)} failed:\n{detail}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def repo_root() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if out.returncode != 0:
        raise SystemExit("not inside a git repository")
    return out.stdout.decode("utf-8", "replace").strip()


def resolve_commit(repo: str, tag: str) -> tuple[str, str]:
    """Return (commit sha, tag) for a tag name, or for a raw ref/commit."""
    ref = tag if "/" in tag or re.fullmatch(r"[0-9a-f]{7,40}", tag) else f"refs/tags/{tag}"
    out = run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo).strip()
    if not out:
        raise SystemExit(f"cannot resolve '{tag}' to a commit")
    return out.splitlines()[0], tag


def tracked_files(repo: str, commit: str) -> list[tuple[str, str, int]]:
    """Return [(path, blob_oid, mode)] for every file tracked in the commit."""
    raw = run_git(["ls-tree", "-r", "-z", commit], repo)
    files: list[tuple[str, str, int]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, otype, oid = meta.split()
        if otype != "blob":
            continue
        files.append((path, oid, int(mode, 8)))
    return files


def read_blobs(repo: str, entries: list[tuple[str, str, int]]) -> dict[str, bytes]:
    """Read the tagged content of many blobs in one `git cat-file --batch` call."""
    oids = b"".join(oid.encode("ascii") + b"\n" for _, oid, _ in entries)
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        input=oids,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise SystemExit("git cat-file --batch failed: " + proc.stderr.decode("utf-8", "replace"))

    stream = io.BytesIO(proc.stdout)
    blobs: dict[str, bytes] = {}
    for path, oid, _ in entries:
        header = stream.readline().decode("utf-8", "replace").strip()
        parts = header.split()
        if len(parts) != 3 or parts[0] != oid:
            raise SystemExit(f"unexpected cat-file header for {path}: {header!r}")
        size = int(parts[2])
        data = stream.read(size)
        stream.read(1)  # trailing newline
        blobs[path] = data
    return blobs


def exclusion_reason(path: str) -> str | None:
    for pattern, reason in EXCLUDE_RULES:
        if re.search(pattern, path):
            return reason
    return None


def select(entries: list[tuple[str, str, int]]) -> tuple[list[str], dict[str, int]]:
    included: list[str] = []
    excluded: dict[str, int] = {}
    for path, _, _ in entries:
        reason = exclusion_reason(path)
        if reason is None:
            included.append(path)
        else:
            excluded[reason] = excluded.get(reason, 0) + 1
    return sorted(included), excluded


def build_zip(zip_path: str, prefix: str, blobs: dict[str, bytes], paths: list[str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in paths:
            info = zipfile.ZipInfo(prefix + path, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0  # MS-DOS: no platform-specific metadata
            info.external_attr = 0o644 << 16
            zf.writestr(info, blobs[path], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_zip(zip_path: str, prefix: str, blobs: dict[str, bytes], paths: list[str]) -> None:
    """Fail loudly unless the zip is exactly the selected tagged content."""
    with zipfile.ZipFile(zip_path) as zf:
        broken = zf.testzip()
        if broken is not None:
            raise SystemExit(f"corrupt zip entry: {broken}")
        names = zf.namelist()
        expected = [prefix + p for p in paths]
        if sorted(names) != expected:
            missing = sorted(set(expected) - set(names))
            extra = sorted(set(names) - set(expected))
            raise SystemExit(f"zip contents differ from the tag (missing={missing}, extra={extra})")
        if len(names) != len(set(names)):
            raise SystemExit("zip contains duplicate entries")
        for path in paths:
            got = zf.read(prefix + path)
            if sha256(got) != sha256(blobs[path]):
                raise SystemExit(f"content mismatch for {path}")
        if zf.comment:
            raise SystemExit("unexpected zip comment")

    for entry in names:
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, entry[len(prefix):]):
                raise SystemExit(f"forbidden entry in package: {entry}")

    stripped = {n[len(prefix):] for n in names}
    missing_required = [r for r in REQUIRED_ENTRIES if r not in stripped]
    if missing_required:
        raise SystemExit("required files missing from the package: " + ", ".join(missing_required))


def release_notes(repo: str, commit: str, tag: str) -> str:
    """Notes for the tag: docs/release-notes/<tag>.md, else the CHANGELOG section."""
    candidates = [f"docs/release-notes/{tag}.md", f"docs/release-notes/{tag.lower()}.md"]
    for candidate in candidates:
        proc = subprocess.run(
            ["git", "show", f"{commit}:{candidate}"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.decode("utf-8", "replace").strip() + "\n"

    date = tag.lstrip("vV")
    changelog = subprocess.run(
        ["git", "show", f"{commit}:CHANGELOG.md"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if changelog.returncode == 0:
        text = changelog.stdout.decode("utf-8", "replace")
        match = re.search(
            rf"^##\s*\[?{re.escape(date)}\]?.*?(?=^##\s|\Z)", text, re.S | re.M
        )
        if match and match.group(0).strip():
            body = match.group(0).strip()
            header = f"# rwhc {tag}\n\n> Release notes extracted from CHANGELOG.md.\n\n"
            return header + body + "\n"

    return (
        f"# rwhc {tag}\n\n"
        f"Source package for tag `{tag}`.\n\n"
        "See [CHANGELOG.md](https://github.com/CodeBH0/rwhc-enhance/blob/master/CHANGELOG.md) "
        "for details.\n"
    )


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n} B"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="release tag to package, e.g. v2026.09.22")
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    parser.add_argument("--notes", help="write the release notes to this file")
    parser.add_argument("--repo", help="repository path (default: the current one)")
    args = parser.parse_args()

    repo = os.path.abspath(args.repo) if args.repo else repo_root()
    commit, tag = resolve_commit(repo, args.tag)
    short = run_git(["rev-parse", "--short", commit], repo).strip()

    entries = tracked_files(repo, commit)
    paths, excluded = select(entries)
    blobs = read_blobs(repo, [e for e in entries if e[0] in set(paths)])

    zip_name = f"rwhc-{tag}.zip"
    prefix = f"rwhc-{tag}/"
    zip_path = os.path.join(args.out, zip_name)
    build_zip(zip_path, prefix, blobs, paths)
    verify_zip(zip_path, prefix, blobs, paths)

    digest = sha256(open(zip_path, "rb").read())
    sidecar = f"{zip_path}.sha256"
    with open(sidecar, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{digest}  {zip_name}\n")

    uncompressed = sum(len(blobs[p]) for p in paths)
    compressed = os.path.getsize(zip_path)

    print(f"tag            : {tag} ({short})")
    print(f"package        : {zip_path}")
    print(f"files          : {len(paths)}")
    print(f"uncompressed   : {human(uncompressed)}")
    print(f"zip size       : {human(compressed)} ({compressed / max(uncompressed, 1):.1%} of original)")
    print(f"sha256         : {digest}")
    print(f"checksum file  : {sidecar}")
    if excluded:
        print("excluded       :")
        for reason, count in sorted(excluded.items()):
            print(f"  - {count:>3} x {reason}")
    print(f"verified       : every entry re-read from the zip and compared with the tag")

    if args.notes:
        notes = release_notes(repo, commit, tag)
        os.makedirs(os.path.dirname(os.path.abspath(args.notes)), exist_ok=True)
        with open(args.notes, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(notes)
        print(f"release notes  : {args.notes} ({len(notes.splitlines())} lines)")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"### Release package `{zip_name}`\n\n"
                f"| field | value |\n| --- | --- |\n"
                f"| tag | `{tag}` (`{short}`) |\n"
                f"| files | {len(paths)} |\n"
                f"| size | {human(compressed)} (uncompressed {human(uncompressed)}) |\n"
                f"| sha256 | `{digest}` |\n"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
