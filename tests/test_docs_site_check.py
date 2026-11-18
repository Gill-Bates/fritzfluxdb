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


def make_site(tmp_path, pages=("index.md", "a.md")):
    for name in [dsc.page_html(p) for p in pages] + list(dsc.REQUIRED_SITE_FILES):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"items": [{"t": 1}]}' if name == "search.json" else "<html></html>")
    return list(pages)


def test_page_html_uses_directory_urls():
    assert dsc.page_html("index.md") == "index.html"
    assert dsc.page_html("a/b.md") == "a/b/index.html"


def test_site_check_reports_missing_leaked_and_empty(tmp_path):
    pages = make_site(tmp_path)
    assert dsc.check_site(tmp_path, pages) == []
    (tmp_path / "a" / "index.html").unlink()
    (tmp_path / "search.json").write_text('{"items": []}')
    for leaked in (tmp_path / "sub" / "mkdocs.yml", tmp_path / ".env"):
        leaked.parent.mkdir(exist_ok=True)
        leaked.write_text("x")
    errors = dsc.check_site(tmp_path, pages)
    assert "missing from site: a/index.html" in errors
    assert "search index is empty" in errors
    assert "must not be published: sub/mkdocs.yml" in errors
    assert "must not be published: .env" in errors


def test_site_check_rejects_unparseable_search_index(tmp_path):
    pages = make_site(tmp_path)
    (tmp_path / "search.json").write_text("{")
    assert "search index is missing or not valid JSON" in dsc.check_site(tmp_path, pages)


def test_links_honour_base_path_and_self_links(tmp_path):
    base = "https://example.org/proj/"
    pages = make_site(tmp_path)
    ok = f'<a href=/proj/x>a</a><link rel=canonical href={base}a/><a href=./a/>b</a>'
    (tmp_path / "index.html").write_text(ok)
    assert dsc.check_site(tmp_path, pages, base) == []
    (tmp_path / "index.html").write_text(f'<a href="/other/x">a</a><a href="{base}gone/">b</a>')
    errors = dsc.check_site(tmp_path, pages, base)
    assert any("outside /proj/" in e for e in errors)
    assert any("self-link without target" in e for e in errors)


def test_site_url_reads_configured_value():
    assert dsc.site_url("site_url: https://x.org/p\n") == "https://x.org/p/"
    assert dsc.site_url("site_name: x\n") == ""
