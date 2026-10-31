import asyncio
import json
import logging
import os
import sys
import uuid
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
MAX_CONCURRENT_ANALYSES = int(os.environ.get("MAX_CONCURRENT_ANALYSES", "3"))

_seen_failed: set = set()
_TEMPLATES = Path(__file__).parent / "templates"

# Job queue: job_id -> {status, subscription_code, build_code, report_url, error, queued_at}
_jobs: dict = {}
_analysis_semaphore: asyncio.Semaphore | None = None  # init in create_app


async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def _run_analysis_job(job_id: str, subscription_code: str, build_code: str):
    """Background task: run analysis with concurrency limit, update _jobs in place."""
    global _analysis_semaphore
    _jobs[job_id]["status"] = "queued"
    async with _analysis_semaphore:
        _jobs[job_id]["status"] = "running"
        logger.info(f"[job:{job_id}] starting analysis {subscription_code}/{build_code}")
        try:
            def _run():
                from analyzer import run_analysis
                return run_analysis(subscription_code, build_code)
            result = await asyncio.to_thread(_run)
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["report_url"] = f"/files/{subscription_code}/{build_code}/report.html"
            _jobs[job_id]["result"] = result
            logger.info(f"[job:{job_id}] done")
        except Exception as e:
            logger.exception(f"[job:{job_id}] failed")
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(e)


async def trigger_analysis(request: Request):
    """POST /analyze — enqueue analysis, return job_id immediately."""
    try:
        body = await request.json()
        subscription_code = body.get("subscription_code", "")
        build_code = body.get("build_code", "")
        if not subscription_code or not build_code:
            return JSONResponse({"error": "subscription_code and build_code required"}, status_code=400)

        job_id = str(uuid.uuid4())[:8]
        _jobs[job_id] = {
            "status": "queued",
            "subscription_code": subscription_code,
            "build_code": build_code,
            "report_url": None,
            "error": None,
            "queued_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        asyncio.create_task(_run_analysis_job(job_id, subscription_code, build_code))
        return JSONResponse({"ok": True, "job_id": job_id, "status": "queued"})
    except Exception as e:
        logger.exception("Failed to enqueue analysis")
        return JSONResponse({"error": str(e)}, status_code=500)


async def analysis_status(request: Request):
    """GET /analyze/status/{job_id}"""
    job_id = request.path_params["job_id"]
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse({"job_id": job_id, **job})


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
            logger.info(f"Poll {sub}: {len(builds)} FAILED build(s) found, sample={builds[:1]}")
            for b in builds:
                # Portal API may return 'code' or 'buildCode'
                build_code = b.get("code") or b.get("buildCode") or b.get("build_code") or ""
                if not build_code:
                    logger.warning(f"Could not extract build code from: {b}")
                    continue
                key = f"{sub}/{build_code}"
                if key in _seen_failed:
                    continue
                _seen_failed.add(key)
                logger.info(f"New FAILED build detected: {key}")
                try:
                    run_analysis(sub, build_code)
                except Exception as e:
                    logger.error(f"Analysis failed for {key}: {e}")
        except Exception as e:
            logger.error(f"Poll failed for {sub}: {e}")


async def _poll_loop():
    # poll once immediately on startup, then every POLL_INTERVAL seconds
    await asyncio.to_thread(_poll_once)
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        await asyncio.to_thread(_poll_once)


def create_app():
    global _analysis_semaphore
    _analysis_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ANALYSES)

    reports_path = Path(REPORTS_DIR)
    reports_path.mkdir(parents=True, exist_ok=True)

    routes = [
        Route("/health", health),
        Route("/analyze", trigger_analysis, methods=["POST"]),
        Route("/analyze/status/{job_id}", analysis_status),
        Route("/reports", reports_index),
        Route("/api/reports", reports_json),
        Mount("/files", app=StaticFiles(directory=str(reports_path), html=True)),
    ]

    app = Starlette(routes=routes, on_startup=[lambda: asyncio.create_task(_poll_loop())])
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8080)

