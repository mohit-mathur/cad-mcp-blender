#!/usr/bin/env python3
"""
Build the Blender extension/addon zip.

Layout produced (works for both Blender 3.x-4.1 legacy addons and 4.2+ extensions):

    cad-mcp-blender-{version}.zip
    └── cad_mcp_blender/
        ├── __init__.py
        └── blender_manifest.toml

Usage:
    python scripts/build_addon.py [--out dist/]
"""
import argparse
import os
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_SRC = REPO_ROOT / "addon"
PACKAGE_NAME = "cad_mcp_blender"


def read_version() -> str:
    manifest = ADDON_SRC / "blender_manifest.toml"
    with manifest.open("rb") as f:
        data = tomllib.load(f)
    return data["version"]


def verify_versions_match(version: str) -> None:
    """bl_info in __init__.py must agree with blender_manifest.toml."""
    init_text = (ADDON_SRC / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)', init_text)
    if not m:
        sys.exit("ERROR: could not find bl_info version tuple in __init__.py")
    bl_version = ".".join(m.groups())
    if bl_version != version:
        sys.exit(
            f"ERROR: version mismatch — blender_manifest.toml={version} "
            f"vs bl_info={bl_version}. Bump both to the same value."
        )


def build(out_dir: Path, version: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"cad-mcp-blender-{version}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in sorted(ADDON_SRC.iterdir()):
            if src.name.startswith((".", "__pycache__")):
                continue
            arcname = f"{PACKAGE_NAME}/{src.name}"
            zf.write(src, arcname)

    return zip_path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=REPO_ROOT / "dist")
    args = p.parse_args()

    version = read_version()
    verify_versions_match(version)
    zip_path = build(args.out, version)
    print(f"Built {zip_path} ({zip_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
