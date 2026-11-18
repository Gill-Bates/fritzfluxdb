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
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ci_output import REPO_URL, log, print_header

__author__ = "Gill-Bates"
SCRIPT = Path(__file__).resolve()
HELP_URL = f"{REPO_URL}/blob/main/tools/{SCRIPT.name}"
# Build-time copies of root files; they exist only after the workflow copy step.
GENERATED_PAGES = frozenset({"changelog.md", "license.md"})
NAV_PAGE_RE = re.compile(r"^\s+-\s+(?:[^:#]+:\s+)?(\S+\.md)\s*$")
# Files and directories the published site must contain besides the nav pages.
REQUIRED_SITE_FILES = (
    "404.html",
    "search.json",
    "sitemap.xml",
    "assets/stylesheets/extra.css",
    "assets/TR-064_WAN_PPP_Connection_v16.pdf",
)
FORBIDDEN_SITE_NAMES = ("mkdocs.yml", "AGENTS.md", "pyproject.toml", ".git", ".env", ".env.*")
URL_ATTR_RE = re.compile(r"""\b(?:href|src)=("[^"]*"|'[^']*'|[^\s>]+)""")
SITE_URL_RE = re.compile(r"^site_url:\s*(\S+)\s*$", re.M)


def page_html(page: str) -> str:
    """Map a nav page to the HTML file Zensical builds with directory URLs."""
    stem = page.removesuffix(".md")
    return "index.html" if stem == "index" else f"{stem.removesuffix('/index')}/index.html"


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


def site_url(config_text: str) -> str:
    """Return the configured site_url with a trailing slash, or an empty string."""
    match = SITE_URL_RE.search(config_text)
    return match.group(1).rstrip("/") + "/" if match else ""


def _target_exists(site_dir: Path, relative: str) -> bool:
    path = site_dir / unquote(relative)
    return path.is_file() or (path.is_dir() and (path / "index.html").is_file())


def check_links(site_dir: Path, base_url: str) -> list[str]:
    """Check root-absolute URLs against the base path and absolute self-links against the site."""
    base_path = urlsplit(base_url).path
    errors: list[str] = []
    for html in sorted(site_dir.rglob("*.html")):
        text = html.read_text(encoding="utf-8", errors="replace")
        for raw in URL_ATTR_RE.findall(text):
            url = raw.strip("\"'")
            if url.startswith("/") and not url.startswith("//") and not url.startswith(base_path):
                errors.append(f"{html.relative_to(site_dir)}: root-absolute URL outside {base_path}: {url}")
            elif url.startswith(base_url):
                relative = urlsplit(url[len(base_url):]).path
                if relative and not _target_exists(site_dir, relative):
                    errors.append(f"{html.relative_to(site_dir)}: self-link without target: {url}")
    return errors


def check_site(site_dir: Path, pages: list[str] | None = None, base_url: str = "") -> list[str]:
    """Report missing nav pages, assets and search index, leaked files and bad links."""
    wanted = [page_html(p) for p in pages or []] + list(REQUIRED_SITE_FILES)
    errors = [f"missing from site: {name}" for name in wanted if not (site_dir / name).is_file()]
    errors += [
        f"must not be published: {path.relative_to(site_dir)}"
        for name in FORBIDDEN_SITE_NAMES
        for path in site_dir.rglob(name)
    ]
    try:
        index = json.loads((site_dir / "search.json").read_text(encoding="utf-8"))
        items = index.get("items") if isinstance(index, dict) else index
        if not items:
            errors.append("search index is empty")
    except (OSError, ValueError):
        errors.append("search index is missing or not valid JSON")
    if base_url:
        errors += check_links(site_dir, base_url)
    return errors


def main(argv: list[str] | None = None) -> int:
    print_header(SCRIPT, (__doc__ or "").strip().splitlines()[0], __author__, HELP_URL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("mkdocs.yml"))
    parser.add_argument("--docs-dir", type=Path, default=Path("docs"))
    parser.add_argument("--site-dir", type=Path, default=Path("site"))
    args = parser.parse_args(argv)
    try:
        config = args.config.read_text(encoding="utf-8")
        errors = check_nav(config, args.docs_dir)
        errors += check_site(args.site_dir, nav_pages(config), site_url(config))
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
