"""The timecard pipeline for the github-tempo flow.

This encodes the whole job so it's repeatable: read the pull requests merged on
corp GitHub over a window, pull the Jira key from each PR description, work out
one Jira per working day from the merge timeline, then write those worklogs into
Tempo — reopening (and restoring) any closed issue that refuses a worklog.

Everything here talks to the live browser session through the server's `/fetch`,
`/eval`, and `/step` endpoints (see webwalk/cli/_shared._call), so it runs with
the logged-in cookies for both hosts. Nothing here is interactive; the CLI
commands in cli.py (`prs` / `gaps` / `plan` / `fill`) are thin wrappers.

Config comes from the environment with sensible defaults, so a different user or
Jira project is a matter of exporting a few vars, not editing code:
    GH_USER          GitHub login          (default matt-rixman)
    JIRA_USER        Jira/Tempo user key   (default matt.rixman)
    TIMECARD_PROJECT Jira project prefix(es) to count, comma-sep (default BILLNA)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any

from webwalk.cli._shared import _call

GITHUB_HOST = os.environ.get("GITHUB_HOST", "github.corp.clover.com")
JIRA_HOST = os.environ.get("JIRA_HOST", "jira.corp.clover.com")
GH_USER = os.environ.get("GH_USER", "matt-rixman")
JIRA_USER = os.environ.get("JIRA_USER", "matt.rixman")
PROJECTS = [p.strip() for p in os.environ.get("TIMECARD_PROJECT", "BILLNA").split(",") if p.strip()]

TEMPO = f"https://{JIRA_HOST}/rest/tempo-timesheets/4"
JIRA = f"https://{JIRA_HOST}/rest/api/2"
FULL_DAY_SECONDS = 8 * 3600

# Grab every anchor whose href is a PR permalink (…/pull/<n>), deduped. Kept
# attribute-selector-free so it survives being passed as an --eval string.
_PR_HREF_JS = (
    "JSON.stringify(Array.from(new Set(Array.from(document.querySelectorAll('a'))"
    ".map(a=>a.getAttribute('href')).filter(h=>h&&/[/]pull[/][0-9]+$/.test(h)))))"
)
_KEY_RE = re.compile(rf"{re.escape(JIRA_HOST)}/browse/([A-Z][A-Z0-9]+-\d+)")


# --- transport ------------------------------------------------------------------


def _body(result: dict[str, Any]) -> Any:
    """Return a /fetch result's body as parsed JSON (it may already be parsed)."""
    b = result.get("body")
    if isinstance(b, (dict, list)):
        return b
    try:
        return json.loads(b) if b else None
    except (json.JSONDecodeError, TypeError):
        return b


def page_fetch(url: str, method: str = "GET", body: Any = None, page_auth: bool = False) -> dict[str, Any]:
    """Fire a request from inside the logged-in page (carries its session)."""
    return _call("/fetch", json={
        "url": url, "method": method,
        "headers": {"Content-Type": "application/json"} if body is not None else None,
        "body": json.dumps(body) if body is not None else None,
        "page_auth": page_auth, "frame": None,
    })


def navigate(url: str, settle: float = 2.0) -> None:
    """Point the browser at a URL and let a SPA render before we read it."""
    _call("/step", json={"action": "navigate", "value": url, "settle": settle})


def eval_js(script: str) -> Any:
    """Run JS in the page and return its (JSON-decoded) result."""
    res = _call("/eval", json={"script": script}).get("result")
    if isinstance(res, str):
        try:
            return json.loads(res)
        except json.JSONDecodeError:
            return res
    return res


# --- GitHub: merged PRs -> Jira keys -------------------------------------------


def _in_projects(key: str) -> bool:
    return any(key.startswith(p + "-") for p in PROJECTS)


def collect_prs(since: str, max_pages: int = 40) -> list[dict[str, Any]]:
    """Every PR you merged since `since` (YYYY-MM-DD) that carries a tracked Jira key.

    Drives the cross-repo PR search page by page (it's React-rendered, so we scrape
    the DOM rather than the API), then fetches each PR's HTML and pulls the Jira
    key(s) out of the description links. PRs with no tracked-project key are dropped.
    """
    import urllib.parse

    q = f"is:pr author:{GH_USER} is:merged merged:>={since}"
    seen: list[str] = []
    for page in range(1, max_pages + 1):
        url = f"https://{GITHUB_HOST}/pulls?q={urllib.parse.quote(q)}&page={page}"
        navigate(url)
        hrefs = eval_js(_PR_HREF_JS) or []
        new = [h for h in hrefs if h not in seen]
        if not new:
            break
        seen.extend(new)

    prs: list[dict[str, Any]] = []
    for href in seen:
        html = _body(page_fetch(f"https://{GITHUB_HOST}{href}")) or ""
        html = html if isinstance(html, str) else json.dumps(html)
        keys = sorted({k for k in _KEY_RE.findall(html) if _in_projects(k)})
        if not keys:
            continue
        merged = None
        mi = html.find("merged commit")
        if mi != -1:
            m = re.search(r'datetime="([^"]+)"', html[mi:mi + 2000])
            if m:
                merged = m.group(1)
        prs.append({"pr": href, "keys": keys, "merged": merged})
    return prs


# --- Tempo: periods, day requirements, worklogs --------------------------------


def period_statuses(num_periods: int = 6) -> list[dict[str, Any]]:
    url = f"{TEMPO}/timesheet-approval/approval-statuses?numberOfPeriods={num_periods}&userKey={JIRA_USER}"
    return _body(page_fetch(url)) or []


def day_requirements(date_from: str, date_to: str) -> dict[str, int]:
    """{date: requiredSeconds} for the range (0 on non-working days / holidays)."""
    data = _body(page_fetch(f"{TEMPO}/private/days/search", "POST",
                            {"from": date_from, "to": date_to, "userKeys": [JIRA_USER]})) or []
    out: dict[str, int] = {}
    for entry in data:
        for d in entry.get("days", []):
            out[d["date"]] = d.get("requiredSeconds", 0)
    return out


def worklogs(date_from: str, date_to: str) -> list[dict[str, Any]]:
    return _body(page_fetch(f"{TEMPO}/worklogs/search", "POST",
                            {"from": date_from, "to": date_to, "worker": [JIRA_USER]})) or []


def create_worklog(day: str, key: str, issue_id: int, seconds: int) -> dict[str, Any]:
    wl = {"worker": JIRA_USER, "started": f"{day} 00:00:00.000",
          "timeSpentSeconds": seconds, "billableSeconds": seconds,
          "comment": f"Working on issue {key}", "originTaskId": issue_id,
          "includeNonWorkingDays": False, "attributes": {}}
    return page_fetch(f"{TEMPO}/worklogs/", "POST", wl, page_auth=True)


# --- Jira: issue lookup + workflow transitions ---------------------------------


def issue_meta(key: str) -> dict[str, Any]:
    o = _body(page_fetch(f"{JIRA}/issue/{key}?fields=status,resolution,summary")) or {}
    f = o.get("fields", {})
    return {"id": int(o["id"]) if o.get("id") else None,
            "status": (f.get("status") or {}).get("name"),
            "resolution": (f.get("resolution") or {}).get("name"),
            "summary": f.get("summary", "")}


def transitions(key: str) -> list[dict[str, Any]]:
    o = _body(page_fetch(f"{JIRA}/issue/{key}/transitions?expand=transitions.fields")) or {}
    return o.get("transitions", [])


def do_transition(key: str, transition_id: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"transition": {"id": str(transition_id)}}
    if fields:
        payload["fields"] = fields
    return page_fetch(f"{JIRA}/issue/{key}/transitions", "POST", payload)


_OPEN_TARGETS = ("reopened", "in progress", "open", "to do")


def _reopen(key: str) -> str | None:
    """Move a closed issue to a status that permits worklogs. Returns target name."""
    for t in transitions(key):
        if "reopen" in t["name"].lower():
            do_transition(key, t["id"])
            return t.get("to", {}).get("name")
    for t in transitions(key):
        if (t.get("to", {}).get("name") or "").lower() in _OPEN_TARGETS:
            do_transition(key, t["id"])
            return t.get("to", {}).get("name")
    return None


def _restore(key: str, status_name: str, resolution: str | None) -> bool:
    """Transition an issue back to `status_name`, re-supplying resolution if required."""
    for t in transitions(key):
        if (t.get("to", {}).get("name") or "") == status_name:
            fields = {}
            needs_res = (t.get("fields") or {}).get("resolution", {}).get("required")
            if needs_res and resolution:
                fields = {"resolution": {"name": resolution}}
            do_transition(key, t["id"], fields or None)
            return True
    return False


# --- allocation: one Jira per working day --------------------------------------


def _dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def missing_periods(statuses: list[dict[str, Any]], today: dt.date | None = None) -> list[tuple[str, str]]:
    """Fully-past periods with required hours but nothing logged yet."""
    today = today or dt.date.today()
    out = []
    for p in statuses:
        per = p["period"]
        if (p.get("workedSeconds", 0) == 0 and p.get("requiredSeconds", 0) > 0
                and dt.date.fromisoformat(per["dateTo"]) < today):
            out.append((per["dateFrom"], per["dateTo"]))
    return out


def allocate(prs: list[dict[str, Any]], periods: list[tuple[str, str]],
             req_fn: Any = day_requirements) -> dict[str, str]:
    """{date: jira_key} for each required working day, from the PR merge timeline.

    A day is attributed to the Jira of the next PR merged on/after it — the work
    you were doing in the run-up to that merge. Days with no PR on/after are left
    out. `req_fn(from, to) -> {date: requiredSeconds}` is injectable for testing.
    """
    timeline = sorted(
        ((_dt(p["merged"]).date(), p["keys"][0]) for p in prs if p.get("merged") and p.get("keys")),
        key=lambda x: x[0],
    )
    plan: dict[str, str] = {}
    for date_from, date_to in periods:
        for day, req in req_fn(date_from, date_to).items():
            if req <= 0:
                continue
            d = dt.date.fromisoformat(day)
            nxt = next((key for mdate, key in timeline if mdate >= d), None)
            if nxt:
                plan[day] = nxt
    return dict(sorted(plan.items()))


# --- fill: write the plan, reopening closed issues as needed -------------------


def _workflow_403(result: dict[str, Any]) -> bool:
    return result.get("status") == 403


def fill(plan: dict[str, str], dry_run: bool = False) -> list[dict[str, Any]]:
    """Create a worklog per planned day, grouping by issue so a closed one is
    reopened once, logged, and restored to its original status/resolution."""
    by_key: dict[str, list[str]] = {}
    for day, key in plan.items():
        by_key.setdefault(key, []).append(day)

    results: list[dict[str, Any]] = []
    for key, days in by_key.items():
        meta = issue_meta(key)
        reqs = {}
        for day in days:
            reqs[day] = day_requirements(day, day).get(day, FULL_DAY_SECONDS) or FULL_DAY_SECONDS
        if dry_run:
            for day in days:
                results.append({"day": day, "key": key, "seconds": reqs[day], "action": "would-log"})
            continue

        attempts = {day: create_worklog(day, key, meta["id"], reqs[day]) for day in days}
        failed = [day for day, r in attempts.items() if not r.get("ok") and _workflow_403(r)]
        reopened = False
        if failed:
            reopened = bool(_reopen(key))
            for day in failed:
                attempts[day] = create_worklog(day, key, meta["id"], reqs[day])
            if reopened:
                _restore(key, meta["status"], meta["resolution"])
        for day, r in attempts.items():
            b = _body(r)
            wid = b[0].get("tempoWorklogId") if isinstance(b, list) and b else None
            results.append({"day": day, "key": key, "ok": bool(r.get("ok")),
                            "status": r.get("status"), "worklogId": wid,
                            "reopened": reopened if day in failed else False})
    return results


def submit_period(date_from: str) -> dict[str, Any]:
    """Submit a period for approval (only when explicitly asked)."""
    return page_fetch(f"{TEMPO}/timesheet-approval/", "POST",
                      {"dateFrom": date_from, "user": JIRA_USER, "action": "submit"})
