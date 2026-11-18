#!/usr/bin/env python3
#
# tools/docs_site_check.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Check the documentation navigation and smoke-test the built site.
Replaces the MkDocs nav validation that Zensical does not provide.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from ci_output import REPO_URL, log, print_header

__author__ = "Gill-Bates"
SCRIPT = Path(__file__).resolve()
HELP_URL = f"{REPO_URL}/blob/main/tools/{SCRIPT.name}"
# Build-time copies of root files; they exist only after the workflow copy step.
GENERATED_PAGES = frozenset({"changelog.md", "license.md"})
NAV_PAGE_RE = re.compile(r"^\s+-\s+(?:[^:#]+:\s+)?(\S+\.md)\s*$")
# Page URL directories and files the published site must contain.
REQUIRED_SITE_FILES = (
    "index.html",
    "404.html",
    "search.json",
    "sitemap.xml",
    "getting-started/installation/index.html",
    "configuration/environment/index.html",
    "storage/questdb/index.html",
    "monitoring/troubleshooting/index.html",
    "development/setup/index.html",
    "changelog/index.html",
    "license/index.html",
    "assets/stylesheets/extra.css",
    "assets/TR-064_WAN_PPP_Connection_v16.pdf",
)
FORBIDDEN_SITE_NAMES = ("mkdocs.yml", "AGENTS.md", "pyproject.toml", ".git")


def nav_pages(config_text: str) -> list[str]:
    """Return the Markdown files listed under the top-level nav key."""
    pages: list[str] = []
    in_nav = False
    for line in config_text.splitlines():
        if re.match(r"^nav:\s*$", line):
            in_nav = True
        elif in_nav and line and not line[0].isspace():
            break
        elif in_nav and (match := NAV_PAGE_RE.match(line)):
            pages.append(match.group(1))
    return pages


def check_nav(config_text: str, docs_dir: Path) -> list[str]:
    """Report nav entries without a file and Markdown files missing from the nav."""
    pages = nav_pages(config_text)
    if not pages:
        return ["no nav entries found in the configuration"]
    errors = [
        f"nav entry has no file: {page}"
        for page in pages
        if page not in GENERATED_PAGES and not (docs_dir / page).is_file()
    ]
    on_disk = {p.relative_to(docs_dir).as_posix() for p in docs_dir.rglob("*.md")}
    on_disk -= {"AGENTS.md"}
    errors += [f"page is not in the nav: {page}" for page in sorted(on_disk - set(pages))]
    return errors


def check_site(site_dir: Path) -> list[str]:
    """Report required files that are missing and repository files that leaked in."""
    errors = [f"missing from site: {name}" for name in REQUIRED_SITE_FILES if not (site_dir / name).is_file()]
    errors += [
        f"must not be published: {path.relative_to(site_dir)}"
        for name in FORBIDDEN_SITE_NAMES
        for path in site_dir.rglob(name)
    ]
    return errors


def main(argv: list[str] | None = None) -> int:
    print_header(SCRIPT, (__doc__ or "").strip().splitlines()[0], __author__, HELP_URL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("mkdocs.yml"))
    parser.add_argument("--docs-dir", type=Path, default=Path("docs"))
    parser.add_argument("--site-dir", type=Path, default=Path("site"))
    args = parser.parse_args(argv)
    try:
        errors = check_nav(args.config.read_text(encoding="utf-8"), args.docs_dir)
        errors += check_site(args.site_dir)
    except OSError as exc:
        log("error", str(exc))
        return 1
    for error in errors:
        log("error", error)
    if not errors:
        log("info", "navigation and site smoke test passed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
