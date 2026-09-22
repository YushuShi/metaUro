"""metaUro: one interface over two immutable evidence collections."""
from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, send_from_directory
from source_worker import SOURCES, workers
from study_selection import build_selection_flow
from automation.sync_sources import assembled_source, load_overlay, bound, sha
from summary_outputs import ensure_current, summary_mode

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
LABELS = {'metaPee': 'Urinary tract', 'MetaVir': 'Prostate'}
PUBLIC_SCOPES = {'metaPee': 'urinary', 'MetaVir': 'prostate'}
UI_LABEL_TRANSLATIONS = {
    'Download': {'zh-CN': '下载', 'zh-TW': '下載', 'nl': 'Downloaden', 'ko': '다운로드'},
    'VERIFY': {'zh-CN': '核实', 'zh-TW': '驗證', 'nl': 'Verifiëren', 'ko': '검증'},
}


def resolve_source(value):
    return next((source for source, public in PUBLIC_SCOPES.items() if value in {source, public}), None)


def public_prefix(source):
    return f'/evidence/{PUBLIC_SCOPES[source]}'


def product_branding(text):
    return re.sub(r'meta(?:pee|vir)', 'metaUro', text, flags=re.IGNORECASE)


def scope_catalog():
    result = []
    for source in SOURCES:
        catalog = json.loads((ROOT / 'sources' / source / 'static/seer-baseline.json').read_text())
        for disease in catalog['diseases']:
            result.append({'source': source, 'group': LABELS[source], 'id': disease['id'], 'label': disease['label'],
                           'prefix': public_prefix(source), 'group_id': PUBLIC_SCOPES[source]})
    return result


def context(source, page='evidence'):
    scopes = scope_catalog()
    selected = request.args.get('scope', '')
    current = next((s for s in scopes if s['source'] == source and selected in {s['id'], s['label']}), next(s for s in scopes if s['source'] == source))
    track = 'chatgpt' if source == 'MetaVir' and (request.args.get('track') == 'chatgpt' or request.path.endswith('/chatgpt')) else 'combined'
    return dict(source=source, prefix=public_prefix(source), public_source=PUBLIC_SCOPES[source], scopes=scopes, current=current, track=track,
                page=page, source_label=LABELS[source], include_subexposures=request.args.get('include_subexposures', 'yes'))


def prefix_text(value, prefix):
    # Rebase only the source apps' known local routes; external citations stay intact.
    paths = r'(?:static|api|analyze|reanalyze|verify|exclude|summary|about|chatgpt|combined|provisional-assets)'
    return re.sub(r'([\'"`(=])/(' + paths + r')(?=[/\'"`?\s)])', lambda m: m[1] + prefix + '/' + m[2], value)


def prefix_json(value, prefix):
    if isinstance(value, dict):
        return {k: prefix_json(v, prefix) for k, v in value.items()}
    if isinstance(value, list):
        return [prefix_json(v, prefix) for v in value]
    if isinstance(value, str) and re.match(r'^/?(?:static|provisional-assets|summary)/', value):
        return prefix + '/' + value.lstrip('/')
    return value


def decorate_html(body, source, path):
    ctx = context(source, 'summary' if path.startswith('summary') else 'evidence')
    soup = BeautifulSoup(prefix_text(body, ctx['prefix']), 'html.parser')
    if not soup.head or not soup.body:
        return body
    soup.title.string = f"metaUro · {'Evidence summary' if ctx['page'] == 'summary' else 'Explore evidence'}"
    for stylesheet in soup.select('link[rel="stylesheet"]'):
        if '/static/style.css' in stylesheet.get('href', ''):
            stylesheet['href'] = '/static/theme.css'
    old_header = soup.find('header')
    old_controls = []
    if old_header:
        for selector in ['.language-picker']:
            control = old_header.select_one(selector)
            if control:
                old_controls.append(str(control))
        old_header.replace_with(BeautifulSoup(render_template('_header.html', **ctx), 'html.parser'))
    # Remove duplicate source-brand navigation, while preserving the source study UI.
    for node in soup.select('.hero h1'):
        node.string = 'Synthesize Evidence on Urologic Cancers & Nutrition'
    for node in soup.select('.hero > p'):
        node.string = 'Explore associations between nutritional exposures and urologic cancer incidence'
    for node in soup.select('#loading p'):
        node.string = 'Loading saved evidence and calculating the selected view…'
    baujat_download = soup.select_one('#download-baujat-png')
    if baujat_download:
        baujat_download.string = 'Download'
    for heading in soup.select('#studies-table thead th'):
        if heading.get_text(' ', strip=True).upper() == 'VERIFIED':
            heading.string = 'VERIFY'
    disease = soup.select_one('#disease')
    if disease:
        selected = next((o for o in disease.find_all('option') if o.get('selected') is not None), disease.find('option'))
        selected_label = ctx['current']['label'] if request.args.get('scope') else (selected.get('value') if selected else '')
        for option in disease.find_all('option'):
            option['data-source'] = source
            option.attrs.pop('selected', None)
            if option.get('value') == selected_label:
                option['selected'] = ''
        group = soup.new_tag('optgroup', label=LABELS[next(s for s in SOURCES if s != source)])
        for item in ctx['scopes']:
            if item['source'] != source:
                option = soup.new_tag('option', value=item['label'])
                option['data-source'], option['data-scope-key'] = item['source'], item['id']
                option.string = item['label']
                group.append(option)
        disease.append(group)
    if ctx['page'] == 'summary' and soup.main:
        notice = soup.new_tag('div', attrs={'class': 'uro-summary-notice'})
        notice.string = 'These are saved summary plots and workbooks. Settings apply to the interactive evidence analysis.'
        soup.main.insert(0, notice)
        picker = soup.new_tag('nav', attrs={'class': 'uro-summary-notice', 'aria-label': 'Disease summaries'})
        for collection in SOURCES:
            link = soup.new_tag('a', href=f'{public_prefix(collection)}/summary?include_subexposures={ctx["include_subexposures"]}')
            link.string = LABELS[collection] + ' summaries'
            picker.append(link)
            picker.append('   ·   ' if collection == SOURCES[0] else '')
        soup.main.insert(0, picker)
    config = dict(source=source, prefix=ctx['prefix'], current=ctx['current'], scopes=ctx['scopes'], page=ctx['page'], track=ctx['track'])
    bootstrap = soup.new_tag('script')
    bootstrap.string = 'window.MetaUro = ' + json.dumps(config).replace('<', '\\u003c') + ';'
    soup.head.insert(0, bootstrap)
    for href in ['/static/analysis-settings.css', '/static/metauro.css']:
        node = soup.new_tag('link', rel='stylesheet', href=href)
        soup.head.append(node)
    # Initialize settings before the source script uses AnalysisSettings.
    for src in ['/static/metauro.js', '/static/analysis-settings.js', '/static/case-counts.js?v=20260918']:
        node = soup.new_tag('script', src=src)
        soup.head.append(node)
    minimum = soup.select_one('#filter-min-cases')
    if minimum:
        minimum['value'], minimum['min'], minimum['step'] = '0', '0', '1'
        minimum['title'] = '0 includes studies regardless of case count. Missing cases use N × the baseline risk selected in Settings.'
    for script in soup.select('script[src]'):
        if '/static/script.js' in script['src']:
            script['src'] = script['src'].split('?')[0] + '?v=selected-case-counts-20260918'
    # Relative downloads/links produced by source templates must stay with their source.
    for node in soup.select('[href], [src], [action], [data]'):
        for attr in ('href', 'src', 'action', 'data'):
            value = node.get(attr)
            if value == '/' or (isinstance(value, str) and value.startswith('/?')):
                node[attr] = ctx['prefix'] + value
    if old_controls:
        controls = soup.select_one('#language-controls')
        if controls:
            controls.append(BeautifulSoup(''.join(old_controls), 'html.parser'))
    return str(soup)


@app.get('/')
def index():
    return redirect('/evidence/urinary/')


@app.get('/health')
def health():
    return jsonify(status='ok', application='metaUro', sources=list(SOURCES))


@app.get('/api/scopes')
def scopes():
    return jsonify(scope_catalog())


@app.get('/settings')
def settings():
    selected_scope = request.args.get('scope')
    inferred = next((s['source'] for s in scope_catalog() if selected_scope in {s['id'], s['label']}), 'metaPee')
    source = resolve_source(request.args.get('group', request.args.get('source', inferred)))
    if source is None:
        abort(404)
    return render_template('settings.html', **context(source, 'settings'))


@app.get('/about')
def about():
    return render_template('about.html', **context('metaPee', 'about'))


@app.get('/study-selection')
def study_selection():
    selected_scope = request.args.get('scope')
    inferred = next((s['source'] for s in scope_catalog() if selected_scope in {s['id'], s['label']}), 'metaPee')
    source = resolve_source(request.args.get('group', inferred))
    if source is None:
        abort(404)
    ctx = context(source, 'selection')
    reviews = [dict(label=LABELS[item], current=item == source,
                    href='/study-selection?' + urlencode(dict(group=PUBLIC_SCOPES[item],
                        include_subexposures=ctx['include_subexposures']))) for item in SOURCES]
    html = render_template('study_selection.html', **ctx, flow=build_selection_flow(source), reviews=reviews)
    monthly = load_overlay(ROOT, source)
    if monthly:
        soup = BeautifulSoup(html, 'html.parser')
        section = soup.new_tag('section', attrs={'class': 'selection-notes'})
        heading = soup.new_tag('h2'); heading.string = 'Monthly updates'; section.append(heading)
        note = soup.new_tag('p')
        note.string = 'Searching is complete through ' + monthly['search_through'] + '. Monthly screening is reported separately from this historical flow diagram.'
        section.append(note)
        link = soup.new_tag('a', href='/monthly-review/' + PUBLIC_SCOPES[source])
        link.string = 'View monthly status and human-review tables'; section.append(link)
        soup.select_one('main').append(section)
        html = str(soup)
    return html


@app.get('/monthly-review/<group>')
def monthly_review(group):
    source = resolve_source(group)
    if source is None:
        abort(404)
    monthly = load_overlay(ROOT, source)
    soup = BeautifulSoup('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>metaUro · Monthly review</title><link rel="stylesheet" href="/static/theme.css"><link rel="stylesheet" href="/static/metauro.css"></head><body><main class="uro-selection"></main></body></html>', 'html.parser')
    header = BeautifulSoup(render_template('_header.html', **context(source, 'selection')), 'html.parser')
    soup.body.insert(0, header)
    main = soup.main
    title = soup.new_tag('h1'); title.string = LABELS[source] + ' monthly review'; main.append(title)
    note = soup.new_tag('p')
    note.string = ('Searching completed through ' + monthly['search_through'] + '. ' +
                   str(monthly.get('human_review_pending', 0)) + ' exposure–disease contexts await human review. ' +
                   'The historical study-selection diagram retains its original cohort counts.') if monthly else 'No monthly update has been synchronized yet.'
    main.append(note)
    for entry in (monthly or {}).get('review_files', []):
        paragraph = soup.new_tag('p')
        link = soup.new_tag('a', href='/monthly-review/' + PUBLIC_SCOPES[source] + '/download/' + entry['filename'])
        link.string = 'Download discrepancy review (CSV)'; paragraph.append(link); main.append(paragraph)
    return str(soup)


@app.get('/monthly-review/<group>/download/<filename>')
def monthly_review_download(group, filename):
    source = resolve_source(group)
    if source is None:
        abort(404)
    monthly = load_overlay(ROOT, source)
    entry = next((item for item in (monthly or {}).get('review_files', []) if item['filename'] == filename), None)
    if not entry:
        abort(404)
    path = bound(ROOT / 'updates' / source, entry['stored_path'])
    if sha(path) != entry['sha256']:
        abort(503)
    return send_from_directory(path.parent, path.name, as_attachment=True, mimetype='text/csv')


@app.route('/evidence/<source>/', defaults={'path': ''}, methods=['GET', 'POST', 'HEAD'])
@app.route('/evidence/<source>/<path:path>', methods=['GET', 'POST', 'HEAD'])
def evidence(source, path):
    source = resolve_source(source)
    if source is None:
        abort(404)
    if path in {'verify', 'exclude'}:
        return jsonify(error='Saved evidence is read-only in metaUro. Interactive study selection is available for this view.'), 403
    if request.method == 'POST' and path not in {'analyze', 'reanalyze'}:
        abort(405)
    if path in {'analyze', 'reanalyze'} and request.method == 'POST':
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error='An analysis request must be a JSON object.'), 400
        settings = payload.get('analysis_settings')
        if isinstance(settings, dict) and settings.get('baseline_source') == 'manual':
            risk = settings.get('manual_risk_percent')
            try:
                valid = not isinstance(risk, bool) and 0 < float(risk) < 100
            except (TypeError, ValueError):
                valid = False
            if not valid:
                return jsonify(error='Manual reference risk must be greater than 0% and less than 100%.'), 400
    if path == 'about':
        return redirect('/about')
    if request.method in {'GET', 'HEAD'} and (path == 'summary' or path.startswith('summary/')):
        try:
            mode = summary_mode(request.args.get('include_subexposures'))
            folder = ensure_current(ROOT, source, mode)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception:
            app.logger.exception('Summary refresh failed: %s', path)
            return jsonify(error='Summary generation failed; please retry after the evidence update completes.'), 503
        workbook_match = re.fullmatch(r'summary/workbooks/([a-z0-9_]+)/(combined|dietary)', path)
        plot_match = re.fullmatch(r'summary/plots/([a-z0-9_]+)/([a-z-]+)', path)
        if workbook_match or plot_match:
            match = workbook_match or plot_match
            manifest = json.loads((folder / 'summary_manifest.json').read_text())
            if match.group(1) not in manifest.get('sources', {}).get(source, {}).get('scopes', []):
                abort(404)
            scope = manifest.get('scopes', {}).get(match.group(1), {})
            artifact = scope.get('workbooks' if workbook_match else 'plots', {}).get(match.group(2), {})
            if not artifact.get('available'):
                abort(404)
            filename = artifact['filename']
            if plot_match:
                filename = artifact.get('locales', {}).get(request.args.get('lang'), filename)
            asset = bound(folder, filename)
            response = send_file(
                asset,
                mimetype=('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                          if workbook_match else 'application/pdf'),
                as_attachment=bool(workbook_match),
                download_name=asset.name,
            )
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-metaUro-Source'] = source
            return response
    prefix = public_prefix(source)
    try:
        if path.startswith('static/'):
            # Settings catalogs and UI assets do not need the scientific engine.
            code_static = ROOT / 'runtime_code' / source / 'static'
            static_root = code_static if path == 'static/script.js' else assembled_source(ROOT, source) / 'static'
            asset = send_from_directory(static_root, path[len('static/'):])
            asset.direct_passthrough = False
            status, response_headers, body = asset.status_code, list(asset.headers), asset.get_data()
        else:
            headers = {k: v for k, v in request.headers if k.lower() not in {'host', 'content-length', 'connection'}}
            status, response_headers, body = workers[source].request(
                path='/' + path + ('?' + request.query_string.decode() if request.query_string else ''),
                method=request.method, data=request.get_data(), headers=headers)
    except (RuntimeError, TimeoutError, EOFError, BrokenPipeError, ValueError, OSError):
        app.logger.exception('Source engine failed: %s', source)
        return jsonify(error='This evidence source is temporarily unavailable. Please try again.'), 503
    content_type = next((v for k, v in response_headers if k.lower() == 'content-type'), '')
    outgoing = [(k, v) for k, v in response_headers if k.lower() not in {'content-length', 'content-encoding', 'etag', 'set-cookie'}]
    if 'text/html' in content_type and status == 200:
        body = decorate_html(body.decode('utf-8'), source, path).encode()
    elif ('javascript' in content_type or path.endswith('.js')) and body:
        body = prefix_text(body.decode('utf-8'), prefix).encode()
        if path == 'static/script.js':
            body = body.replace(b"setSelectedExposure('Vitamin A');", b"setSelectedExposure(new URL(location.href).searchParams.get('exposure') || 'Vitamin A');")
            body = body.replace(b'metapee_extracted_studies', b'metauro_extracted_studies').replace(b'metavir_extracted_studies', b'metauro_extracted_studies')
            body = body.replace(
                b"'Quality Score', 'Verified', 'Exclusion Flags'",
                b"'Quality Score', 'VERIFY', 'Exclusion Flags'",
            )
    elif 'text/css' in content_type:
        body = prefix_text(body.decode('utf-8'), prefix).encode()
    elif 'application/json' in content_type and body and request.method != 'HEAD' and status not in {204, 304}:
        payload = json.loads(body)
        if path == 'static/i18n-translations.json' and isinstance(payload, dict):
            payload.update(UI_LABEL_TRANSLATIONS)
        monthly = load_overlay(ROOT, source)
        if isinstance(payload, dict) and 'last_updated' in payload and monthly and monthly.get('completed_at'):
            payload['last_updated'] = datetime.fromisoformat(monthly['completed_at'].replace('Z', '+00:00')).timestamp()
        if isinstance(payload, dict) and isinstance(payload.get('error'), str):
            payload['error'] = product_branding(payload['error'])
        body = json.dumps(prefix_json(payload, prefix), ensure_ascii=False).encode()
    for index, (key, value) in enumerate(outgoing):
        if key.lower() == 'location' and value.startswith('/'):
            outgoing[index] = (key, prefix + value)
        elif key.lower() == 'content-disposition':
            outgoing[index] = (key, product_branding(value))
    response = Response(body, status=status, headers=outgoing)
    if path in {'analyze', 'reanalyze'}:
        response.headers['Cache-Control'] = 'no-store'
    response.headers['X-metaUro-Source'] = source
    return response


if __name__ == '__main__':
    app.run(host='127.0.0.1' if 'PORT' not in os.environ else '0.0.0.0', port=int(os.environ.get('PORT', '5012')), debug=False, use_reloader=False)
