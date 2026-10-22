"""MetaUro: one interface over two immutable evidence collections."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from datetime import datetime
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, send_from_directory
from source_worker import SOURCES, workers
from study_selection import build_selection_flow, selection_catalog
from automation.sync_sources import assembled_source, load_overlay, bound, sha
from summary_outputs import ensure_current, summary_dir, summary_mode

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
LABELS = {'metaPee': 'Urinary tract', 'MetaVir': 'Prostate'}
PUBLIC_SCOPES = {'metaPee': 'urinary', 'MetaVir': 'prostate'}
SCOPE_PARENTS = {
    'urothelial_carcinoma_of_the_bladder': 'bladder_cancer',
    'squamous_cell_carcinoma_of_the_bladder': 'bladder_cancer',
    'adenocarcinoma_of_the_bladder': 'bladder_cancer',
    'small_cell_carcinoma_of_the_bladder': 'bladder_cancer',
    'renal_cell_carcinoma': 'kidney_and_renal_pelvis_cancer',
    'clear_cell_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'papillary_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'chromophobe_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'urothelial_carcinoma_of_the_renal_pelvis': 'kidney_and_renal_pelvis_cancer',
    'renal_sarcoma': 'kidney_and_renal_pelvis_cancer',
    'aggressive_prostate_cancer': 'prostate_cancer',
    'prostate_cancer_excluding_aggressive': 'prostate_cancer',
}
SUMMARY_MANIFESTS = {
    'metaPee': 'summary/{mode}/summary_manifest.json',
    'MetaVir': 'summary/{mode}/summary_manifest.json',
}
from display_localization import normalize_catalog

UI_LABEL_TRANSLATIONS = {
    'Bladder, kidney, renal pelvis, and prostate cancers.': {'zh-CN':'膀胱癌、肾癌、肾盂癌及前列腺癌。','zh-TW':'膀胱癌、腎癌、腎盂癌及攝護腺癌。','nl':'Blaas-, nier-, nierbekken- en prostaatkanker.','ko':'방광암, 신장암, 신우암 및 전립선암.'},
    'Settings': {'zh-CN': '设置', 'zh-TW': '設定', 'nl': 'Instellingen', 'ko': '설정'},
    'Study selection': {'zh-CN': '研究筛选', 'zh-TW': '研究篩選', 'nl': 'Studieselectie', 'ko': '연구 선정'},
    'Download': {'zh-CN': '下载', 'zh-TW': '下載', 'nl': 'Downloaden', 'ko': '다운로드'},
    'VERIFY': {'zh-CN': '核实', 'zh-TW': '驗證', 'nl': 'Verifiëren', 'ko': '검증'},
    'Egger’s test was not performed because fewer than {count} studies were available. With a small number of studies, the test generally has insufficient power to reliably detect funnel plot asymmetry.': {
        'zh-CN': '由于可用研究少于 {count} 项，未进行 Egger 检验。研究数量较少时，该检验通常没有足够的统计效能来可靠地检测漏斗图不对称。',
        'zh-TW': '由於可用研究少於 {count} 項，未進行 Egger 檢定。研究數量較少時，此檢定通常沒有足夠的統計效能來可靠地偵測漏斗圖不對稱。',
        'nl': 'De Egger-test werd niet uitgevoerd omdat minder dan {count} studies beschikbaar waren. Bij een klein aantal studies heeft de test doorgaans onvoldoende onderscheidingsvermogen om asymmetrie van het trechterdiagram betrouwbaar vast te stellen.',
        'ko': '이용 가능한 연구가 {count}개 미만이어서 Egger 검정을 수행하지 않았습니다. 연구 수가 적으면 이 검정은 일반적으로 깔때기 그림 비대칭을 신뢰성 있게 탐지할 검정력이 부족합니다.',
    },
    'Egger’s test could not be reliably estimated for this meta-analysis.': {
        'zh-CN': '无法为该荟萃分析可靠地估计 Egger 检验结果。',
        'zh-TW': '無法為此薈萃分析可靠地估計 Egger 檢定結果。',
        'nl': 'De Egger-test kon voor deze meta-analyse niet betrouwbaar worden geschat.',
        'ko': '이 메타분석에서는 Egger 검정을 신뢰성 있게 추정할 수 없었습니다.',
    },
    'Egger’s test indicates significant funnel plot asymmetry (p={p}) for the meta-analysis of {exposure} and {disease}. This finding suggests the presence of small-study effects, which may arise from publication bias or other sources of heterogeneity related to study size. The pooled estimate should therefore be interpreted with appropriate caution.': {
        'zh-CN': 'Egger 检验提示 {exposure} 与 {disease} 荟萃分析的漏斗图存在显著不对称（p={p}）。这一结果提示存在小样本研究效应，可能源于发表偏倚或其他与研究规模相关的异质性来源。因此，应谨慎解释合并估计值。',
        'zh-TW': 'Egger 檢定顯示 {exposure} 與 {disease} 薈萃分析的漏斗圖存在顯著不對稱（p={p}）。此結果提示存在小型研究效應，可能源於發表偏倚或其他與研究規模相關的異質性來源。因此，應適當謹慎解讀合併估計值。',
        'nl': 'De Egger-test wijst op significante asymmetrie van het trechterdiagram (p={p}) voor de meta-analyse van {exposure} en {disease}. Deze bevinding duidt op kleinstudie-effecten, die kunnen voortkomen uit publicatiebias of andere bronnen van heterogeniteit die samenhangen met de onderzoeksgrootte. De gepoolde schatting moet daarom met gepaste voorzichtigheid worden geïnterpreteerd.',
        'ko': 'Egger 검정은 {exposure} 노출과 {disease} 메타분석에서 유의한 깔때기 그림 비대칭을 나타냅니다(p={p}). 이 결과는 출판편향 또는 연구 규모와 관련된 다른 이질성 원인에서 발생할 수 있는 소규모 연구 효과가 있음을 시사합니다. 따라서 통합 추정치는 적절한 주의를 기울여 해석해야 합니다.',
    },
    'Egger’s test does not indicate significant funnel plot asymmetry (p={p}) for the meta-analysis of {exposure} and {disease}. There is no statistical evidence of small-study effects based on this test, although the absence of significance does not rule out publication bias.': {
        'zh-CN': 'Egger 检验未提示 {exposure} 与 {disease} 荟萃分析的漏斗图存在显著不对称（p={p}）。根据该检验，没有小样本研究效应的统计学证据，但结果不显著并不能排除发表偏倚。',
        'zh-TW': 'Egger 檢定未顯示 {exposure} 與 {disease} 薈萃分析的漏斗圖存在顯著不對稱（p={p}）。根據此檢定，沒有小型研究效應的統計證據，但結果不顯著並不能排除發表偏倚。',
        'nl': 'De Egger-test wijst niet op significante asymmetrie van het trechterdiagram (p={p}) voor de meta-analyse van {exposure} en {disease}. Deze test levert geen statistisch bewijs voor kleinstudie-effecten, hoewel het ontbreken van significantie publicatiebias niet uitsluit.',
        'ko': 'Egger 검정은 {exposure} 노출과 {disease} 메타분석에서 유의한 깔때기 그림 비대칭을 나타내지 않습니다(p={p}). 이 검정에 근거한 소규모 연구 효과의 통계적 근거는 없지만, 유의하지 않다는 사실이 출판편향을 배제하지는 않습니다.',
    },
}

EGGERS_MIN_STUDIES = 10
EGGERS_FEWER_THAN_TEN_MESSAGE = (
    'Egger’s test was not performed because fewer than 10 studies were available. '
    'With a small number of studies, the test generally has insufficient power to '
    'reliably detect funnel plot asymmetry.'
)
EGGERS_UNAVAILABLE_MESSAGE = (
    'Egger’s test could not be reliably estimated for this meta-analysis.'
)
POOL_NARRATIVE = re.compile(r'^The pooled analysis of (\d+) studies yielded\b')
POOL_TEMPLATE_OLD = 'The pooled analysis of {count} studies yielded'
POOL_TEMPLATE_NEW = 'The pooled analysis of {count} effect estimates from {publications} publications yielded'
POOL_TRANSLATION_PREFIXES = {
    'zh-CN': ('对 {count} 项研究的合并分析', '对来自 {publications} 篇文献的 {count} 个效应估计值进行合并分析'),
    'zh-TW': ('對 {count} 項研究的合併分析', '對來自 {publications} 篇文獻的 {count} 個效果估計值進行合併分析'),
    'nl': ('De gepoolde analyse van {count} studies', 'De gepoolde analyse van {count} effectschattingen uit {publications} publicaties'),
    'ko': ('{count}개 연구의 통합 분석', '{publications}편의 논문에서 나온 {count}개 효과 추정치의 통합 분석'),
}


def _add_pooling_translations(catalog):
    for old_key, translations in list(catalog.items()):
        if not old_key.startswith(POOL_TEMPLATE_OLD) or not isinstance(translations, dict):
            continue
        updated = {}
        for language, (old_prefix, new_prefix) in POOL_TRANSLATION_PREFIXES.items():
            value = translations.get(language)
            if isinstance(value, str) and value.startswith(old_prefix):
                updated[language] = new_prefix + value[len(old_prefix):]
        if updated:
            catalog[old_key.replace(POOL_TEMPLATE_OLD, POOL_TEMPLATE_NEW, 1)] = updated


def _patch_urinary_pooling_script(body):
    """Keep MetaUro's display labels accurate across language and measure toggles."""
    script = body.decode('utf-8')
    old_nonsignificant = (
        'The pooled analysis of {count} studies yielded an effect size of {estimate} '
        '(95% CI: {low}–{high}), indicating no statistically significant association '
        'between {exposure} and {disease}. Although the point estimate suggests a '
        'potential {relationship} relationship, the confidence interval includes the '
        'null. These findings were accompanied by {heterogeneity} (I² = {i2}%), {implication}.'
    )
    new_nonsignificant = old_nonsignificant.replace(POOL_TEMPLATE_OLD, POOL_TEMPLATE_NEW, 1)
    rewrites = (
        (
            r'        if (!/^The pooled analysis of \d+ studies yielded/i.test(original)) {',
            r'        const reportAware = /^The pooled analysis of \d+ effect estimates from \d+ publications yielded/i.test(original);'
            '\n' + r'        if (!reportAware && !/^The pooled analysis of \d+ studies yielded/i.test(original)) {',
        ),
        (
            r'        const countMatch = original.match(/^The pooled analysis of (\d+) studies/i);',
            r'        const countMatch = original.match(/^The pooled analysis of (\d+) (?:studies|effect estimates from (\d+) publications)/i);'
            '\n        const publications = countMatch ? countMatch[2] : null;',
        ),
        (
            '            count,\n            estimate: es.toFixed(2),',
            '            count,\n            publications,\n            estimate: es.toFixed(2),',
        ),
        (
            '            elements.resultsInterpretation.textContent = uiText(template, common);',
            "            elements.resultsInterpretation.textContent = uiText(reportAware ? template.replace('"
            + POOL_TEMPLATE_OLD + "', '" + POOL_TEMPLATE_NEW + "') : template, common);",
        ),
        (
            "            '" + old_nonsignificant + "',",
            "            (reportAware ? '" + new_nonsignificant + "' : '" + old_nonsignificant + "'),",
        ),
        (
            "            lastHeadlineStudyCount = Number(data.headline.n_studies)",
            "            lastHeadlineStudyCount = Number(data.headline.n_effect_estimates)\n"
            "                || Number(data.headline.n_studies)",
        ),
    )
    for old, new in rewrites:
        if script.count(old) != 1:
            app.logger.error('Urinary pooling-label patch did not match the served source script')
            return body
        script = script.replace(old, new, 1)
    return script.encode('utf-8')


def _patch_jbi_tooltip_script(body):
    """Read itemized answers from both saved JBI record layouts."""
    original = b'        return Object.entries(study.JBI)'
    replacement = (
        b'        const answers = study.JBI.answers;\n'
        b"        const itemized = answers && typeof answers === 'object' && !Array.isArray(answers)\n"
        b'            && Object.keys(answers).some(question => /^q\\d+$/i.test(question))\n'
        b'            ? answers : study.JBI;\n'
        b'        return Object.entries(itemized)'
    )
    if body.count(original) != 1:
        app.logger.error('JBI tooltip patch did not match the served source script')
        return body
    return body.replace(original, replacement, 1)


def _selected_pool_rows(payload, source):
    """Find the saved or request-selected inputs behind a pooled response."""
    rows = payload.get('studies') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    rows = [row for row in rows if isinstance(row, dict)]
    if rows and all('Settings Pooling Selected' in row for row in rows):
        return [row for row in rows if row.get('Settings Pooling Selected') is True]
    scope_field = ('disease_hierarchy_pooling_selected' if source == 'metaPee'
                   else 'disease_scope_pooling_selected')
    return [row for row in rows
            if row.get('subexposure_pooling_selected') is True
            and row.get(scope_field) is not False]


def _pool_input_counts(payload, source):
    rows = _selected_pool_rows(payload, source)
    if not rows:
        return None
    pmids = [str(row.get('PMID') or '').strip() for row in rows]
    if any(not pmid or pmid.casefold() in {'na', 'n/a', 'none', 'nan'} for pmid in pmids):
        return None
    return len(rows), len(set(pmids))


def _selection_pool_estimates(flow, source):
    """Count actual selected estimates in the versioned current-pool packet."""
    if not flow.get('available'):
        return None
    packet = next((item for item in flow.get('provenance', ())
                   if item.get('label') == 'Current direct-exposure pool and recorded restrictions'), None)
    if not packet:
        return None
    try:
        path = bound(ROOT, packet['path'])
        if sha(path) != packet['sha256']:
            return None
        payload = json.loads(path.read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return None
    counts = _pool_input_counts(payload, source)
    if not counts or counts[1] != flow.get('poolable_reports'):
        return None
    declared = payload.get('pooled_study_count')
    if declared is not None and declared != counts[0]:
        return None
    return counts[0]


def current_pooling_interpretation(result, source):
    """Label pooled input rows and distinct publications without recalculation."""
    if source != 'metaPee' or not isinstance(result, dict):
        return
    headline = result.get('headline')
    if not isinstance(headline, dict):
        return
    counts = _pool_input_counts(result, source)
    if not counts:
        return
    estimates, publications = counts
    declared = result.get('pooled_study_count')
    if declared is not None and declared != estimates:
        return
    result['pooled_estimate_count'] = estimates
    result['pooled_publication_count'] = publications
    headline['n_effect_estimates'] = estimates
    headline['n_publications'] = publications
    interpretation = headline.get('results_interpretation')
    if not isinstance(interpretation, str):
        return
    match = POOL_NARRATIVE.match(interpretation)
    if match and int(match.group(1)) == estimates and estimates != publications:
        headline['results_interpretation'] = POOL_NARRATIVE.sub(
            f'The pooled analysis of {estimates} effect estimates from {publications} publications yielded',
            interpretation, count=1,
        )


def current_egger_interpretation(result, analysis_request=None):
    """Return current presentation prose without mutating source evidence."""
    if not isinstance(result, dict) or not isinstance(result.get('headline'), dict):
        return None
    studies = result.get('studies') if isinstance(result.get('studies'), list) else []
    if len(studies) < EGGERS_MIN_STUDIES:
        return EGGERS_FEWER_THAN_TEN_MESSAGE
    p_value = result['headline'].get('eggers_p')
    try:
        p_value = float(p_value)
        if not math.isfinite(p_value):
            raise ValueError
    except (TypeError, ValueError):
        return EGGERS_UNAVAILABLE_MESSAGE
    request_data = analysis_request if isinstance(analysis_request, dict) else {}
    exposure = str(result.get('exposure') or request_data.get('exposure') or 'the selected exposure')
    disease = str(result.get('disease') or request_data.get('disease') or 'the selected disease')
    if p_value < 0.05:
        return (
            f'Egger’s test indicates significant funnel plot asymmetry (p={p_value:.4f}) '
            f'for the meta-analysis of {exposure} and {disease}. This finding suggests '
            'the presence of small-study effects, which may arise from publication bias '
            'or other sources of heterogeneity related to study size. The pooled estimate '
            'should therefore be interpreted with appropriate caution.'
        )
    return (
        f'Egger’s test does not indicate significant funnel plot asymmetry (p={p_value:.4f}) '
        f'for the meta-analysis of {exposure} and {disease}. There is no statistical '
        'evidence of small-study effects based on this test, although the absence of '
        'significance does not rule out publication bias.'
    )


def resolve_source(value):
    return next((source for source, public in PUBLIC_SCOPES.items() if value in {source, public}), None)


def public_prefix(source):
    return f'/evidence/{PUBLIC_SCOPES[source]}'


def product_branding(text):
    return re.sub(r'meta(?:pee|vir)', 'MetaUro', text, flags=re.IGNORECASE)


def scope_catalog():
    result = []
    for source in SOURCES:
        catalog = json.loads((assembled_source(ROOT, source) / 'static/seer-baseline.json').read_text())
        for disease in catalog['diseases']:
            scope_id = disease['id']
            result.append({
                'source': source,
                'group': LABELS[source],
                'id': scope_id,
                'label': disease['label'],
                'prefix': public_prefix(source),
                'group_id': PUBLIC_SCOPES[source],
                'parent_id': disease.get('parent_id') or SCOPE_PARENTS.get(scope_id),
            })
    by_id = {item['id']: item for item in result}
    for item in result:
        depth, parent_id, seen = 0, item['parent_id'], {item['id']}
        while parent_id:
            if parent_id in seen or parent_id not in by_id:
                raise ValueError(f'Invalid disease-scope hierarchy for {item["id"]}')
            seen.add(parent_id)
            depth += 1
            parent_id = by_id[parent_id]['parent_id']
        item['hierarchy_depth'] = depth
        parent = by_id.get(item['parent_id'])
        item['analysis_disease'] = (
            parent['label'] if item['source'] == 'MetaVir' and parent else item['label'])
    return result


def summary_scope_catalog(include_subexposures='yes'):
    """Return the cross-source scopes backed by saved summary manifests."""
    mode = 'no' if str(include_subexposures).strip().lower() in {'no', 'false', '0'} else 'yes'
    catalog = scope_catalog()
    available = set()
    for source, pattern in SUMMARY_MANIFESTS.items():
        try:
            manifest_path = ROOT / pattern.format(mode=mode)
            manifest = json.loads(manifest_path.read_text())
            scopes = manifest.get('scopes') or {}
            available.update(
                (source, scope_id)
                for scope_id, details in scopes.items()
                if isinstance(details, dict)
                and scope_id in manifest.get('sources', {}).get(source, {}).get('scopes', [])
                and (details.get('workbooks') or {}).get('combined', {}).get('available') is True
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Preserve access if a future source release briefly omits its
            # manifest; the source page will still report unavailable outputs.
            available.update((source, item['id']) for item in catalog if item['source'] == source)
    return [item for item in catalog if (item['source'], item['id']) in available]


def replace_scope_selector(soup, ctx):
    """Render one cross-repository disease hierarchy without source optgroups."""
    selector = soup.select_one('#disease, #summary-scope, #summary-disease')
    if not selector:
        return
    is_summary = selector.get('id', '').startswith('summary-')
    items = summary_scope_catalog(ctx['include_subexposures']) if is_summary else ctx['scopes']
    selected_id = ctx['current']['id'] if any(
        item['id'] == ctx['current']['id'] for item in items) else (items[0]['id'] if items else '')
    selector.clear()
    for item in items:
        option = soup.new_tag('option', value=item['id'] if is_summary else item['label'])
        option['data-source'] = item['source']
        option['data-scope-key'] = item['id']
        option['data-disease'] = item['analysis_disease']
        option['data-parent-key'] = item['parent_id'] or ''
        option['data-hierarchy-depth'] = str(item['hierarchy_depth'])
        if item['hierarchy_depth']:
            option['class'] = ['disease-subcategory']
            option['data-subcategory'] = item['id']
        if item['id'] == selected_id:
            option['selected'] = ''
        option.string = '\u00a0\u00a0' * item['hierarchy_depth'] + item['label']
        selector.append(option)


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
    soup.title.string = f"MetaUro · {'Evidence summary' if ctx['page'] == 'summary' else 'Explore evidence'}"
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
        node.string = 'MetaUro'
    for node in soup.select('.hero > p'):
        node.decompose()
    hero_title = soup.select_one('.hero h1')
    if hero_title:
        subtitle = soup.new_tag('p', attrs={'class': 'app-scope'})
        subtitle.string = 'Bladder, kidney, renal pelvis, and prostate cancers.'
        hero_title.insert_after(subtitle)
        mode = 'no' if str(ctx['include_subexposures']).lower() in {'no', 'false', '0'} else 'yes'
        manifest = json.loads((ROOT / 'summary' / mode / 'summary_manifest.json').read_text())
        published = manifest.get('sources', {}).get(source, {}).get('metadata', {}).get('generated_at', '')
        updated = soup.new_tag('p', attrs={'class': 'app-updated'})
        updated.append('Last updated: ')
        stamp = soup.new_tag('time', id='last-updated', datetime=published, attrs={'data-published-at': published, 'class': 'notranslate', 'translate': 'no'})
        stamp.string = datetime.fromisoformat(published).strftime('%Y-%m-%d %H:%M %Z') if published else 'Not recorded'
        updated.append(stamp)
        subtitle.insert_after(updated)
        identity = soup.new_tag('div', attrs={'class': 'app-identity'})
        hero_title.insert_before(identity)
        hero_title['class'] = 'notranslate'
        hero_title['translate'] = 'no'
        for node in (hero_title, subtitle, updated):
            identity.append(node.extract())
    for node in soup.select('#loading p'):
        node.string = 'Loading saved evidence and calculating the selected view…'
    baujat_download = soup.select_one('#download-baujat-png')
    if baujat_download:
        baujat_download.string = 'Download'
    for heading in soup.select('#studies-table thead th'):
        if heading.get_text(' ', strip=True).upper() == 'VERIFIED':
            heading.string = 'VERIFY'
    # The source applications retain these explanatory controls, but metaUro's
    # consolidated interface keeps the forest and extracted-study sections terse.
    for selector in ('#forest-scale-label', '#analysis-baseline-notice', '.poolability-help', '.summary-back-link', '.summary-eyebrow', '.summary-method-note', '.summary-intro > p', '.summary-section-heading p'):
        for node in soup.select(selector):
            node.decompose()
    replace_scope_selector(soup, ctx)
    if ctx['page'] == 'summary' and soup.main:
        title = soup.select_one('main h1')
        if title:
            title.string = 'Summary'
        downloads = list(soup.select('a.summary-download'))
        combined = next((node for node in downloads
                         if '/summary/workbooks/' in node.get('href', '')
                         and '/combined' in node.get('href', '')), None)
        if combined:
            combined.extract()
            combined.string = 'Download summary workbook'
            scope_heading = soup.select_one('.summary-results-heading h2')
            scope_label = scope_heading.get_text(' ', strip=True) if scope_heading else ctx['current']['label']
            combined['aria-label'] = f'Download summary workbook for {scope_label}'
            target = soup.select_one('.summary-results-heading .summary-counts') or soup.select_one('.summary-results-heading')
            if target:
                target.append(combined)
        for node in downloads:
            if node is not combined and node.parent:
                node.decompose()
        for note in soup.select('.summary-method-note'):
            if note.get_text(' ', strip=True).startswith('Summary eligibility:'):
                note.decompose()
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
            script['src'] = script['src'].split('?')[0] + '?v=20261022-pooling-counts'
        elif '/static/i18n.js' in script['src']:
            script['src'] = script['src'].split('?')[0] + '?v=20261022-pooling-counts'
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
    source = next((item['source'] for item in scope_catalog() if request.args.get('scope') in {item['id'], item['label']}), 'metaPee')
    return render_template('about.html', **context(source, 'about'))


@app.get('/study-selection')
def study_selection():
    selected_scope = request.args.get('scope')
    inferred = next((s['source'] for s in scope_catalog() if selected_scope in {s['id'], s['label']}), 'metaPee')
    source = resolve_source(request.args.get('group', inferred))
    if source is None:
        abort(404)
    ctx = context(source, 'selection')
    available = {item: set(selection_catalog(item)) for item in SOURCES}
    selection_scopes = [item for item in ctx['scopes'] if item['label'] in available[item['source']]]
    flow = build_selection_flow(source, ctx['current']['label'], request.args.get('exposure'))
    flow = {**flow, 'pooled_estimates': _selection_pool_estimates(flow, source)}
    analysis_query = {'scope': ctx['current']['id'], 'include_subexposures': 'no'}
    if flow.get('exposure'):
        analysis_query['exposure'] = flow['exposure']
    analysis_href = ctx['prefix'] + '/?' + urlencode(analysis_query)
    html = render_template('study_selection.html', **ctx, flow=flow,
                           selection_scopes=selection_scopes, analysis_href=analysis_href)
    monthly = load_overlay(ROOT, source)
    if monthly and monthly.get('search_through') and monthly.get('kind') != 'scientific_correction':
        soup = BeautifulSoup(html, 'html.parser')
        section = soup.new_tag('section', attrs={'class': 'selection-notes'})
        heading = soup.new_tag('h2'); heading.string = 'Monthly updates'; section.append(heading)
        note = soup.new_tag('p')
        note.string = 'Searching is complete through ' + monthly['search_through'] + '. Monthly screening remains separate until the reviewed update is released.'
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
    soup = BeautifulSoup('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>MetaUro · Monthly review</title><link rel="stylesheet" href="/static/theme.css"><link rel="stylesheet" href="/static/metauro.css"></head><body><main class="uro-selection"></main></body></html>', 'html.parser')
    header = BeautifulSoup(render_template('_header.html', **context(source, 'selection')), 'html.parser')
    soup.body.insert(0, header)
    main = soup.main
    title = soup.new_tag('h1'); title.string = LABELS[source] + ' monthly review'; main.append(title)
    note = soup.new_tag('p')
    if monthly and monthly.get('kind') == 'scientific_correction':
        monthly = None
    note.string = ('Searching completed through ' + monthly['search_through'] + '. ' +
                   str(monthly.get('human_review_pending', 0)) + ' exposure–disease contexts await human review. ' +
                   'The study-selection charts continue to use the released screening audit.') if monthly and monthly.get('search_through') else 'No completed monthly search release is available yet. Reviewed evidence revisions remain available.'
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
    analysis_request = None
    if source is None:
        abort(404)
    if path in {'verify', 'exclude'}:
        return jsonify(error='Saved evidence is read-only in MetaUro. Interactive study selection is available for this view.'), 403
    if request.method == 'POST' and path not in {'analyze', 'reanalyze'}:
        abort(405)
    if path in {'analyze', 'reanalyze'} and request.method == 'POST':
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error='An analysis request must be a JSON object.'), 400
        analysis_request = payload
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
            asset = send_from_directory(
                static_root, path[len('static/'):],
                conditional=path != 'static/script.js',
            )
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
        if path == 'static/i18n.js':
            body = body.replace(
                b'    function storedLanguage() {',
                b"    function storedLanguage() {\n        const requested = new URL(window.location.href).searchParams.get('lang');\n        if (SUPPORTED_LANGUAGES.has(requested)) return requested;",
            )
            body = body.replace(b'i18n-translations.json?v=1.4', b'i18n-translations.json?v=20261022-pooling-counts')
        if path == 'static/script.js':
            body = body.replace(b'    function updateLastUpdated(ts = null) {', b'    function updateLastUpdated(ts = null) {\n        if (elements.lastUpdated?.dataset.publishedAt) return;')
            body = body.replace(b"setSelectedExposure('Vitamin A');", b"setSelectedExposure(new URL(location.href).searchParams.get('exposure') || 'Vitamin A');")
            body = body.replace(b'metapee_extracted_studies', b'metauro_extracted_studies').replace(b'metavir_extracted_studies', b'metauro_extracted_studies')
            body = body.replace(
                b"'Quality Score', 'Verified', 'Exclusion Flags'",
                b"'Quality Score', 'VERIFY', 'Exclusion Flags'",
            )
            body = _patch_jbi_tooltip_script(body)
            if source == 'metaPee':
                body = _patch_urinary_pooling_script(body)
    elif 'text/css' in content_type:
        body = prefix_text(body.decode('utf-8'), prefix).encode()
    elif 'application/json' in content_type and body and request.method != 'HEAD' and status not in {204, 304}:
        payload = json.loads(body)
        if path in {'analyze', 'reanalyze'} and isinstance(payload, dict):
            current_pooling_interpretation(payload, source)
            interpretation = current_egger_interpretation(payload, analysis_request)
            if interpretation is not None:
                payload['headline']['funnel_interpretation'] = interpretation
        if path == 'static/i18n-translations.json' and isinstance(payload, dict):
            payload.update(UI_LABEL_TRANSLATIONS)
            if source == 'metaPee':
                _add_pooling_translations(payload)
            payload = normalize_catalog(payload)
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
    if path == 'static/script.js':
        response.headers['Cache-Control'] = 'no-store'
    if path in {'analyze', 'reanalyze'}:
        response.headers['Cache-Control'] = 'no-store'
    response.headers['X-metaUro-Source'] = source
    return response


if __name__ == '__main__':
    app.run(host='127.0.0.1' if 'PORT' not in os.environ else '0.0.0.0', port=int(os.environ.get('PORT', '5012')), debug=False, use_reloader=False)
