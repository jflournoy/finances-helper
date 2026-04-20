#!/usr/bin/env python3
"""Manage Amazon order history zip in data/imports/ — keep newest, remove old ones."""

from pathlib import Path
import sys


def update_amazon_orders():
    imports_dir = Path("data/imports")

    zip_files = sorted(imports_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    if not zip_files:
        print("Error: No zip files found in data/imports/", file=sys.stderr)
        sys.exit(1)

    newest_zip = zip_files[-1]
    print(f"Keeping: {newest_zip.name}")

    for old_zip in zip_files[:-1]:
        print(f"Removing: {old_zip.name}")
        old_zip.unlink()


if __name__ == "__main__":
    update_amazon_orders()
