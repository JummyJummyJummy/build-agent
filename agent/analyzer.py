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
    """For each error, search KB and return top matches with full content for high-score hits."""
    matches = []
    seen_ids = set()
    for err in errors[:5]:
        query = err.get("message", "") or err.get("type", "")
        if not query:
            continue
        results = search_kbas("build", query, top_k=3)
        for r in results:
            doc_id = r.get("id", "")
            if doc_id and doc_id not in seen_ids:
                seen_ids.add(doc_id)
                full_content = None
                if (r.get("score") or 0) > 0.3:
                    try:
                        detail = get_kba("build", doc_id)
                        # response: {"id": ..., "data": {"content": ..., ...}, ...}
                        data = detail.get("data") or {}
                        full_content = (
                            data.get("content")
                            or detail.get("content")
                            or detail.get("text")
                        )
                    except Exception as e:
                        logger.warning("get_kba failed for %s: %s", doc_id, e)
                matches.append({
                    "error": query[:120],
                    "kba_id": r.get("title", ""),
                    "title": r.get("title", ""),
                    "score": r.get("score"),
                    "url": r.get("url", ""),
                    "snippet": r.get("snippet", ""),
                    "doc_id": doc_id,
                    "full_content": full_content,
                })
    return matches


def _build_llm_prompt(parsed: dict, kba_matches: list) -> tuple[str, str]:
    system = (
        "You are an SAP Commerce Cloud support engineer. "
        "Analyze build failures and return a JSON object with these exact keys: "
        "summary, root_cause, evidence, suggestion, kbas. "
        "All values except kbas must be HTML strings (use <p>, <code>, <pre>, <ul>/<li> as appropriate). "
        "IMPORTANT: inside JSON string values, never use raw double-quote characters (\") — "
        "use &quot; instead for any double quotes that appear inside HTML attribute values or code examples. "
        "kbas is a list of objects with id (KBA number as string), title, and confidence (high/medium/low). "
        "Return ONLY the JSON object, no markdown fences, no extra text."
    )
    errors_text = json.dumps(parsed.get("errors", [])[:10], indent=2)
    metadata = parsed.get("metadata", {})

    # Build KBA section: summary list + full content for high-score matches
    kba_summary = []
    kba_full_blocks = []
    for m in kba_matches[:5]:
        entry = {k: v for k, v in m.items() if k != "full_content"}
        kba_summary.append(entry)
        if m.get("full_content"):
            parts = (m.get("doc_id", "") or "").split(":")
            kba_num = parts[1] if len(parts) >= 2 and parts[1].isdigit() else m.get("doc_id", "")
            kba_full_blocks.append(
                f"### KBA {kba_num} — {m.get('title', '')}\n{m['full_content']}"
            )

    kba_summary_text = json.dumps(kba_summary, indent=2)
    kba_full_text = ("\n\n".join(kba_full_blocks)) if kba_full_blocks else ""

    user = f"""Build Analysis Request

## Build Metadata
- Commerce Version: {metadata.get('commerce_version', 'unknown')}
- Build Result: {parsed.get('build_result', 'unknown')}
- Builder Version: {metadata.get('builder_version', 'unknown')}

## Top Errors
{errors_text}

## Matching KBAs from knowledge base (summary)
{kba_summary_text}
{f'''
## Full KBA Content (use this as the primary source for root cause and resolution steps)
{kba_full_text}
''' if kba_full_text else ''}
Return a JSON object with keys: summary, root_cause, evidence, suggestion, kbas.
- summary: 2-3 sentence HTML overview of what failed and why.
- root_cause: HTML explanation of the root cause, referencing exact error messages with <code>.
- evidence: HTML with the key log lines or stack traces as <pre> blocks.
- suggestion: HTML numbered steps to fix the issue, drawn from the full KBA content above when available.
- kbas: list of {{id, title, confidence}} objects for relevant KBA notes (use IDs from the knowledge base matches above when available).
"""
    return system, user


def _fix_json_html_quotes(raw: str) -> str:
    """Fix JSON string values that contain raw HTML with unescaped quotes/newlines.

    LLMs emit HTML like <pre>"key": "val"\n...</pre> inside JSON strings without
    escaping inner double-quotes or literal newlines, both of which break json.loads.
    """
    import re as _re

    def _sanitize_block(m):
        inner = m.group(1)
        # escape unescaped double quotes
        inner = _re.sub(r'(?<!\\)"', '&quot;', inner)
        # escape literal newlines (invalid inside a JSON string)
        inner = inner.replace('\n', '\\n').replace('\r', '')
        tag = m.group(0)
        open_tag = tag[:tag.index('>') + 1]
        close_tag = '</' + tag.split('</')[-1]
        return open_tag + inner + close_tag

    raw = _re.sub(r'<pre>(.*?)</pre>', _sanitize_block, raw, flags=_re.DOTALL)
    raw = _re.sub(r'<code>(.*?)</code>', _sanitize_block, raw, flags=_re.DOTALL)
    return raw


def _parse_analysis_json(raw: str) -> dict:
    """Extract structured JSON from LLM response, falling back to plain text wrapping."""
    raw = raw.strip()
    # strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    for attempt in (raw, _fix_json_html_quotes(raw)):
        try:
            data = json.loads(attempt)
            for key in ("summary", "root_cause", "evidence", "suggestion"):
                if key not in data:
                    data[key] = ""
            if "kbas" not in data:
                data["kbas"] = []
            return data
        except json.JSONDecodeError:
            continue
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
        if len(rows) >= 3:
            break
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
        if len(rows) >= 3:
            break
        # Extract numeric KBA id from doc_id like "kba:3740808:0"
        doc_id = m.get("doc_id", "")
        parts = doc_id.split(":")
        kba_num = parts[1] if len(parts) >= 2 and parts[1].isdigit() else None
        # Skip if this KBA number already in table
        if kba_num and kba_num in seen:
            continue
        title = m.get("title", "")
        if not kba_num and title in seen:
            continue
        seen.add(kba_num or title)
        url = m.get("url", "#")
        kba_label = kba_num if kba_num else "—"
        kba_href = f"https://me.sap.com/notes/{kba_num}/E" if kba_num else H.escape(url)
        rows.append(
            f'<tr><td><a href="{kba_href}" target="_blank">{H.escape(kba_label)}</a></td>'
            f'<td>{H.escape(title)}</td>'
            f'<td><span class="conf-low">LOW</span></td></tr>'
        )
    return (
        "<table>"
        "<thead><tr><th>KBA</th><th>Title</th><th>Confidence</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _strip_html(html_str: str) -> str:
    """Remove HTML tags and decode common entities for plain-text email."""
    import re
    text = re.sub(r'<br\s*/?>', '\n', html_str or "", flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '\n- ', text, flags=re.IGNORECASE)
    text = re.sub(r'<ol[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</ol>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<ul[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</ul>', '', text, flags=re.IGNORECASE)
    # numbered list items <li> inside <ol> — replace with numbers
    counter = [0]
    def _num_li(m):
        counter[0] += 1
        return f'\n{counter[0]}. '
    text = re.sub(r'<li[^>]*>', _num_li, text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
    # collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _generate_email_txt(subscription_code: str, build_code: str,
                         parsed: dict, kba_matches: list, analysis: dict) -> str:
    metadata = parsed.get("metadata", {})
    project = metadata.get("customer_project_name", subscription_code)
    commerce_version = metadata.get("commerce_version", "unknown")
    branch = metadata.get("branch", "unknown")
    build_result = parsed.get("build_result", "FAILED")
    errors = parsed.get("errors", [])

    summary = _strip_html(analysis.get("summary", ""))
    root_cause = _strip_html(analysis.get("root_cause", ""))
    suggestion = _strip_html(analysis.get("suggestion", ""))
    kbas = analysis.get("kbas", [])

    # Build KBA reference lines
    kba_lines = []
    seen = set()
    for k in kbas:
        kid = str(k.get("id", "")).strip()
        if not kid or kid in seen:
            continue
        seen.add(kid)
        kba_lines.append(
            f"See KBA {kid} - {k.get('title', '')}: https://me.sap.com/notes/{kid}/E"
        )
    for m in kba_matches:
        doc_id = m.get("doc_id", "")
        # extract numeric KBA id from doc_id like "kba:3740808:0"
        parts = doc_id.split(":")
        if len(parts) >= 2 and parts[1].isdigit() and parts[1] not in seen:
            seen.add(parts[1])
            kba_lines.append(
                f"See KBA {parts[1]} - {m.get('title', '')}: {m.get('url', '')}"
            )

    # Symptom paragraph
    error_msgs = "; ".join(
        e.get("message", "")[:200] for e in errors[:3] if e.get("message")
    )
    symptom = (
        f"Build {build_code} on project {project} has failed with result {build_result}. "
        f"The build was running Commerce version {commerce_version} on branch {branch}. "
    )
    if error_msgs:
        symptom += f"The following errors were detected: {error_msgs}."

    lines = [
        f"Subject: [Build {build_code}] Build Failed - {project}",
        "",
        "Dear Customer,",
        "",
        "Hope this message finds you well.",
        "After a deep investigation, I would like to provide the below summary for you:",
        "",
        "#Symptoms#",
        symptom,
        "",
        "##Investigation##",
        root_cause,
        "",
        "##Cause##",
        root_cause,
        "",
        "##Solution##",
        suggestion,
    ]
    if kba_lines:
        lines.append("")
        lines += kba_lines
    lines += [
        "",
        "We hope the summary above can help you :) If you have any remaining concerns or need further support, please don't hesitate to let me know :)",
        "I am always happy to help you.",
        "",
        "Best regards,",
        "Jummy",
        "SAP Support",
        "-----------------------------------",
        "KBA 1239839 - How to close an incident on the SAP One Support Launchpad",
        "https://launchpad.support.sap.com/#/notes/1239839",
        "KBA 2331471 - How your feedback counts in completing the SAP Support Interaction Survey",
        "https://launchpad.support.sap.com/#/notes/2331471",
    ]
    return "\n".join(lines)


def _generate_html_report(subscription_code: str, build_code: str,
                           parsed: dict, kba_matches: list, analysis: dict,
                           report_path: str, email_txt: str = "") -> None:
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
        "{{EMAIL_TXT}}":            email_txt.replace("</", "<\\/").replace("`", "\\`"),
    }

    out = template
    for key, val in replacements.items():
        out = out.replace(key, val)

    Path(report_path).write_text(out, encoding="utf-8")
    logger.info(f"HTML report written to {report_path}")


def run_analysis(subscription_code: str, build_code: str, trigger_source: str = "manual") -> dict:
    """Main analysis pipeline. Returns report metadata."""
    logger.info(f"Starting analysis: {subscription_code}/{build_code} (source={trigger_source})")

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
        "trigger_source": trigger_source,
        "kba_matches": kba_matches,
        "analysis": analysis,
    }, indent=2), encoding="utf-8")

    # 6. Email text
    email_txt = _generate_email_txt(subscription_code, build_code, parsed, kba_matches, analysis)
    email_path = str(report_dir / "email.txt")
    Path(email_path).write_text(email_txt, encoding="utf-8")

    # 7. HTML report
    report_html = str(report_dir / "report.html")
    _generate_html_report(subscription_code, build_code, parsed, kba_matches, analysis, report_html, email_txt)

    logger.info(f"Analysis complete: {report_html}")
    return {
        "subscription_code": subscription_code,
        "build_code": build_code,
        "build_result": parsed.get("build_result"),
        "report_path": report_html,
        "kba_count": len(kba_matches),
    }
