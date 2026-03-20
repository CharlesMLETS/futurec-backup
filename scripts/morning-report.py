#!/usr/bin/env python3
"""Morning task report — fetches undone tasks from Vikunja and generates a
prioritized daily briefing.

Reads VIKUNJA_URL and VIKUNJA_TOKEN from config/vikunja.env (relative to the
OpenClaw base directory).  Outputs a Markdown report to stdout and saves it
to workspace/reports/YYYY-MM-DD.md.  Maintains a skip tracker at
workspace/reports/.task-tracker.json.

Usage (on VM-150 as openclaw):
    python3 ~/.openclaw/scripts/morning-report.py
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone, date

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZERO_DATE_PREFIX = "0001-01-01"
PRIORITY_LABELS = {0: "—", 1: "Low", 2: "Medium", 3: "High", 4: "Urgent", 5: "DO NOW"}
SKIP_THRESHOLD = 5
PER_PAGE = 50  # Vikunja default page size

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def find_base_dir():
    """Locate the OpenClaw base directory (.openclaw/).

    Strategy: walk up from script location looking for the directory that
    contains config/, workspace/, and scripts/.  Falls back to
    ~/.openclaw if nothing matches.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # scripts/ lives directly inside .openclaw/
    candidate = os.path.abspath(os.path.join(script_dir, ".."))
    if os.path.isdir(os.path.join(candidate, "config")) or os.path.isdir(
        os.path.join(candidate, "workspace")
    ):
        return candidate
    return os.path.expanduser("~/.openclaw")


def load_config(base_dir):
    """Read config/vikunja.env and return (base_url, token)."""
    env_path = os.path.join(base_dir, "config", "vikunja.env")
    if not os.path.exists(env_path):
        print(f"ERROR: Config not found at {env_path}", file=sys.stderr)
        sys.exit(1)

    env = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")

    base_url = env.get("VIKUNJA_URL", "").rstrip("/")
    token = env.get("VIKUNJA_TOKEN", "")

    if not base_url:
        print("ERROR: VIKUNJA_URL not set in vikunja.env", file=sys.stderr)
        sys.exit(1)
    if not token:
        print("ERROR: VIKUNJA_TOKEN not set in vikunja.env", file=sys.stderr)
        sys.exit(1)

    return base_url, token


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def api_get(url, token):
    """Authenticated GET, returns parsed JSON."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason} — {url}\n{body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {url} — {e.reason}")


def fetch_all_projects(base_url, token):
    """Return list of {id, title} for all real projects (positive IDs)."""
    data = api_get(f"{base_url}/api/v1/projects", token)
    projects = []
    for p in data:
        pid = p.get("id", 0)
        if pid > 0:
            projects.append({"id": pid, "title": p.get("title", f"Project {pid}")})
    return projects


def fetch_project_tasks(base_url, token, project_id):
    """Fetch all tasks for a project, handling pagination."""
    tasks = []
    page = 1
    while True:
        url = f"{base_url}/api/v1/projects/{project_id}/tasks?page={page}&per_page={PER_PAGE}"
        batch = api_get(url, token)
        if not batch:
            break
        tasks.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return tasks


def fetch_all_undone_tasks(base_url, token):
    """Iterate all projects, collect undone tasks with project name attached."""
    projects = fetch_all_projects(base_url, token)
    all_tasks = []
    errors = []

    for proj in projects:
        try:
            tasks = fetch_project_tasks(base_url, token, proj["id"])
            for t in tasks:
                if not t.get("done", False):
                    t["_project_name"] = proj["title"]
                    all_tasks.append(t)
        except RuntimeError as e:
            errors.append(f"Project '{proj['title']}' (ID {proj['id']}): {e}")

    return all_tasks, errors


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def parse_date(iso_string):
    """Parse an ISO 8601 date string.  Returns a date object or None for
    zero-value dates (0001-01-01)."""
    if not iso_string or iso_string.startswith(ZERO_DATE_PREFIX):
        return None
    try:
        # Handle timezone offsets — strip to just the date portion for
        # comparison purposes.
        clean = iso_string.replace("Z", "+00:00")
        # Python 3.7+ fromisoformat handles most ISO strings.
        dt = datetime.fromisoformat(clean)
        return dt.date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def section_due_soonest(tasks, today, limit=5):
    """Section 1: tasks with due dates, sorted soonest first."""
    candidates = []
    for t in tasks:
        due = parse_date(t.get("due_date", ""))
        if due is not None:
            delta = (due - today).days
            candidates.append({
                "id": t["id"],
                "title": t.get("title", "Untitled"),
                "project": t.get("_project_name", "?"),
                "due_date": due.isoformat(),
                "days_left": delta,
                "overdue": delta < 0,
                "priority": t.get("priority", 0),
            })
    candidates.sort(key=lambda x: x["days_left"])
    return candidates[:limit]


def section_high_priority(tasks, limit=5):
    """Section 2: high-priority (>= 3) tasks, most recently created first."""
    candidates = []
    for t in tasks:
        pri = t.get("priority", 0)
        if pri >= 3:
            created = parse_date(t.get("created", ""))
            candidates.append({
                "id": t["id"],
                "title": t.get("title", "Untitled"),
                "project": t.get("_project_name", "?"),
                "created": t.get("created", "")[:10],
                "priority": pri,
            })
    candidates.sort(key=lambda x: x["created"], reverse=True)
    return candidates[:limit]


def section_gantt_active(tasks, today):
    """Section 3: tasks where today is between start_date and end_date."""
    results = []
    for t in tasks:
        start = parse_date(t.get("start_date", ""))
        end = parse_date(t.get("end_date", ""))
        if start is not None and end is not None and start <= today <= end:
            days_remaining = (end - today).days
            results.append({
                "id": t["id"],
                "title": t.get("title", "Untitled"),
                "project": t.get("_project_name", "?"),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "days_remaining": days_remaining,
                "priority": t.get("priority", 0),
            })
    return results


# ---------------------------------------------------------------------------
# Skip tracker
# ---------------------------------------------------------------------------


def load_tracker(path):
    """Load the skip tracker JSON.  Returns a fresh tracker on any error."""
    try:
        with open(path) as f:
            data = json.load(f)
            if "tasks" in data:
                return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return {"last_updated": "", "tasks": {}}


def save_tracker(tracker, path):
    """Persist the skip tracker."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(tracker, f, indent=2)


def update_tracker(tracker, current_tasks, today):
    """Increment counts for tasks present in this report; remove absent ones.

    current_tasks is a dict {str(task_id): {title, project, priority}}.
    """
    old_ids = set(tracker["tasks"].keys())
    new_ids = set(current_tasks.keys())

    # Remove tasks no longer qualifying
    for tid in old_ids - new_ids:
        del tracker["tasks"][tid]

    # Increment / add tasks
    for tid in new_ids:
        info = current_tasks[tid]
        if tid in tracker["tasks"]:
            tracker["tasks"][tid]["count"] += 1
            tracker["tasks"][tid]["title"] = info["title"]
            tracker["tasks"][tid]["project"] = info["project"]
            tracker["tasks"][tid]["priority"] = info["priority"]
        else:
            tracker["tasks"][tid] = {
                "title": info["title"],
                "project": info["project"],
                "count": 1,
                "first_seen": today.isoformat(),
                "priority": info["priority"],
            }

    tracker["last_updated"] = datetime.now().isoformat(timespec="seconds")
    return tracker


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report(s1, s2, s3, tracker, today, errors):
    """Build the Markdown report string."""
    lines = [f"# Morning Task Report — {today.isoformat()}", ""]

    # Section 1: Upcoming Deadlines
    lines.append("## 1. Upcoming Deadlines")
    if s1:
        lines.append("")
        lines.append("| Task | Project | Due | Days Left | Priority | Seen |")
        lines.append("|------|---------|-----|-----------|----------|------|")
        for t in s1:
            tid = str(t["id"])
            skip = tracker["tasks"].get(tid, {}).get("count", 0)
            if t["overdue"]:
                days_str = f"**{abs(t['days_left'])}d overdue**"
            elif t["days_left"] == 0:
                days_str = "**TODAY**"
            else:
                days_str = f"{t['days_left']}d"
            pri = PRIORITY_LABELS.get(t["priority"], "?")
            lines.append(f"| {t['title']} | {t['project']} | {t['due_date']} | {days_str} | {pri} | {skip} |")
    else:
        lines.append("\nNo tasks with upcoming due dates.")
    lines.append("")

    # Section 2: High-Priority Recent
    lines.append("## 2. High-Priority Recent")
    if s2:
        lines.append("")
        lines.append("| Task | Project | Created | Priority | Seen |")
        lines.append("|------|---------|---------|----------|------|")
        for t in s2:
            tid = str(t["id"])
            skip = tracker["tasks"].get(tid, {}).get("count", 0)
            pri = PRIORITY_LABELS.get(t["priority"], "?")
            lines.append(f"| {t['title']} | {t['project']} | {t['created']} | {pri} | {skip} |")
    else:
        lines.append("\nNo high-priority tasks (priority >= High).")
    lines.append("")

    # Section 3: Active Gantt Tasks
    lines.append("## 3. Active Gantt Tasks")
    if s3:
        lines.append("")
        lines.append("| Task | Project | Start | End | Days Left | Seen |")
        lines.append("|------|---------|-------|-----|-----------|------|")
        for t in s3:
            tid = str(t["id"])
            skip = tracker["tasks"].get(tid, {}).get("count", 0)
            lines.append(
                f"| {t['title']} | {t['project']} | {t['start_date']} | "
                f"{t['end_date']} | {t['days_remaining']}d | {skip} |"
            )
    else:
        lines.append("\nNo tasks currently in gantt range.")
    lines.append("")

    # Section 4: Repeatedly Skipped (conditional)
    skipped = [
        (tid, info)
        for tid, info in tracker["tasks"].items()
        if info["count"] >= SKIP_THRESHOLD
    ]
    if skipped:
        skipped.sort(key=lambda x: x[1]["count"], reverse=True)
        lines.append("## 4. Repeatedly Skipped")
        lines.append("")
        lines.append("| Task | Project | Reports | First Seen | Priority |")
        lines.append("|------|---------|---------|------------|----------|")
        for tid, info in skipped:
            pri = PRIORITY_LABELS.get(info["priority"], "?")
            lines.append(
                f"| {info['title']} | {info['project']} | "
                f"{info['count']} | {info['first_seen']} | {pri} |"
            )
        lines.append("")

    # Footer
    if errors:
        lines.append("---")
        lines.append("**Warnings:**")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    base_dir = find_base_dir()
    base_url, token = load_config(base_dir)

    today = date.today()
    reports_dir = os.path.join(base_dir, "workspace", "reports")
    tracker_path = os.path.join(reports_dir, ".task-tracker.json")
    report_path = os.path.join(reports_dir, f"{today.isoformat()}.md")

    # Fetch tasks
    try:
        all_tasks, errors = fetch_all_undone_tasks(base_url, token)
    except RuntimeError as e:
        print(f"ERROR: Vikunja API unreachable — {e}", file=sys.stderr)
        # Write minimal failure report
        fail_report = f"# Morning Task Report — {today.isoformat()}\n\nVikunja API unreachable. Check CT-163 status.\n"
        os.makedirs(reports_dir, exist_ok=True)
        with open(report_path, "w") as f:
            f.write(fail_report)
        print(fail_report)
        sys.exit(1)

    # Build sections
    s1 = section_due_soonest(all_tasks, today)
    s2 = section_high_priority(all_tasks)
    s3 = section_gantt_active(all_tasks, today)

    # Build set of all task IDs appearing in any section
    current_tasks = {}
    for section in (s1, s2, s3):
        for t in section:
            tid = str(t["id"])
            if tid not in current_tasks:
                current_tasks[tid] = {
                    "title": t["title"],
                    "project": t["project"],
                    "priority": t.get("priority", 0),
                }

    # Update skip tracker
    tracker = load_tracker(tracker_path)
    tracker = update_tracker(tracker, current_tasks, today)
    save_tracker(tracker, tracker_path)

    # Format and save report
    report = format_report(s1, s2, s3, tracker, today, errors)
    os.makedirs(reports_dir, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(report)

    # Print to stdout for FutureC to read
    print(report)


if __name__ == "__main__":
    main()
