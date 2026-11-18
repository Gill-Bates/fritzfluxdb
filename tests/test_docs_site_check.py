#!/usr/bin/env python3
#
# tests/test_docs_site_check.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Nav validation and site smoke test of tools/docs_site_check.py."""

from pathlib import Path

import docs_site_check as dsc

ROOT = Path(__file__).resolve().parent.parent
NAV = "site_name: x\nnav:\n  - Home: index.md\n  - Group:\n      - A: a.md\n      - Changelog: changelog.md\n"


def make_docs(tmp_path, names):
    for name in names:
        (tmp_path / name).write_text("x")
    return tmp_path


def test_nav_pages_reads_nested_entries():
    assert dsc.nav_pages(NAV) == ["index.md", "a.md", "changelog.md"]


def test_nav_ok_with_generated_pages_absent(tmp_path):
    assert dsc.check_nav(NAV, make_docs(tmp_path, ["index.md", "a.md", "AGENTS.md"])) == []


def test_nav_reports_missing_and_omitted_pages(tmp_path):
    errors = dsc.check_nav(NAV, make_docs(tmp_path, ["index.md", "extra.md"]))
    assert "nav entry has no file: a.md" in errors
    assert "page is not in the nav: extra.md" in errors


def test_real_navigation_matches_docs_tree():
    text = (ROOT / "mkdocs.yml").read_text()
    assert dsc.check_nav(text, ROOT / "docs") == []


def test_site_check_reports_missing_and_leaked_files(tmp_path):
    for name in dsc.REQUIRED_SITE_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    assert dsc.check_site(tmp_path) == []
    (tmp_path / "search.json").unlink()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "mkdocs.yml").write_text("x")
    errors = dsc.check_site(tmp_path)
    assert "missing from site: search.json" in errors
    assert "must not be published: sub/mkdocs.yml" in errors
