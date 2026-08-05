"""Unit tests for the github-tempo timecard logic (pure parts, no live browser)."""

from __future__ import annotations

import datetime as dt

from webwalk.flows.github_tempo import timecard as tc


def test_key_extraction_matches_browse_links_in_project():
    html = (
        'blah <a href="https://jira.corp.clover.com/browse/BILLNA-8483">x</a> '
        'and <a href="https://jira.corp.clover.com/browse/SECREQ-4706">y</a>'
    )
    found = tc._KEY_RE.findall(html)
    assert found == ["BILLNA-8483", "SECREQ-4706"]
    assert tc._in_projects("BILLNA-8483") is True
    # SECREQ is not in the default tracked projects, so it's dropped downstream.
    assert tc._in_projects("SECREQ-4706") is False


def test_missing_periods_only_past_and_empty():
    statuses = [
        {"period": {"dateFrom": "2026-07-26", "dateTo": "2026-08-01"},
         "workedSeconds": 0, "requiredSeconds": 144000},                      # current -> excluded
        {"period": {"dateFrom": "2026-07-19", "dateTo": "2026-07-25"},
         "workedSeconds": 0, "requiredSeconds": 144000},                      # past + empty -> missing
        {"period": {"dateFrom": "2026-07-12", "dateTo": "2026-07-18"},
         "workedSeconds": 144000, "requiredSeconds": 144000},                 # already filled -> not missing
    ]
    miss = tc.missing_periods(statuses, today=dt.date(2026, 7, 26))
    assert miss == [("2026-07-19", "2026-07-25")]


def test_allocate_assigns_next_pr_per_day():
    prs = [
        {"pr": "/a/pull/1", "keys": ["BILLNA-100"], "merged": "2026-07-07T10:00:00Z"},
        {"pr": "/a/pull/2", "keys": ["BILLNA-200"], "merged": "2026-07-09T10:00:00Z"},
    ]
    # Mon-Wed working, others non-working (0 required seconds -> skipped).
    reqs = {
        "2026-07-06": 28800, "2026-07-07": 28800, "2026-07-08": 28800,
        "2026-07-09": 28800, "2026-07-10": 0,
    }
    plan = tc.allocate(prs, [("2026-07-06", "2026-07-10")], req_fn=lambda a, b: reqs)
    # Mon/Tue -> first PR (merged 7th); Wed/Thu -> next PR (merged 9th); Fri skipped.
    assert plan == {
        "2026-07-06": "BILLNA-100", "2026-07-07": "BILLNA-100",
        "2026-07-08": "BILLNA-200", "2026-07-09": "BILLNA-200",
    }
