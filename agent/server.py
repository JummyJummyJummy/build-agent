import asyncio
import json
import logging
import os
import sys
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
POLL_SUBSCRIPTIONS = os.environ.get("POLL_SUBSCRIPTIONS", "")  # comma-separated
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))  # seconds

_seen_failed: set = set()


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


async def list_reports(request: Request):
    """List available reports."""
    reports = []
    base = Path(REPORTS_DIR)
    if base.exists():
        for sub_dir in sorted(base.iterdir()):
            for build_dir in sorted(sub_dir.iterdir()):
                report_html = build_dir / "report.html"
                analysis_json = build_dir / "analysis.json"
                entry = {
                    "subscription_code": sub_dir.name,
                    "build_code": build_dir.name,
                    "report_url": f"/reports/{sub_dir.name}/{build_dir.name}/report.html",
                }
                if analysis_json.exists():
                    data = json.loads(analysis_json.read_text())
                    entry["build_result"] = data.get("build_result")
                    entry["kba_count"] = len(data.get("kba_matches", []))
                reports.append(entry)
    return JSONResponse(reports)


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
        Route("/reports", list_reports),
        Mount("/reports", app=StaticFiles(directory=str(reports_path), html=True)),
    ]

    app = Starlette(routes=routes, on_startup=[lambda: asyncio.create_task(_poll_loop())])
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8080)
