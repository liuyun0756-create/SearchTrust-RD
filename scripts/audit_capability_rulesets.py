#!/usr/bin/env python3
"""P0-02 read-only inventory via the existing linked Supabase CLI session.

No credentials, report bodies, customer identities or share tokens are printed.
Any access/query/shape failure produces not_checked and a nonzero exit, never zeros.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess


SQL_PATH = Path(__file__).with_suffix(".sql")
COUNT_FIELDS = (
    "report_count", "completed_reports", "ever_approved_reports", "latest_approved_reports",
    "deliverable_approved_reports", "shared_reports", "share_count", "active_share_reports",
)


class AuditError(Exception):
    pass


def parse_inventory(output: str) -> dict:
    try:
        result = json.loads(output)
        rows = result if isinstance(result, list) else result["rows"]
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError
        value = rows[0]["inventory"]
        if value["transaction_read_only"] != "on":
            raise ValueError
        if datetime.fromisoformat(value["observed_at"]).tzinfo is None:
            raise ValueError
        if type(value["report_count"]) is not int or value["report_count"] < 0:
            raise ValueError
        if not isinstance(value["groups"], list):
            raise ValueError
        versions = set()
        for group in value["groups"]:
            if set(group) != {"ruleset_version", *COUNT_FIELDS}:
                raise ValueError
            version = group["ruleset_version"]
            if version is not None and (not isinstance(version, str) or not version.strip()):
                raise ValueError
            if version in versions:
                raise ValueError
            versions.add(version)
            if any(type(group[k]) is not int or group[k] < 0 for k in COUNT_FIELDS):
                raise ValueError
            if any(group[k] > group["report_count"] for k in COUNT_FIELDS if k != "share_count"):
                raise ValueError
            if not (group["deliverable_approved_reports"] <= group["latest_approved_reports"] <= group["ever_approved_reports"]
                    and group["active_share_reports"] <= group["shared_reports"] <= group["share_count"]
                    and group["deliverable_approved_reports"] <= group["completed_reports"]
                    and group["active_share_reports"] <= group["completed_reports"]):
                raise ValueError
        if sum(g["report_count"] for g in value["groups"]) != value["report_count"]:
            raise ValueError
        if set(value) != {"observed_at", "transaction_read_only", "report_count", "groups"}:
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AuditError("INVENTORY_RESULT_INVALID") from None


def audit(frontend_dir: Path, project_ref: str) -> dict:
    try:
        linked = (frontend_dir / "supabase/.temp/project-ref").read_text().strip()
    except OSError:
        raise AuditError("LINKED_PROJECT_UNAVAILABLE") from None
    if not project_ref or linked != project_ref:
        raise AuditError("LINKED_PROJECT_MISMATCH")
    try:
        result = subprocess.run(
            ["npx", "--no-install", "supabase", "db", "query", "--linked", "--file", str(SQL_PATH), "-o", "json"],
            cwd=frontend_dir, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AuditError("INVENTORY_QUERY_UNAVAILABLE") from None
    if result.returncode:
        # CLI errors may contain connection details; do not echo stdout/stderr.
        raise AuditError("INVENTORY_QUERY_FAILED")
    return {"status": "ok", "project_ref": project_ref, **parse_inventory(result.stdout)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-dir", type=Path,
                        default=Path(__file__).resolve().parents[2] / "search-trust")
    parser.add_argument("--project-ref", required=True, help="Must match the already linked project.")
    args = parser.parse_args(argv)
    try:
        result = audit(args.frontend_dir, args.project_ref)
    except AuditError as exc:
        print(json.dumps({"status": "not_checked", "error_code": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
