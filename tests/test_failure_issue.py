#!/usr/bin/env python3
#
# tests/test_failure_issue.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Find, create and comment behaviour of tools/failure_issue.py with a faked gh."""

import json

import failure_issue as fi
import pytest

TITLE = "Security rescan failed"
URL = "https://github.com/o/r/actions/runs/42/attempts/2"


class FakeGh:
    """Answers issue list, view, create and comment calls like gh and records every call."""

    def __init__(self, issues=(), view=None):
        self.issues = list(issues)
        self.view = view or {"body": "", "comments": []}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps(self.issues)
        if args[:2] == ("issue", "view"):
            return json.dumps(self.view)
        if args[:2] == ("issue", "create"):
            return "https://github.com/o/r/issues/7\n"
        if args[:2] == ("issue", "comment"):
            return ""
        raise AssertionError(args)


@pytest.fixture
def actions_env(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Security rescan")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)


def install(monkeypatch, fake):
    monkeypatch.setattr(fi, "gh", fake)
    return fake


def test_creates_the_issue_when_none_is_open(actions_env, monkeypatch, capsys):
    fake = install(monkeypatch, FakeGh())
    assert fi.main(["--title", TITLE]) == 0
    create = [c for c in fake.calls if c[:2] == ("issue", "create")]
    assert len(create) == 1
    assert create[0][create[0].index("--title") + 1] == TITLE
    assert URL in create[0][create[0].index("--body") + 1]
    assert "action=created" in capsys.readouterr().out


def test_comments_on_the_existing_issue_with_the_exact_title(
    actions_env, monkeypatch, capsys
):
    issues = [
        {"number": 9, "title": TITLE + " (old)"},
        {"number": 5, "title": TITLE},
        {"number": 8, "title": TITLE},
    ]
    fake = install(monkeypatch, FakeGh(issues))
    assert fi.main(["--title", TITLE]) == 0
    comment = [c for c in fake.calls if c[:2] == ("issue", "comment")]
    assert len(comment) == 1
    assert comment[0][2] == "5"
    assert not [c for c in fake.calls if c[:2] == ("issue", "create")]
    assert "action=commented" in capsys.readouterr().out


class PagedGh(FakeGh):
    """Honours --limit and --search like gh; the search index may lag (no results)."""

    def __init__(self, issues, *, search_works=True):
        super().__init__(issues)
        self.search_works = search_works

    def __call__(self, *args):
        if args[:2] == ("issue", "list"):
            self.calls.append(args)
            found = self.issues
            if "--search" in args:
                needle = args[args.index("--search") + 1].split('"')[1]
                found = [i for i in found if needle in i["title"]] if self.search_works else []
            return json.dumps(found[: int(args[args.index("--limit") + 1])])
        return super().__call__(*args)


@pytest.mark.parametrize("search_works", [True, False])
def test_finds_the_tracking_issue_behind_more_than_200_open_issues(
    actions_env, monkeypatch, search_works
):
    issues = [{"number": n, "title": f"Unrelated {n}"} for n in range(1, 251)]
    issues.append({"number": 999, "title": TITLE})
    fake = install(monkeypatch, PagedGh(issues, search_works=search_works))
    assert fi.main(["--title", TITLE]) == 0
    assert not [c for c in fake.calls if c[:2] == ("issue", "create")]
    assert [c for c in fake.calls if c[:2] == ("issue", "comment")][0][2] == "999"


@pytest.mark.parametrize(
    "view",
    [
        {"body": f"Run: {URL}", "comments": []},
        {"body": "x", "comments": [{"body": f"again {URL}"}]},
    ],
)
def test_rerun_of_the_same_attempt_adds_nothing(actions_env, monkeypatch, capsys, view):
    fake = install(monkeypatch, FakeGh([{"number": 5, "title": TITLE}], view))
    assert fi.main(["--title", TITLE]) == 0
    assert not [c for c in fake.calls if c[:2] == ("issue", "comment")]
    assert "action=unchanged" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_REPOSITORY", "not a repo"),
        ("GITHUB_RUN_ID", "4x2"),
        ("GITHUB_SERVER_URL", "http://github.com"),
    ],
)
def test_malformed_environment_fails_before_any_gh_call(
    actions_env, monkeypatch, name, value
):
    fake = install(monkeypatch, FakeGh())
    monkeypatch.setenv(name, value)
    assert fi.main(["--title", TITLE]) == 1
    assert fake.calls == []


@pytest.mark.parametrize("title", ["", "  ", "two\nlines", "x" * 200])
def test_bad_titles_are_refused(actions_env, monkeypatch, title):
    fake = install(monkeypatch, FakeGh())
    assert fi.main(["--title", title]) == 1
    assert fake.calls == []


def test_gh_failure_is_an_error(actions_env, monkeypatch):
    def broken(*args):
        raise fi.IssueError("gh issue list failed: 403")

    monkeypatch.setattr(fi, "gh", broken)
    assert fi.main(["--title", TITLE]) == 1
