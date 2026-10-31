import json
import logging
import os
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime

from portal_client import download_build_logs, get_build_progress
from parse_build_log import parse_log as parse_log_file
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
        "Analyze build failures and return a JSON object with these exact keys: "
        "summary, root_cause, evidence, suggestion, kbas. "
        "All values except kbas must be HTML strings (use <p>, <code>, <pre>, <ul>/<li> as appropriate). "
        "kbas is a list of objects with id (KBA number as string), title, and confidence (high/medium/low). "
        "Return ONLY the JSON object, no markdown fences, no extra text."
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

## Matching KBAs from knowledge base
{kba_text}

Return a JSON object with keys: summary, root_cause, evidence, suggestion, kbas.
- summary: 2-3 sentence HTML overview of what failed and why.
- root_cause: HTML explanation of the root cause, referencing exact error messages with <code>.
- evidence: HTML with the key log lines or stack traces as <pre> blocks.
- suggestion: HTML numbered steps to fix the issue.
- kbas: list of {{id, title, confidence}} objects for relevant KBA notes (use IDs from the knowledge base matches above when available).
"""
    return system, user


def _parse_analysis_json(raw: str) -> dict:
    """Extract structured JSON from LLM response, falling back to plain text wrapping."""
    raw = raw.strip()
    # strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    try:
        data = json.loads(raw)
        for key in ("summary", "root_cause", "evidence", "suggestion"):
            if key not in data:
                data[key] = ""
        if "kbas" not in data:
            data["kbas"] = []
        return data
    except json.JSONDecodeError:
        html = raw.replace("\n", "<br>")
        return {
            "summary": f"<p>{html}</p>",
            "root_cause": "",
            "evidence": "",
            "suggestion": "",
            "kbas": [],
        }


def _render_kba_table(kbas: list, kb_matches: list) -> str:
    import html as H
    if not kbas and not kb_matches:
        return "<p>No KBA matches found. Open an incident on <strong>CEC-SCC-CLA-BUI</strong> with the build log attached.</p>"
    rows = []
    seen = set()
    # AI-provided KBAs first
    for k in kbas:
        kba_id = str(k.get("id", "")).strip()
        if not kba_id or kba_id in seen:
            continue
        seen.add(kba_id)
        conf = k.get("confidence", "low").lower()
        conf_cls = {"high": "conf-high", "medium": "conf-medium"}.get(conf, "conf-low")
        url = f"https://me.sap.com/notes/{kba_id}/E"
        rows.append(
            f'<tr><td><a href="{url}" target="_blank">{H.escape(kba_id)}</a></td>'
            f'<td>{H.escape(k.get("title", ""))}</td>'
            f'<td><span class="{conf_cls}">{conf.upper()}</span></td></tr>'
        )
    # KB search matches as supplementary (low confidence if not already listed)
    for m in kb_matches:
        title = m.get("title", "")
        if title in seen:
            continue
        seen.add(title)
        url = m.get("url", "#")
        rows.append(
            f'<tr><td><a href="{H.escape(url)}" target="_blank">—</a></td>'
            f'<td>{H.escape(title)}</td>'
            f'<td><span class="conf-low">LOW</span></td></tr>'
        )
    return (
        "<table>"
        "<thead><tr><th>KBA</th><th>Title</th><th>Confidence</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _generate_html_report(subscription_code: str, build_code: str,
                           parsed: dict, kba_matches: list, analysis: dict,
                           report_path: str) -> None:
    import html as H
    template_path = Path(__file__).parent / "templates" / "build_report_glass.html"
    template = template_path.read_text(encoding="utf-8")

    metadata = parsed.get("metadata", {})
    build_result = parsed.get("build_result", "UNKNOWN")
    badge_cls = "badge-failed" if "FAIL" in build_result else "badge-success"
    result_label = build_result.replace("_", " ")

    replacements = {
        "{{BUILD_ID}}":             H.escape(build_code),
        "{{CUSTOMER_PROJECT}}":     H.escape(subscription_code),
        "{{TIMESTAMP}}":            datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "{{BUILD_RESULT}}":         result_label,
        "{{RESULT_BADGE_CLASS}}":   badge_cls,
        "{{BRANCH}}":               H.escape(metadata.get("branch", "—")),
        "{{COMMERCE_VERSION}}":     H.escape(metadata.get("commerce_version", "—")),
        "{{SOLR_VERSION}}":         "—",
        "{{EXTENSION_PACKS_HTML}}": "<span style='color:rgba(255,255,255,0.35)'>—</span>",
        "{{BUILDER_VERSION}}":      H.escape(metadata.get("builder_version", "—")),
        "{{IMAGE_VERSION}}":        H.escape(metadata.get("image_version", "—")),
        "{{JSAPPS_DETAIL_HTML}}":   "<p>No JS application info.</p>",
        "{{SUMMARY_HTML}}":         analysis.get("summary", ""),
        "{{ROOT_CAUSE_HTML}}":      analysis.get("root_cause", ""),
        "{{EVIDENCE_HTML}}":        analysis.get("evidence", ""),
        "{{RESOLUTION_HTML}}":      analysis.get("suggestion", ""),
        "{{REFERENCE_HTML}}":       _render_kba_table(analysis.get("kbas", []), kba_matches),
    }

    out = template
    for key, val in replacements.items():
        out = out.replace(key, val)

    Path(report_path).write_text(out, encoding="utf-8")
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
        parsed, _banner_blocks = parse_log_file(log_file)

    parsed_path = str(report_dir / "parsed.json")
    Path(parsed_path).write_text(json.dumps(parsed, indent=2), encoding="utf-8")

    # 3. KBA matching
    kba_matches = _kba_matches(parsed.get("errors", []))

    # 4. LLM analysis
    system_prompt, user_prompt = _build_llm_prompt(parsed, kba_matches)
    raw_analysis = analyze(system_prompt, user_prompt)
    analysis = _parse_analysis_json(raw_analysis)

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
