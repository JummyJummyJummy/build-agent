import json
import logging
import os
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime

from portal_client import download_build_logs, get_build_progress
from parse_build_log import parse_log_file
from kb_client import search_kbas, get_kba
from llm_client import analyze

logger = logging.getLogger(__name__)

REPORTS_DIR = os.environ.get("REPORTS_DIR", "/reports")


def _kba_matches(errors: list) -> list:
    """For each error, search KB and return top matches."""
    matches = []
    seen_ids = set()
    for err in errors[:5]:  # limit to top 5 errors to avoid too many KB calls
        query = err.get("message", "") or err.get("type", "")
        if not query:
            continue
        results = search_kbas("build", query, top_k=3)
        for r in results:
            doc_id = r.get("id", "")
            if doc_id and doc_id not in seen_ids:
                seen_ids.add(doc_id)
                matches.append({
                    "error": query[:120],
                    "kba_id": r.get("title", ""),
                    "title": r.get("title", ""),
                    "score": r.get("score"),
                    "url": r.get("url", ""),
                    "snippet": r.get("snippet", ""),
                    "doc_id": doc_id,
                })
    return matches


def _build_llm_prompt(parsed: dict, kba_matches: list) -> tuple[str, str]:
    system = (
        "You are an SAP Commerce Cloud support engineer. "
        "Analyze build failures and provide concise root cause analysis and resolution steps. "
        "Be specific, reference exact error messages and KBA numbers."
    )
    errors_text = json.dumps(parsed.get("errors", [])[:10], indent=2)
    kba_text = json.dumps(kba_matches[:5], indent=2)
    metadata = parsed.get("metadata", {})
    user = f"""Build Analysis Request

## Build Metadata
- Commerce Version: {metadata.get('commerce_version', 'unknown')}
- Build Result: {parsed.get('build_result', 'unknown')}
- Builder Version: {metadata.get('builder_version', 'unknown')}

## Top Errors
{errors_text}

## Matching KBAs
{kba_text}

Please provide:
1. Root cause summary (2-3 sentences)
2. Most likely resolution steps (numbered)
3. Relevant KBA references
"""
    return system, user


def _generate_html_report(subscription_code: str, build_code: str,
                           parsed: dict, kba_matches: list, analysis: str,
                           report_path: str) -> None:
    metadata = parsed.get("metadata", {})
    errors = parsed.get("errors", [])
    build_result = parsed.get("build_result", "UNKNOWN")
    result_color = "#d32f2f" if "FAIL" in build_result else "#388e3c"

    kba_rows = ""
    for m in kba_matches:
        kba_rows += f"""<tr>
            <td>{m.get('error','')[:80]}</td>
            <td><a href="{m.get('url','#')}" target="_blank">{m.get('title','')}</a></td>
            <td>{m.get('snippet','')[:150]}</td>
        </tr>"""

    error_rows = ""
    for e in errors[:20]:
        error_rows += f"<tr><td>{e.get('type','')}</td><td><code>{str(e.get('message',''))[:200]}</code></td></tr>"

    analysis_html = analysis.replace("\n", "<br>")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Build Analysis — {build_code}</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 2rem; color: #333; }}
  h1 {{ color: #1a73e8; }}
  .result {{ font-size: 1.4rem; font-weight: bold; color: {result_color}; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  .analysis {{ background: #f9f9f9; border-left: 4px solid #1a73e8; padding: 1rem; margin: 1rem 0; }}
  code {{ background: #eee; padding: 2px 4px; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>Build Failure Analysis</h1>
<p><strong>Subscription:</strong> {subscription_code} &nbsp;|&nbsp;
   <strong>Build:</strong> {build_code} &nbsp;|&nbsp;
   <strong>Generated:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
<p class="result">{build_result}</p>

<h2>Build Metadata</h2>
<table>
  <tr><th>Commerce Version</th><td>{metadata.get('commerce_version','—')}</td></tr>
  <tr><th>Builder Version</th><td>{metadata.get('builder_version','—')}</td></tr>
  <tr><th>Java Version</th><td>{metadata.get('java_version','—')}</td></tr>
</table>

<h2>AI Analysis</h2>
<div class="analysis">{analysis_html}</div>

<h2>Matching KBAs ({len(kba_matches)})</h2>
<table>
  <tr><th>Error</th><th>KBA</th><th>Snippet</th></tr>
  {kba_rows or '<tr><td colspan="3">No KBA matches found</td></tr>'}
</table>

<h2>Errors ({len(errors)})</h2>
<table>
  <tr><th>Type</th><th>Message</th></tr>
  {error_rows or '<tr><td colspan="2">No errors extracted</td></tr>'}
</table>
</body>
</html>"""

    Path(report_path).write_text(html, encoding="utf-8")
    logger.info(f"HTML report written to {report_path}")


def run_analysis(subscription_code: str, build_code: str) -> dict:
    """Main analysis pipeline. Returns report metadata."""
    logger.info(f"Starting analysis: {subscription_code}/{build_code}")

    report_dir = Path(REPORTS_DIR) / subscription_code / build_code
    report_dir.mkdir(parents=True, exist_ok=True)

    # 1. Download log
    log_zip = str(report_dir / "build-logs.zip")
    download_build_logs(subscription_code, build_code, log_zip)

    # 2. Extract and parse
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(log_zip, "r") as z:
            z.extractall(tmp)
        log_files = list(Path(tmp).rglob("*.log"))
        if not log_files:
            log_files = list(Path(tmp).rglob("*"))
        log_file = str(sorted(log_files, key=lambda f: f.stat().st_size, reverse=True)[0])
        parsed = parse_log_file(log_file)

    parsed_path = str(report_dir / "parsed.json")
    Path(parsed_path).write_text(json.dumps(parsed, indent=2), encoding="utf-8")

    # 3. KBA matching
    kba_matches = _kba_matches(parsed.get("errors", []))

    # 4. LLM analysis
    system_prompt, user_prompt = _build_llm_prompt(parsed, kba_matches)
    analysis = analyze(system_prompt, user_prompt)

    # 5. Save analysis
    analysis_path = str(report_dir / "analysis.json")
    Path(analysis_path).write_text(json.dumps({
        "subscription_code": subscription_code,
        "build_code": build_code,
        "build_result": parsed.get("build_result"),
        "kba_matches": kba_matches,
        "analysis": analysis,
    }, indent=2), encoding="utf-8")

    # 6. HTML report
    report_html = str(report_dir / "report.html")
    _generate_html_report(subscription_code, build_code, parsed, kba_matches, analysis, report_html)

    logger.info(f"Analysis complete: {report_html}")
    return {
        "subscription_code": subscription_code,
        "build_code": build_code,
        "build_result": parsed.get("build_result"),
        "report_path": report_html,
        "kba_count": len(kba_matches),
    }
