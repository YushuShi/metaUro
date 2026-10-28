(() => {
    'use strict';
    const form = document.getElementById('sample-summary-form');
    if (!form) return;
    const $ = id => document.getElementById(id);
    const status = $('sample-summary-status'), plots = $('sample-summary-plots');
    const initial = new URLSearchParams(location.search);
    const repository = document.body.dataset.sampleSizeRepository;
    const inputs = {control_exposure_percent: 'sample-control-exposure', allocation_ratio: 'sample-allocation', alpha: 'sample-alpha', power: 'sample-power', sidedness: 'sample-sidedness'};
    let catalog, controller, catalogSequence = 0, settingsChanged = false, baselineEdited = initial.has('baseline_percent');
    let lastDisease = (repository === 'MetaNeuro' ? initial.get('outcome') : '') || initial.get('disease') || initial.get('scope') || '';
    const number = value => value === null || value === undefined || String(value).trim() === '' ? NaN : Number(value);
    const normalize = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    const fmt = value => Number(value).toLocaleString(undefined, {maximumFractionDigits: 0});
    const plotColor = (value, fallback = '#2475c0') => /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(String(value || '')) ? value : fallback;
    const element = (tag, text, className) => {const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node;};
    function settings() {
        if (window.AnalysisSettings?.get) return window.AnalysisSettings.get();
        if (window.MetaNeuroSettings?.get) return window.MetaNeuroSettings.get();
        if (repository === 'MetaOnco') {
            try {return JSON.parse(localStorage.getItem('metaonco-analysis-settings-v1') || '{}');} catch (_) {return {};}
        }
        if (repository === 'MetaUro') {
            const selected = catalog?.diseases.find(item => item.value === $('sample-summary-disease').value);
            const source = selected?.source || document.body.dataset.sampleSizeSource;
            try {
                const saved = JSON.parse(localStorage.getItem(`metaUro:${source}`) || '{}');
                const scope = selected?.value || initial.get('disease') || initial.get('scope') || '';
                return {...saved, ...(saved.scope_baselines?.[normalize(scope)] || {baseline_source: 'seer', manual_risk_percent: null})};
            } catch (_) {return {};}
        }
        return {};
    }
    function params() {
        const query = new URLSearchParams(location.search);
        for (const [key, value] of Object.entries(settings())) {
            if (value !== null && typeof value !== 'object') query.set(key, String(value));
        }
        // Explicit URL settings survive catalog loading and the first data
        // request. A subsequent Settings edit makes the updated state current.
        if (!settingsChanged) for (const [key, value] of initial) query.set(key, value);
        const context = settings().baselineContext;
        if (context && typeof context === 'object') query.set('baseline_context', JSON.stringify(context));
        return query;
    }
    async function json(url, options) {
        const response = await fetch(url, options);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || payload.message || `Unable to load the summary (${response.status}).`);
        return payload;
    }
    function setStatus(message, failed = false) {status.textContent = message; status.classList.toggle('sample-error', failed);}
    function defaultBaseline() {
        const selected = catalog.diseases.find(item => item.value === $('sample-summary-disease').value);
        const value = number(selected?.baseline_percent);
        if (!baselineEdited) $('sample-baseline').value = Number.isFinite(value) ? String(value) : '';
        $('sample-baseline-note').textContent = Number.isFinite(value)
            ? `App default: ${value.toLocaleString(undefined, {maximumSignificantDigits: 6})}%.`
            : 'No default incidence is available. Enter a risk to enable prospective planning.';
    }
    async function loadCatalog(first = false) {
        const sequence = ++catalogSequence;
        controller?.abort();
        const query = params();
        // A summary-only override belongs to the selected disease. The catalog
        // must retain each other disease's own baseline when switching scopes.
        query.delete('baseline_percent');
        const next = await json(`/api/sample-size-summary/catalog?${query}`);
        if (sequence !== catalogSequence) return;
        catalog = next;
        if (!Array.isArray(catalog.diseases) || !catalog.diseases.length) throw new Error('No diseases are available for sample-size planning.');
        const select = $('sample-summary-disease');
        const wanted = select.value || lastDisease;
        select.replaceChildren(...catalog.diseases.map(item => {
            const option = element('option', item.label || item.value); option.value = item.value; return option;
        }));
        const chosen = catalog.diseases.find(item => [item.value, item.label, ...(item.aliases || [])].some(value => normalize(value) === normalize(wanted)));
        if (chosen) select.value = chosen.value;
        lastDisease = select.value;
        if (first) {
            for (const [key, id] of Object.entries(inputs)) {
                const current = settings();
                const stored = key === 'control_exposure_percent' ? current.controlExposurePercent ?? (current.exposure_rate == null ? null : Number(current.exposure_rate) * 100)
                    : key === 'allocation_ratio' ? current.controls_per_case ?? current.controlsPerCase : null;
                const value = initial.get(key) ?? stored ?? catalog.defaults?.[key];
                if (value !== null && value !== undefined) $(id).value = String(value);
            }
            if (initial.has('baseline_percent')) $('sample-baseline').value = initial.get('baseline_percent');
        }
        defaultBaseline();
        select.disabled = false;
        $('sample-summary-submit').disabled = false;
        await update();
    }
    function svgNode(tag, attributes, text) {
        const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
        for (const [key, value] of Object.entries(attributes || {})) node.setAttribute(key, String(value));
        if (text !== undefined) node.textContent = text;
        return node;
    }
    function drawChart(points, panel) {
        // A larger canvas leaves room for every exposure's visible label.
        const width = 960;
        const height = Math.max(540, points.reduce((total, p) => total + (Math.ceil(String(p.exposure).length / 30) + 1) * 15 + 16, 130));
        const box = {left: 82, right: 310, top: 40, bottom: 75};
        const plotWidth = width - box.left - box.right, plotHeight = height - box.top - box.bottom;
        const measure = panel.effect_label || panel.effect_measure || (panel.design === 'retrospective' ? 'OR' : 'RR');
        const logRatio = ['RR', 'OR'].includes(measure.toUpperCase());
        const effectX = point => logRatio ? Math.log(Number(point.effect_size)) : Number(point.effect_size);
        const axisLabel = logRatio ? `log(${measure}) (natural log)` : measure;
        const xs = points.map(effectX), ys = points.map(p => Math.log10(Number(p.total_n)));
        let xmin = Math.min(...xs, ...(logRatio ? [0] : [])), xmax = Math.max(...xs, ...(logRatio ? [0] : [])), ymin = Math.min(...ys), ymax = Math.max(...ys);
        const xpad = Math.max((xmax - xmin) * .12, Math.abs(xmin) * .04, .03);
        const ypad = Math.max((ymax - ymin) * .08, .15);
        xmin -= xpad;
        xmax += xpad; ymin -= ypad; ymax += ypad;
        const x = value => box.left + (value - xmin) / (xmax - xmin) * plotWidth;
        const y = value => box.top + (ymax - value) / (ymax - ymin) * plotHeight;
        const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, class: 'sample-chart', role: 'img', 'aria-label': `${panel.measurement === 'dietary' ? 'Dietary only' : 'All exposure measurements'}, ${panel.design}: ${axisLabel} versus log base 10 of required total sample size.`});
        svg.append(svgNode('title', {}, `Each labeled dot is one exposure. The horizontal axis is ${axisLabel}. The vertical axis is log₁₀(total participants).`));
        for (let i = 0; i <= 5; i++) {
            const xv = xmin + i / 5 * (xmax - xmin), yv = ymin + i / 5 * (ymax - ymin);
            svg.append(svgNode('line', {x1: box.left, y1: y(yv), x2: width - box.right, y2: y(yv), stroke: '#e3e9ee'}));
            svg.append(svgNode('text', {x: box.left - 12, y: y(yv) + 4, 'text-anchor': 'end', class: 'sample-tick'}, yv.toFixed(2)));
            svg.append(svgNode('text', {x: x(xv), y: height - box.bottom + 25, 'text-anchor': 'middle', class: 'sample-tick'}, Number(xv.toPrecision(3)).toString()));
        }
        svg.append(svgNode('path', {d: `M${box.left},${box.top}V${height - box.bottom}H${width - box.right}`, fill: 'none', stroke: '#81909b'}));
        if (logRatio) svg.append(svgNode('line', {x1: x(0), y1: box.top, x2: x(0), y2: height - box.bottom, stroke: '#81909b', 'stroke-dasharray': '5 4', class: 'sample-null-line'}));
        svg.append(svgNode('text', {x: box.left + plotWidth / 2, y: height - 20, 'text-anchor': 'middle', class: 'sample-axis'}, axisLabel));
        svg.append(svgNode('text', {transform: `translate(22 ${box.top + plotHeight / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'sample-axis'}, 'log₁₀(required total sample size)'));
        // Keep callouts close to their dots; place crowded labels in open rows.
        const labels = points.map(p => ({p, px: x(effectX(p)), py: y(Math.log10(Number(p.total_n)))})).sort((a, b) => a.py - b.py);
        const occupied = [];
        for (const item of labels) {
            const lines = [];
            const words = String(item.p.exposure).split(/\s+/);
            for (const word of words) {
                if (!lines.length || (lines[lines.length - 1] + ' ' + word).length > 34) lines.push(word);
                else lines[lines.length - 1] += ' ' + word;
            }
            lines.push(`N = ${fmt(item.p.total_n)}`);
            const labelHeight = lines.length * 15 + 4, labelWidth = Math.min(285, Math.max(...lines.map(line => line.length)) * 7 + 10);
            let lx = Math.min(width - labelWidth - 8, item.px + 13), ly = item.py - labelHeight / 2;
            const collides = (cx, cy) => occupied.some(other => cx < other.x + other.w + 7 && cx + labelWidth + 7 > other.x && cy < other.y + other.h + 6 && cy + labelHeight + 6 > other.y);
            let placed = false;
            for (let offset = 0; offset < plotHeight; offset += 10) {
                for (const direction of [1, -1]) {
                    const cy = Math.max(box.top, Math.min(height - box.bottom - labelHeight, ly + offset * direction));
                    if (!collides(lx, cy)) {ly = cy; placed = true; break;}
                }
                if (placed) break;
            }
            occupied.push({x: lx, y: ly, w: labelWidth, h: labelHeight});
            const dotColor = plotColor(item.p.dot_color || item.p.color);
            const labelColor = plotColor(item.p.label_color || item.p.color, dotColor);
            const group = svgNode('g', {tabindex: '0', style: `--sample-dot-color:${dotColor};--sample-label-color:${labelColor}`});
            group.append(svgNode('title', {}, `${item.p.exposure}: ${measure} ${Number(item.p.effect_size).toPrecision(5)}${logRatio ? `; log(${measure}) ${effectX(item.p).toPrecision(5)}` : ''}; ${fmt(item.p.total_n)} total participants; ${fmt(item.p.study_count)} studies.`));
            group.append(svgNode('line', {x1: item.px + 4, y1: item.py, x2: lx - 3, y2: ly + labelHeight / 2, stroke: '#91a3b1', 'stroke-width': .8}));
            group.append(svgNode('circle', {cx: item.px, cy: item.py, r: 5}));
            const text = svgNode('text', {x: lx, y: ly + 12, class: 'sample-point-label'});
            lines.forEach((line, i) => text.append(svgNode('tspan', {x: lx, dy: i === 0 ? 0 : 15}, line)));
            group.append(text); svg.append(group);
        }
        return svg;
    }
    function renderPanel(panel) {
        const card = element('article', undefined, 'sample-plot-card');
        card.append(element('h2', `${panel.measurement === 'dietary' ? 'Dietary only' : 'All exposure measurements'} · ${panel.design === 'retrospective' ? 'Retrospective' : 'Prospective'}`));
        const measure = panel.effect_label || panel.effect_measure || (panel.design === 'retrospective' ? 'OR' : 'RR');
        const logRatio = ['RR', 'OR'].includes(measure.toUpperCase());
        const points = (panel.points || []).filter(p => number(p.study_count) >= 2 && number(p.total_n) > 0 && Number.isFinite(number(p.total_n)) && Number.isFinite(number(p.effect_size)) && (!logRatio || number(p.effect_size) > 0));
        card.append(element('p', `${points.length} exposures · ${panel.design === 'retrospective' ? 'Unmatched case-control planning' : 'Prospective planning'}`, 'sample-panel-note'));
        if (panel.note) card.append(element('p', panel.note, 'sample-panel-note'));
        if (points.length) {
            const scroll = element('div', undefined, 'sample-chart-scroll'); scroll.append(drawChart(points, panel)); card.append(scroll);
            const details = element('details'); details.append(element('summary', 'View sample sizes and study counts'));
            const wrapper = element('div', undefined, 'sample-table-scroll'), table = element('table'), head = element('thead'), header = element('tr');
            ['Exposure', 'Effect size', 'Total N', 'Studies'].forEach(label => {const th = element('th', label); th.scope = 'col'; header.append(th);});
            head.append(header); table.append(head); const body = element('tbody');
            points.forEach(point => {const row = element('tr'); [point.exposure, Number(point.effect_size).toPrecision(5), fmt(point.total_n), fmt(point.study_count)].forEach((value, index) => {const cell = element('td', value); if (index === 0) cell.setAttribute('style', `color:${plotColor(point.label_color || point.color || point.dot_color)}`); row.append(cell);}); body.append(row);});
            table.append(body); wrapper.append(table); details.append(wrapper); card.append(details);
        } else card.append(element('p', panel.reason || 'No exposures have at least two eligible studies and a finite required sample size.'));
        if (panel.omitted?.length) {
            const details = element('details'); details.append(element('summary', `Unavailable sample sizes (${panel.omitted.length})`));
            const list = element('ul'); panel.omitted.forEach(item => list.append(element('li', `${item.exposure}: ${item.reason}`))); details.append(list); card.append(details);
        }
        return card;
    }
    async function update() {
        if (!form.reportValidity()) return;
        controller?.abort(); controller = new AbortController();
        const currentController = controller;
        const query = params();
        for (const [key, value] of new FormData(form)) {if (String(value).trim()) query.set(key, value); else query.delete(key);}
        // Catalog and main summary scope parameters may use different IDs.
        query.delete('scope');
        query.delete('exposure');
        const url = new URL(location.href); url.search = query.toString(); history.replaceState({}, '', url);
        setStatus('Calculating sample sizes from the selected evidence…'); plots.setAttribute('aria-busy', 'true');
        $('sample-summary-submit').disabled = true;
        try {
            const data = await json(`/api/sample-size-summary?${query}`, {signal: currentController.signal});
            if (currentController !== controller) return;
            const panels = ['all', 'dietary'].flatMap(measurement => ['prospective', 'retrospective'].map(design => data.panels?.find(panel => panel.measurement === measurement && panel.design === design) || {measurement, design, points: []}));
            plots.replaceChildren(...panels.map(renderPanel));
            setStatus(`Sample size summary updated for ${$('sample-summary-disease').selectedOptions[0]?.textContent || data.disease}.`);
            const base = number(data.baseline_percent);
            $('sample-summary-context').textContent = [Number.isFinite(base) ? `Baseline incidence ${base.toLocaleString(undefined, {maximumSignificantDigits: 6})}%` : 'Baseline incidence unavailable', `${$('sample-control-exposure').value}% exposure among controls`, `allocation 1:${$('sample-allocation').value}`, `${Number($('sample-power').value) * 100}% power`, `α = ${$('sample-alpha').value}`, `${$('sample-sidedness').value === 'one' ? 'one' : 'two'}-sided`, data.note || '', ...(data.notices || [])].filter(Boolean).join(' · ');
        } catch (error) {
            if (currentController === controller && !currentController.signal.aborted && error.name !== 'AbortError') {
                plots.replaceChildren(); setStatus(error.message, true);
            }
        } finally {
            if (currentController === controller) {plots.setAttribute('aria-busy', 'false'); $('sample-summary-submit').disabled = false;}
        }
    }
    form.addEventListener('submit', event => {event.preventDefault(); update();});
    $('sample-baseline').addEventListener('input', () => {baselineEdited = $('sample-baseline').value !== '';});
    $('sample-summary-disease').addEventListener('change', () => {
        baselineEdited = false; lastDisease = $('sample-summary-disease').value;
        if (repository === 'MetaUro') {
            settingsChanged = true;
            loadCatalog().catch(error => setStatus(error.message, true));
        } else {defaultBaseline(); update();}
    });
    let settingsTimer;
    const refresh = () => {
        settingsChanged = true;
        controller?.abort();
        clearTimeout(settingsTimer);
        settingsTimer = setTimeout(() => {
            const sequence = catalogSequence + 1;
            loadCatalog().catch(error => {if (sequence === catalogSequence) setStatus(error.message, true);});
        }, 220);
    };
    window.addEventListener('analysissettingschange', refresh);
    window.addEventListener('planning-settings-change', refresh);
    document.addEventListener('DOMContentLoaded', () => {
        (async () => {
            if (window.AnalysisSettings?.ready) await window.AnalysisSettings.ready;
            await loadCatalog(true);
        })().catch(error => setStatus(error.message, true));
    });
})();
