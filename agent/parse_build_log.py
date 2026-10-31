#!/usr/bin/env python3
"""
parse_build_log.py — SAP Commerce Cloud build log parser.

Usage:
    python3 parse_build_log.py <path-to-releaselog.txt> [--output <out.json>] [--evidence <evidence.txt>]

Outputs:
  - structured JSON with metadata, task_timeline, signals, errors, build_result
  - evidence.txt with raw banner blocks (Host/Builder/Manifest sections)
"""

import re
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

TS_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)\s+(INFO|WARN|ERROR|DEBUG)\s+(\S+)\s+\[([^\]]+)\]\s+(.*)'
)
BARE_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)')

TASK_START_RE  = re.compile(r'Started task: (\S+)')
TASK_FINISH_RE = re.compile(r'(?:Task finished|Finished task): (\S+)')
BUILD_STARTED_RE = re.compile(r'd\.h\.c\.Builder \[main\] BUILD STARTED')

# Build result: only trust these exact Builder [main] lines
BUILD_SUCCESS_RE = re.compile(r'd\.h\.c\.Builder \[main\] BUILD SUCCESSFUL')
BUILD_FAILED_RE  = re.compile(r'd\.h\.c\.Builder \[main\] BUILD FAILED')

# Banner block detector: a line of 60+ asterisks
BANNER_START_RE  = re.compile(r'^\**\s*\*{60,}')
BANNER_SECTION_RE = re.compile(
    r'\*\s*(Host details|Builder details|Builder parameters'
    r'|Javascript Applications Manifest|Core commerce Manifest'
    r'|Images to generate|Services to enable)\s*\*'
)

# Header / Builder parameters block
HEADER_FIELDS = {
    'java_version':             re.compile(r'\*\s+Java version:\s+(\S+)'),
    'builder_version':          re.compile(r'\*\s+Builder version:\s+(\S+)'),
    'customer_project_name':    re.compile(r'\*\s+customerProjectName:\s+(\S+)'),
    'image_version':            re.compile(r'\*\s+resultingImageVersion:\s+(\S+)'),
    'branch':                   re.compile(r'\*\s+projectRepositoryBranch:\s+(.+?)\s*\*?\s*$'),
    'repo_url':                 re.compile(r'\*\s+projectRepositoryUrl:\s+(\S+)'),
    'base_image_registry':      re.compile(r'\*\s+baseImageRegistryUrl:\s+(\S+)'),
    'project_image_registry':   re.compile(r'\*\s+projectImageRegistryUrl:\s+(\S+)'),
}

MANIFEST_COMMERCE_VERSION_RE = re.compile(r'\*\s+SAP Commerce version:\s+(\S+)')
MANIFEST_EXT_PACK_NAME_RE    = re.compile(r'\*\s+Name:\s+(\S+)')
MANIFEST_EXT_PACK_VER_RE     = re.compile(r'\*\s+Version:\s+(\S+)')
MANIFEST_PROPERTIES_RE       = re.compile(r'\*\s+Location:\s+(\S+)')

# Commerce suite version determined at runtime
COMMERCE_SUITE_VER_RE  = re.compile(r'EvaluateCoreCommerceArtifactsTask.*Determined Commerce Suite version.*?:\s+(\S+)')
COMMERCE_SUITE_VER2_RE = re.compile(r'ExactCommerceSuiteVersionUtil.*Determined Commerce Suite version.*?:\s+(\S+)')

# ---------------------------------------------------------------------------
# Anchor-based error scanner constants
# ---------------------------------------------------------------------------

ANCHOR_PATTERNS = [
    re.compile(r'PrintOutputHandler.*sterr:\s+BUILD FAILED'),
    re.compile(r'PrintOutputHandler.*stdout:\s+BUILD FAILED'),
    re.compile(r'ERROR.*\[FAILED\].*Command.*failed on retry'),
    re.compile(r'Exception in thread "main"'),
]

ERROR_RE = re.compile(r'''
    OutOfMemoryError | GC\ overhead | GCLocker | out\ of\ resources
    | java\.lang\.\w+Error | java\.lang\.\w+Exception
    | \[yjavac\]\ .*(?:error:|cannot\ find\ symbol|does\ not\ exist|unmappable)
    | \[groovyc\]\ .*error
    | npm\ ERR! | yarn\ ERR | \bEACCES\b | \bETIMEDOUT\b | Cannot\ find\ a\ package\.json
    | ConnectException | UnknownHostException | SSLHandshakeException
    | Repository\ not\ found | Could\ not\ read\ from\ remote
    | Permission\ denied | Authentication\ failed
    | (?<!\w)fatal: | ERROR:\ Repository
''', re.VERBOSE | re.IGNORECASE)

THREAD_RE_PH = re.compile(r'PrintOutputHandler \[(Thread-\d+)\]')

_ERROR_TYPE_RULES = [
    ('OutOfMemoryError', re.compile(r'OutOfMemoryError|GC overhead|GCLocker|out of resources', re.IGNORECASE)),
    ('JavaError',        re.compile(r'java\.lang\.\w+Error', re.IGNORECASE)),
    ('JavaException',    re.compile(r'java\.lang\.\w+Exception', re.IGNORECASE)),
    ('CompilationError', re.compile(r'\[yjavac\].*(?:error:|cannot find symbol|does not exist|unmappable)|\[groovyc\].*error', re.IGNORECASE)),
    ('YarnError',        re.compile(r'npm ERR!|yarn ERR|\bEACCES\b|\bETIMEDOUT\b|Cannot find a package\.json', re.IGNORECASE)),
    ('NetworkError',     re.compile(r'ConnectException|UnknownHostException|SSLHandshakeException', re.IGNORECASE)),
    ('GitError',         re.compile(r'Repository not found|Could not read from remote|Permission denied|Authentication failed|(?<!\w)fatal:', re.IGNORECASE)),
]


# ---------------------------------------------------------------------------
# Helpers: error classification and thread indexing
# ---------------------------------------------------------------------------

def _classify_error_type(line):
    for etype, pat in _ERROR_TYPE_RULES:
        if pat.search(line):
            return etype
    return 'GeneralError'


def _build_thread_index(task_lines):
    """Return dict mapping thread_id -> sorted list of task-relative line indices."""
    index = {}
    for i, line in enumerate(task_lines):
        m = THREAD_RE_PH.search(line)
        if m:
            tid = m.group(1)
            if tid not in index:
                index[tid] = []
            index[tid].append(i)
    return index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_timestamp(ts_str):
    for fmt in ('%Y-%m-%d %H:%M:%S,%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            pass
    return None


def duration_seconds(start_ts, end_ts):
    if start_ts and end_ts:
        return int((end_ts - start_ts).total_seconds())
    return None


def extract_timestamp(line):
    m = TS_RE.match(line)
    if m:
        return parse_timestamp(m.group(1))
    m = BARE_TS_RE.match(line)
    if m:
        return parse_timestamp(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Banner block extractor
# ---------------------------------------------------------------------------

def extract_banner_blocks(lines):
    """
    Extract all ****....**** banner blocks from the log.
    Returns list of {'title': str, 'lines': [str], 'start_line': int}.
    """
    blocks = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        # Detect banner opening line (60+ asterisks, possibly prefixed by timestamp)
        stripped = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \w+ \S+ \[\S+\] ', '', line)
        if not re.match(r'\*{60,}', stripped.strip()) and not re.match(r'\*{60,}', stripped.replace(' ', '')):
            i += 1
            continue

        # Collect all lines until the next banner closing line (another row of 60+ stars)
        block_start = i
        block_lines = [line]
        j = i + 1
        while j < n:
            l = lines[j]
            stripped_l = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \w+ \S+ \[\S+\] ', '', l)
            block_lines.append(l)
            # Banner ends when we hit another line of 60+ stars after the first
            if j > i + 1 and re.match(r'\*{60,}', stripped_l.strip().replace(' ', '')):
                break
            j += 1

        # Find the section title inside the block
        title = 'Unknown'
        for bl in block_lines:
            m = BANNER_SECTION_RE.search(bl)
            if m:
                title = m.group(1)
                break

        blocks.append({'title': title, 'start_line': block_start + 1, 'lines': block_lines})
        i = j + 1

    return blocks


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_log(path):
    lines = Path(path).read_text(errors='replace').splitlines()
    n = len(lines)

    result = {
        'build_id':      Path(path).stem.replace('releaselog-', ''),
        'log_lines':     n,
        'metadata':      {},
        'task_timeline': [],
        'signals':       [],
        'errors':        [],
        'build_result':  'UNKNOWN',
        'banner_blocks': [],  # titles of extracted banners (full text in evidence file)
    }

    # -----------------------------------------------------------------------
    # Step A: Determine build result from TAIL ONLY
    # Check last 100 lines for BUILD SUCCESSFUL first.
    # If not found, check last 200 lines for BUILD FAILED or Exception in thread.
    # -----------------------------------------------------------------------
    tail_100 = lines[max(0, n - 100):]
    tail_200 = lines[max(0, n - 200):]

    overall_result = 'UNKNOWN'
    for line in reversed(tail_100):
        if BUILD_SUCCESS_RE.search(line):
            overall_result = 'SUCCESSFUL'
            break

    if overall_result == 'UNKNOWN':
        # No BUILD SUCCESSFUL in last 100 lines — it failed somehow
        for line in reversed(tail_200):
            if BUILD_FAILED_RE.search(line):
                overall_result = 'FAILED_BUILD'
                break
        if overall_result == 'UNKNOWN':
            for line in reversed(tail_200):
                if 'Exception in thread' in line:
                    overall_result = 'FAILED_EXCEPTION'
                    break
        if overall_result == 'UNKNOWN':
            # Has tasks but no explicit result — treat as failed
            overall_result = 'FAILED_UNKNOWN'

    result['build_result'] = overall_result

    # -----------------------------------------------------------------------
    # Step B: Extract banner blocks for evidence file
    # -----------------------------------------------------------------------
    banner_blocks = extract_banner_blocks(lines)
    result['banner_blocks'] = [b['title'] for b in banner_blocks]

    # -----------------------------------------------------------------------
    # Pass 1: collect task boundaries
    # -----------------------------------------------------------------------
    tasks = []

    for i, line in enumerate(lines):
        m = TASK_START_RE.search(line)
        if m and 'd.h.c.Builder [main]' in line:
            ts = extract_timestamp(line)
            tasks.append({'name': m.group(1), 'start_line': i, 'start_ts': ts,
                          'end_line': None, 'end_ts': None})

    # Find last timestamped line (last lines may be pure stack trace)
    last_ts = None
    for raw in reversed(lines):
        last_ts = extract_timestamp(raw)
        if last_ts:
            break

    for idx, task in enumerate(tasks):
        if idx + 1 < len(tasks):
            task['end_line'] = tasks[idx + 1]['start_line'] - 1
            task['end_ts']   = tasks[idx + 1]['start_ts']
        else:
            task['end_line'] = n - 1
            task['end_ts']   = last_ts

    # -----------------------------------------------------------------------
    # Pass 2: header + manifest metadata (lines before first task)
    # -----------------------------------------------------------------------
    header_end = tasks[0]['start_line'] if tasks else min(200, n)
    _extract_header_metadata(lines[:header_end], result['metadata'])

    # -----------------------------------------------------------------------
    # Pass 3: per-task extraction
    # -----------------------------------------------------------------------
    for task in tasks:
        tname = task['name']
        task_lines = lines[task['start_line']:task['end_line'] + 1]
        dur = duration_seconds(task['start_ts'], task['end_ts'])

        entry = {
            'name':             tname,
            'started':          task['start_ts'].isoformat() if task['start_ts'] else None,
            'duration_seconds': dur,
            'status':           'ok',
            'signals':          [],
        }

        if tname == 'checkCredentials':
            _extract_check_credentials(task_lines, entry)
        elif tname == 'cloneCustomerProject':
            _extract_clone(task_lines, entry)
        elif tname == 'readCoreCommerceManifest':
            _extract_core_manifest(task_lines, result['metadata'], entry)
        elif tname == 'readJsAppsManifest':
            _extract_jsapps_manifest(task_lines, result['metadata'], entry)
        elif tname in ('evaluateCoreCommerceArtifacts', 'evaluateCoreCommerceServicesAndImages'):
            _extract_evaluate(task_lines, result['metadata'], entry)
        elif tname == 'buildCoreCommerce':
            _extract_build_core_commerce(task_lines, entry)
        elif tname == 'buildJsApps':
            _extract_build_jsapps(task_lines, entry)
        elif tname == 'testCoreCommerce':
            _extract_test_core_commerce(task_lines, entry)
        elif tname == 'printTestResults':
            _extract_print_test_results(task_lines, entry)
        elif tname == 'buildImages':
            _extract_build_images(task_lines, entry)

        errors = _scan_errors(task_lines, tname, task['start_line'])
        result['errors'].extend(errors)
        if errors:
            entry['status'] = 'failed'

        result['task_timeline'].append({
            'name':             entry['name'],
            'started':          entry['started'],
            'duration_seconds': entry['duration_seconds'],
            'status':           entry['status'],
        })
        if entry['signals']:
            result['signals'].append({'task': tname, 'items': entry['signals']})

    return result, banner_blocks


# ---------------------------------------------------------------------------
# Metadata extractors
# ---------------------------------------------------------------------------

def _extract_header_metadata(lines, meta):
    for line in lines:
        for field, pattern in HEADER_FIELDS.items():
            if field not in meta:
                m = pattern.search(line)
                if m:
                    meta[field] = m.group(1).strip()


def _extract_core_manifest(lines, meta, entry):
    in_ext_pack   = False
    current_pack  = {}
    ext_packs     = []
    properties_files = []
    addons        = []
    in_addons     = False

    for line in lines:
        if 'commerce_version' not in meta:
            m = MANIFEST_COMMERCE_VERSION_RE.search(line)
            if m:
                meta['commerce_version'] = m.group(1)

        if 'Extension packs to be included' in line:
            in_ext_pack = True
            continue
        if in_ext_pack:
            m = MANIFEST_EXT_PACK_NAME_RE.search(line)
            if m:
                if current_pack.get('name'):
                    ext_packs.append(current_pack)
                current_pack = {'name': m.group(1)}
                continue
            m = MANIFEST_EXT_PACK_VER_RE.search(line)
            if m and current_pack:
                current_pack['version'] = m.group(1)
                continue
            if '* Image Processing' in line or '* References' in line:
                if current_pack.get('name'):
                    ext_packs.append(current_pack)
                current_pack  = {}
                in_ext_pack   = False

        m = MANIFEST_PROPERTIES_RE.search(line)
        if m and 'Location:' in line:
            properties_files.append(m.group(1))

        if 'Addons to be installed' in line:
            in_addons = True
        if in_addons and re.search(r'storefront:', line):
            addons.append(line.strip().strip('*').strip())
        if in_addons and '* Properties:' in line:
            in_addons = False

    if current_pack.get('name'):
        ext_packs.append(current_pack)

    if ext_packs:
        meta['extension_packs'] = ext_packs
    if properties_files:
        meta['properties_files'] = list(dict.fromkeys(properties_files))
    if addons:
        meta['addons'] = addons

    entry['signals'].append({
        'type':                  'manifest_parsed',
        'commerce_version':      meta.get('commerce_version'),
        'extension_packs':       ext_packs,
        'properties_files_count': len(properties_files),
    })


def _extract_jsapps_manifest(lines, meta, entry):
    apps    = []
    current = {}
    for line in lines:
        m = re.search(r'\*\s+Name:\s+(\S+)', line)
        if m and 'Path:' not in line:
            if current.get('name'):
                apps.append(current)
            current = {'name': m.group(1), 'ssr': False}
        if current and re.search(r'enabled:\s+true', line):
            current['ssr'] = True
        if current:
            m2 = re.search(r'\*\s+Path:\s+(\S+)', line)
            if m2:
                current['path'] = m2.group(1)

    if current.get('name'):
        apps.append(current)

    meta['js_apps'] = apps
    if apps:
        entry['signals'].append({'type': 'jsapps_manifest', 'apps': apps})


def _extract_evaluate(lines, meta, entry):
    for line in lines:
        for pat in (COMMERCE_SUITE_VER_RE, COMMERCE_SUITE_VER2_RE):
            m = pat.search(line)
            if m:
                meta['commerce_suite_version_resolved'] = m.group(1)
                entry['signals'].append({'type': 'commerce_version_resolved', 'version': m.group(1)})
                return


# ---------------------------------------------------------------------------
# Task-specific extractors
# ---------------------------------------------------------------------------

def _extract_check_credentials(lines, entry):
    results = []
    for line in lines:
        if 'Authentication to registry' in line:
            m = re.search(r'registry:\s+(\S+)\s+(succeeded|failed)', line)
            if m:
                results.append({'registry': m.group(1), 'result': m.group(2)})
        if 'failed' in line.lower() and 'registry' in line.lower():
            entry['status'] = 'failed'
    if results:
        entry['signals'].append({'type': 'registry_auth', 'results': results})


def _extract_clone(lines, entry):
    for line in lines:
        m = re.search(r'HEAD is now at ([0-9a-f]{7,})', line)
        if m:
            entry['signals'].append({'type': 'git_commit', 'sha': m.group(1)})
        if 'fatal:' in line or 'Authentication failed' in line:
            entry['status'] = 'failed'
            entry['signals'].append({'type': 'git_error', 'message': line.strip()})


def _extract_build_core_commerce(lines, entry):
    success_count  = 0
    failed_threads = []
    for line in lines:
        if 'PrintOutputHandler' in line and 'stdout: BUILD SUCCESSFUL' in line:
            success_count += 1
        if 'PrintOutputHandler' in line and 'stdout: BUILD FAILED' in line:
            m = re.search(r'\[Thread-(\d+)\]', line)
            failed_threads.append(m.group(1) if m else '?')
            entry['status'] = 'failed'

    entry['signals'].append({
        'type':               'ant_build_summary',
        'sub_build_successes': success_count,
        'failed_threads':      failed_threads,
    })


def _extract_build_jsapps(lines, entry):
    """
    Extract per-app build result for JsApps.

    Log patterns observed:
      Start:   "d.h.c.j.JsAppBuilder [main] Building application: {name}, ..."
               "d.h.c.j.t.BuildJsAppsTask [main] Preparing to build application {name}"
      Success: "Task finished: buildJsApps" (no per-app success line emitted)
               Angular/SPA: PrintOutputHandler stdout: "Output location: ..."
                            PrintOutputHandler stdout: "Done in N.NNs."
      Failure: YarnExecutor exit code != 0
               PrintOutputHandler sterr/stdout: ERROR: / error: {message}
    """
    apps        = {}
    current_app = None

    for line in lines:
        # App build start — "Building application: {name}"
        m = re.search(r'JsAppBuilder \[main\] Building application:\s+(\S+)', line)
        if m:
            current_app       = m.group(1).rstrip(',')
            apps[current_app] = 'building'
            continue

        # Also catch "Preparing to build application {name}"
        m = re.search(r'BuildJsAppsTask.*Preparing to build application\s+(\S+)', line)
        if m and m.group(1) not in apps:
            apps[m.group(1)] = 'building'
            continue

        # SPA Angular/Webpack completion signals (PrintOutputHandler stdout)
        if 'PrintOutputHandler' in line and 'stdout:' in line:
            # "Output location: ..." or "Done in N.NNs." → bundle finished
            if re.search(r'(?:Output location:|Done in \d+\.\d+s\.)', line):
                if current_app and apps.get(current_app) == 'building':
                    apps[current_app] = 'success'

        # Yarn/npm exit code
        if 'YarnExecutor' in line and 'exit code' in line:
            m = re.search(r'exit code[:\s]+(\d+)', line)
            if m:
                code = m.group(1)
                if code != '0':
                    entry['status'] = 'failed'
                    if current_app:
                        apps[current_app] = 'failed'
                    entry['signals'].append({'type': 'yarn_exit_code', 'code': code, 'app': current_app})

        # Strict error: only real yarn/webpack error lines, not filenames
        if re.search(r'PrintOutputHandler.*(?:stdout|sterr):\s+(?:ERROR:|error:)\s', line):
            entry['status'] = 'failed'
            if current_app:
                apps[current_app] = 'failed'

    # Any app still 'building' at end of task with no failure → success
    for name, status in apps.items():
        if status == 'building' and entry['status'] == 'ok':
            apps[name] = 'success'

    if apps:
        entry['signals'].append({'type': 'jsapp_build_results', 'apps': apps})


def _extract_test_core_commerce(lines, entry):
    for line in lines:
        if 'Tests are not configured, skipping' in line:
            entry['signals'].append({'type': 'tests_skipped', 'reason': 'not configured'})
            return
        if 'yunitlauncher' in line.lower():
            entry['signals'].append({'type': 'tests_running'})
            return
    entry['signals'].append({'type': 'tests_unknown'})


def _extract_print_test_results(lines, entry):
    xml_lines = []
    in_xml    = False
    for line in lines:
        if 'BEGIN: Test result' in line:
            in_xml = True
            continue
        if 'END: Test result' in line:
            in_xml = False
            continue
        if in_xml:
            xml_lines.append(line)

    if not xml_lines:
        return

    xml_text = '\n'.join(xml_lines)

    total = failures = errors = skipped = 0
    failed_tests = []

    for m in re.finditer(r'<testsuite\b([^>]+)>', xml_text):
        attrs = m.group(1)
        def _int(attr):
            am = re.search(rf'{attr}="(\d+)"', attrs)
            return int(am.group(1)) if am else 0
        total    += _int('tests')
        failures += _int('failures')
        errors   += _int('errors')
        skipped  += _int('skipped')

    for m in re.finditer(r'<testcase\b([^>]+)>(.*?)</testcase>', xml_text, re.DOTALL):
        if '<failure' in m.group(2) or '<error' in m.group(2):
            attrs   = m.group(1)
            name_m  = re.search(r'name="([^"]+)"', attrs)
            class_m = re.search(r'classname="([^"]+)"', attrs)
            msg_m   = re.search(r'message="([^"]*)"', m.group(2))
            failed_tests.append({
                'test':      name_m.group(1)  if name_m  else '?',
                'classname': class_m.group(1) if class_m else '?',
                'message':   msg_m.group(1)[:200] if msg_m else '',
            })

    entry['signals'].append({
        'type':         'test_results',
        'total':        total,
        'failures':     failures,
        'errors':       errors,
        'skipped':      skipped,
        'passed':       total - failures - errors - skipped,
        'failed_tests': failed_tests,
    })
    if failures > 0 or errors > 0:
        entry['status'] = 'failed'


def _extract_build_images(lines, entry):
    images = {}
    for line in lines:
        m = re.search(r'AbstractDockerImageBuilder.*(?:Building and pushing|Tagging).*name:\s+([^\s,]+)', line)
        if m:
            images[m.group(1)] = images.get(m.group(1), 'building')
        if re.search(r'JibDockerImageBuilder.*Finished building.*image id', line):
            for k in reversed(list(images)):
                if images[k] == 'building':
                    images[k] = 'success'
                break
        if 'ConnectException' in line or 'Connection refused' in line:
            entry['status'] = 'failed'
        m = re.search(r'Trying to build an image\s+(\S+),\s+trial\s+(\d+)', line)
        if m:
            img   = m.group(1).split('/')[-1].split(':')[0]
            trial = int(m.group(2))
            if trial > 1:
                entry['signals'].append({'type': 'image_retry', 'image': img, 'trial': trial})

    if images:
        entry['signals'].append({'type': 'image_build_results', 'images': images})


# ---------------------------------------------------------------------------
# Generic error scanner
# ---------------------------------------------------------------------------

def _scan_errors(task_lines, task_name, line_offset):
    n = len(task_lines)

    # ── Step 1: find failure anchor ────────────────────────────────────────
    anchor_idx = None
    for anchor_pat in ANCHOR_PATTERNS:
        for i, line in enumerate(task_lines):
            if anchor_pat.search(line):
                anchor_idx = i
                break
        if anchor_idx is not None:
            break

    # ── Fallback: no-log sentinel (only when anchor found or task suspiciously tiny) ──
    if n < 5 or any('No builder log is available' in l for l in task_lines):
        if anchor_idx is not None:
            return [{
                'task':                task_name,
                'type':                'NO_LOG',
                'line_number':         line_offset + 1,
                'message':             'No builder log available — infrastructure or cluster failure likely',
                'same_thread_context': [],
                'raw_context':         task_lines[:min(n, 10)],
            }]
        return []

    # ── Step 2: determine scan range ──────────────────────────────────────
    if anchor_idx is not None:
        scan_lines = task_lines[:anchor_idx]
    else:
        scan_lines = task_lines[max(0, n - 200):]

    # ── Step 3: build per-thread index ────────────────────────────────────
    thread_index = _build_thread_index(task_lines)

    # ── Step 4: collect error signals ─────────────────────────────────────
    raw_signals = []
    for i, line in enumerate(scan_lines):
        # Skip [echo] lines — Ant echo tasks emit informational strings that may
        # contain substrings like "fatal:" or error codes but are not failures
        if re.search(r'\[echo\]', line):
            continue
        if ERROR_RE.search(line):
            task_idx = i if anchor_idx is not None else (n - len(scan_lines) + i)
            raw_signals.append((task_idx, line))

    # ── Step 5: deduplicate — merge signals within 5 lines of each other ──
    signals = []
    last_idx = -999
    for task_idx, line in raw_signals:
        if task_idx - last_idx < 5:
            continue
        signals.append((task_idx, line))
        last_idx = task_idx

    # ── Step 6: fallback sentinel if anchor found but no signals ──────────
    if not signals:
        if anchor_idx is not None:
            return [{
                'task':                task_name,
                'type':                'TRUNCATED',
                'line_number':         line_offset + n,
                'message':             ('Build process terminated without explicit failure signal '
                                        '(possible OOM kill or infrastructure failure, exit code 137)'),
                'same_thread_context': [],
                'raw_context':         task_lines[max(0, n - 50):],
            }]
        return []

    # ── Step 7: build context for each signal ─────────────────────────────
    errors = []
    for task_idx, line in signals:
        raw_ctx = task_lines[max(0, task_idx - 50): task_idx + 51]

        m = THREAD_RE_PH.search(line)
        if m:
            tid = m.group(1)
            t_seq = thread_index.get(tid, [])
            pos = next((p for p, idx in enumerate(t_seq) if idx == task_idx), None)
            if pos is None:
                pos = min(range(len(t_seq)), key=lambda p: abs(t_seq[p] - task_idx)) if t_seq else 0
            same_ctx = [task_lines[t_seq[p]] for p in range(max(0, pos - 50), min(len(t_seq), pos + 51))]
        else:
            same_ctx = raw_ctx

        clean = re.sub(
            r'^\S+\s+\S+\s+d\.h\.c\.h\.PrintOutputHandler\s+\[[^\]]+\]\s+(?:stdout|sterr):\s*',
            '', line
        ).strip()

        errors.append({
            'task':                task_name,
            'type':                _classify_error_type(line),
            'line_number':         line_offset + task_idx + 1,
            'message':             clean[:500],
            'same_thread_context': same_ctx,
            'raw_context':         raw_ctx,
        })

    return errors


# ---------------------------------------------------------------------------
# Evidence file writer
# ---------------------------------------------------------------------------

def write_evidence(banner_blocks, out_path):
    """Write extracted banner blocks to a plain-text evidence file."""
    parts = []
    for block in banner_blocks:
        parts.append(f'=== {block["title"]} (log line {block["start_line"]}) ===\n')
        parts.append('\n'.join(block['lines']))
        parts.append('\n\n')
    Path(out_path).write_text(''.join(parts), encoding='utf-8')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Parse CCv2 build log to structured JSON + evidence')
    ap.add_argument('log_path',          help='Path to releaselog-*.txt')
    ap.add_argument('--output',   '-o',  help='Output JSON path (default: stdout)')
    ap.add_argument('--evidence', '-e',  help='Output evidence.txt path (default: no file)')
    args = ap.parse_args()

    result, banner_blocks = parse_log(args.log_path)
    out = json.dumps(result, indent=2, default=str)

    if args.output:
        Path(args.output).write_text(out)
        print(f'JSON written to {args.output}', file=sys.stderr)
    else:
        print(out)

    if args.evidence:
        write_evidence(banner_blocks, args.evidence)
        print(f'Evidence written to {args.evidence}', file=sys.stderr)


if __name__ == '__main__':
    main()
