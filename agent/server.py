import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse, HTMLResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

REPORTS_DIR = os.environ.get("REPORTS_DIR", "/reports")
POLL_SUBSCRIPTIONS = os.environ.get("POLL_SUBSCRIPTIONS", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))

_seen_failed: set = set()

_TEMPLATES = Path(__file__).parent / "templates"


async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def trigger_analysis(request: Request):
    """Manual trigger: POST /analyze {subscription_code, build_code}"""
    try:
        body = await request.json()
        subscription_code = body.get("subscription_code", "")
        build_code = body.get("build_code", "")
        if not subscription_code or not build_code:
            return JSONResponse({"error": "subscription_code and build_code required"}, status_code=400)

        def _run():
            from analyzer import run_analysis
            return run_analysis(subscription_code, build_code)

        result = await asyncio.to_thread(_run)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        logger.exception("Analysis failed")
        return JSONResponse({"error": str(e)}, status_code=500)


def _collect_reports() -> dict:
    """Scan REPORTS_DIR and return {subscription_code: [build_entry, ...]} sorted newest first."""
    import html as H
    base = Path(REPORTS_DIR)
    tenants: dict[str, list] = {}
    if not base.exists():
        return tenants
    for sub_dir in sorted(base.iterdir()):
        if not sub_dir.is_dir():
            continue
        builds = []
        for build_dir in sorted(sub_dir.iterdir(), reverse=True):
            if not build_dir.is_dir():
                continue
            report_html = build_dir / "report.html"
            analysis_json = build_dir / "analysis.json"
            entry = {
                "build_code": build_dir.name,
                "report_url": f"/files/{sub_dir.name}/{build_dir.name}/report.html",
                "build_result": None,
                "kba_count": 0,
                "generated_at": None,
            }
            if analysis_json.exists():
                try:
                    data = json.loads(analysis_json.read_text())
                    entry["build_result"] = data.get("build_result")
                    entry["kba_count"] = len(data.get("kba_matches", []))
                except Exception:
                    pass
            if report_html.exists():
                mtime = report_html.stat().st_mtime
                entry["generated_at"] = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            builds.append(entry)
        if builds:
            tenants[sub_dir.name] = builds
    return tenants


async def reports_index(request: Request):
    """Render the HTML reports index page."""
    import html as H
    tenants = _collect_reports()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_reports = sum(len(v) for v in tenants.values())
    total_tenants = len(tenants)
    total_failed = sum(
        1 for builds in tenants.values()
        for b in builds if b.get("build_result") and "FAIL" in b.get("build_result", "")
    )

    if not tenants:
        tenant_sections = '<div class="empty-state">No reports yet. Trigger an analysis with POST /analyze.</div>'
    else:
        sections = []
        for i, (sub, builds) in enumerate(tenants.items()):
            rows = []
            for b in builds:
                result = b.get("build_result") or "UNKNOWN"
                if "FAIL" in result:
                    badge = f'<span class="badge badge-failed">{H.escape(result.replace("_"," "))}</span>'
                elif result == "SUCCESSFUL":
                    badge = '<span class="badge badge-success">SUCCESSFUL</span>'
                else:
                    badge = f'<span class="badge badge-unknown">{H.escape(result)}</span>'
                link = f'<a href="{H.escape(b["report_url"])}">View Report</a>'
                rows.append(
                    f'<tr><td>{H.escape(b["build_code"])}</td>'
                    f'<td>{badge}</td>'
                    f'<td>{H.escape(b.get("generated_at") or "—")}</td>'
                    f'<td>{b.get("kba_count", 0)}</td>'
                    f'<td>{link}</td></tr>'
                )
            table = (
                '<div class="builds-table-wrap"><table>'
                '<thead><tr><th>Build</th><th>Result</th><th>Generated</th><th>KBAs</th><th>Report</th></tr></thead>'
                '<tbody>' + "".join(rows) + '</tbody></table></div>'
            )
            tid = f"tenant-{i}"
            sections.append(
                f'<div class="tenant-section" id="{tid}">'
                f'<div class="tenant-header" onclick="toggleTenant(\'{tid}\')">'
                f'<span class="tenant-name">{H.escape(sub)}</span>'
                f'<span class="tenant-count">{len(builds)} build{"s" if len(builds)!=1 else ""}</span>'
                f'<span class="chevron"></span>'
                f'</div>{table}</div>'
            )
        tenant_sections = "\n".join(sections)

    template = (_TEMPLATES / "reports_index.html").read_text(encoding="utf-8")
    html = template
    for k, v in {
        "{{TIMESTAMP}}": timestamp,
        "{{TOTAL_REPORTS}}": str(total_reports),
        "{{TOTAL_TENANTS}}": str(total_tenants),
        "{{TOTAL_FAILED}}": str(total_failed),
        "{{TENANT_SECTIONS_HTML}}": tenant_sections,
    }.items():
        html = html.replace(k, v)
    return HTMLResponse(html)


async def reports_json(request: Request):
    """JSON API: list all reports."""
    tenants = _collect_reports()
    result = []
    for sub, builds in tenants.items():
        for b in builds:
            result.append({"subscription_code": sub, **b})
    return JSONResponse(result)


def _poll_once():
    """Check subscriptions for new FAILED builds and trigger analysis."""
    if not POLL_SUBSCRIPTIONS:
        return
    from portal_client import list_builds
    from analyzer import run_analysis

    for sub in POLL_SUBSCRIPTIONS.split(","):
        sub = sub.strip()
        if not sub:
            continue
        try:
            builds = list_builds(sub, status="FAILED")
            for b in builds:
                key = f"{sub}/{b.get('code','')}"
                if key in _seen_failed:
                    continue
                _seen_failed.add(key)
                logger.info(f"New FAILED build detected: {key}")
                try:
                    run_analysis(sub, b["code"])
                except Exception as e:
                    logger.error(f"Analysis failed for {key}: {e}")
        except Exception as e:
            logger.error(f"Poll failed for {sub}: {e}")


async def _poll_loop():
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        await asyncio.to_thread(_poll_once)


def create_app():
    reports_path = Path(REPORTS_DIR)
    reports_path.mkdir(parents=True, exist_ok=True)

    routes = [
        Route("/health", health),
        Route("/analyze", trigger_analysis, methods=["POST"]),
        Route("/reports", reports_index),
        Route("/api/reports", reports_json),
        Mount("/files", app=StaticFiles(directory=str(reports_path), html=True)),
    ]

    app = Starlette(routes=routes, on_startup=[lambda: asyncio.create_task(_poll_loop())])
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8080)

