#!/usr/bin/env python3
"""
cleanup_reports.py — delete report directories older than RETENTION_DAYS.
Runs as a k8s CronJob against the shared PVC at REPORTS_DIR.
"""
import os
import shutil
import time
from pathlib import Path

REPORTS_DIR = os.environ.get("REPORTS_DIR", "/reports")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

cutoff = time.time() - RETENTION_DAYS * 86400
base = Path(REPORTS_DIR)

deleted = 0
if base.exists():
    for sub_dir in base.iterdir():
        if not sub_dir.is_dir():
            continue
        for build_dir in sub_dir.iterdir():
            if not build_dir.is_dir():
                continue
            report_html = build_dir / "report.html"
            ref = report_html if report_html.exists() else build_dir
            if ref.stat().st_mtime < cutoff:
                shutil.rmtree(build_dir)
                print(f"Deleted: {build_dir}")
                deleted += 1
        # remove empty subscription directory
        if not any(sub_dir.iterdir()):
            sub_dir.rmdir()
            print(f"Removed empty dir: {sub_dir}")

print(f"Cleanup done: {deleted} build report(s) deleted (retention={RETENTION_DAYS}d)")
