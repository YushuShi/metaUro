(() => {
    'use strict';
    const form = document.getElementById('sample-summary-form');
    if (!form) return;
    const $ = id => document.getElementById(id);
    const status = $('sample-summary-status'), plots = $('sample-summary-plots');
    const initial = new URLSearchParams(location.search);
    const repository = document.body.dataset.sampleSizeRepository;
    const inputs = {control_exposure_percent: 'sample-control-exposure', allocation_ratio: 'sample-allocation', alpha: 'sample-alpha', power: 'sample-power', sidedness: 'sample-sidedness', max_sample_size: 'sample-maximum-size'};
    let catalog, controller, catalogSequence = 0, settingsChanged = false, baselineEdited = initial.has('baseline_percent');
    let lastDisease = (repository === 'MetaNeuro' ? initial.get('outcome') : '') || initial.get('disease') || initial.get('scope') || '';
    const number = value => value === null || value === undefined || String(value).trim() === '' ? NaN : Number(value);
    const normalize = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    const fmt = value => Number(value).toLocaleString(undefined, {maximumFractionDigits: 0});
    const effect = value => Number(value).toFixed(3);
    const rankings = new Map();
    const plotColor = (value, fallback = '#2475c0') => /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(String(value || '')) ? value : fallback;
    const trialMarker = point => ['*', '#'].includes(point.rct_marker) ? point.rct_marker : '';
    const trialNote = point => trialMarker(point) ? `${fmt(point.rct_count)} randomized trial report(s): ${point.rct_marker === '*' ? 'all estimates share' : 'at least one estimate does not share'} the pooled effect direction.` : '';
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
    function drawChart(points, panel, maximumSampleSize) {
        // A larger canvas leaves room for every exposure's visible label.
        const width = 960;
        const height = Math.max(540, points.reduce((total, p) => total + (Math.ceil(String(p.exposure).length / 30) + 1) * 15 + 16, 130));
        const box = {left: 82, right: 310, top: 40, bottom: 75};
        const plotWidth = width - box.left - box.right, plotHeight = height - box.top - box.bottom;
        const measure = panel.effect_label || panel.effect_measure || (panel.design === 'retrospective' ? 'OR' : 'RR');
        const logRatio = ['RR', 'OR'].includes(measure.toUpperCase());
        const effectX = point => logRatio ? Math.log(Number(point.effect_size)) : Number(point.effect_size);
        const axisLabel = logRatio ? `ln(${measure})` : measure;
        const xs = points.map(effectX), ys = points.map(p => Math.log10(Number(p.total_n)));
        let xmin = Math.min(...xs, ...(logRatio ? [0] : [])), xmax = Math.max(...xs, ...(logRatio ? [0] : [])), ymin = Math.min(...ys);
        const ymax = Math.log10(maximumSampleSize);
        const xpad = Math.max((xmax - xmin) * .12, Math.abs(xmin) * .04, .03);
        const ypad = Math.max((ymax - ymin) * .08, .15);
        xmin -= xpad;
        xmax += xpad; ymin -= ypad;
        const x = value => box.left + (value - xmin) / (xmax - xmin) * plotWidth;
        const y = value => box.top + (ymax - value) / (ymax - ymin) * plotHeight;
        const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, class: 'sample-chart', role: 'img', 'aria-label': `${panel.measurement === 'dietary' ? 'Dietary only' : 'All exposure measurements'}, ${panel.design}: ${axisLabel} versus log base 10 of required total sample size.`});
        for (let i = 0; i <= 5; i++) {
            const xv = xmin + i / 5 * (xmax - xmin), yv = ymin + i / 5 * (ymax - ymin);
            svg.append(svgNode('line', {x1: box.left, y1: y(yv), x2: width - box.right, y2: y(yv), stroke: '#e3e9ee'}));
            svg.append(svgNode('text', {x: box.left - 12, y: y(yv) + 4, 'text-anchor': 'end', class: 'sample-tick'}, yv.toFixed(2)));
            svg.append(svgNode('text', {x: x(xv), y: height - box.bottom + 25, 'text-anchor': 'middle', class: 'sample-tick'}, effect(xv)));
        }
        svg.append(svgNode('path', {d: `M${box.left},${box.top}V${height - box.bottom}H${width - box.right}`, fill: 'none', stroke: '#81909b'}));
        if (logRatio) svg.append(svgNode('line', {x1: x(0), y1: box.top, x2: x(0), y2: height - box.bottom, stroke: '#81909b', 'stroke-dasharray': '5 4', class: 'sample-null-line'}));
        svg.append(svgNode('text', {x: box.left + plotWidth / 2, y: height - 20, 'text-anchor': 'middle', class: 'sample-axis'}, axisLabel));
        svg.append(svgNode('text', {transform: `translate(22 ${box.top + plotHeight / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'sample-axis'}, 'log₁₀(required total sample size)'));
        // Keep each name directly beside its dot, even in crowded regions.
        const labels = points.map(p => ({p, px: x(effectX(p)), py: y(Math.log10(Number(p.total_n)))})).sort((a, b) => a.py - b.py);
        for (const item of labels) {
            const lines = [];
            const words = String(item.p.exposure).split(/\s+/);
            for (const word of words) {
                if (!lines.length || (lines[lines.length - 1] + ' ' + word).length > 34) lines.push(word);
                else lines[lines.length - 1] += ' ' + word;
            }
            lines.push(`N = ${fmt(item.p.total_n)}`);
            const labelHeight = lines.length * 15 + 4;
            const lx = item.px + 13, ly = item.py - labelHeight / 2;
            const dotColor = plotColor(item.p.dot_color || item.p.color);
            const labelColor = plotColor(item.p.label_color || item.p.color, dotColor);
            const group = svgNode('g', {tabindex: '0', style: `--sample-dot-color:${dotColor};--sample-label-color:${labelColor}`});
            group.append(svgNode('title', {}, `${item.p.exposure}: ${measure} ${effect(item.p.effect_size)}${logRatio ? `; ln(${measure}) ${effect(effectX(item.p))}` : ''}; ${fmt(item.p.total_n)} total participants; ${fmt(item.p.study_count)} studies. ${trialNote(item.p)}`));
            group.append(svgNode('circle', {cx: item.px, cy: item.py, r: 5, fill: dotColor}));
            const text = svgNode('text', {x: lx, y: ly + 12, class: 'sample-point-label', fill: labelColor});
            lines.forEach((line, i) => {
                const span = svgNode('tspan', {x: lx, dy: i === 0 ? 0 : 15}, line);
                if (i === lines.length - 2 && trialMarker(item.p)) span.append(svgNode('tspan', {'baseline-shift': 'super', 'font-size': '9px', class: 'sample-rct-marker'}, trialMarker(item.p)));
                text.append(span);
            });
            group.append(text); svg.append(group);
        }
        return svg;
    }
    function fullSizeChart(chart, title) {
        const [,, width, height] = chart.getAttribute('viewBox').split(' ').map(Number);
        const disease = chart.getAttribute('data-disease') || '';
        const heading = [];
        for (const word of `${repository} · ${disease}`.split(/\s+/)) {
            if (!heading.length || `${heading[heading.length - 1]} ${word}`.length > 96) heading.push(word);
            else heading[heading.length - 1] += ` ${word}`;
        }
        const top = 35 + heading.length * 21, fullHeight = height + top + 70;
        const svg = svgNode('svg', {xmlns: 'http://www.w3.org/2000/svg', width, height: fullHeight, viewBox: `0 0 ${width} ${fullHeight}`, class: 'sample-chart', role: 'img', 'aria-label': title});
        svg.append(svgNode('title', {}, `${repository}: ${disease} — ${title}`));
        svg.append(svgNode('desc', {}, chart.getAttribute('data-planning-context') || ''));
        // Embed presentation styles so a downloaded SVG retains its labels,
        // colors and trial markers independently of the website stylesheet.
        svg.append(svgNode('style', {}, '.sample-chart{font-family:Arial,sans-serif}.sample-tick{font-size:12px;fill:#536573}.sample-axis{font-size:14px;font-weight:600;fill:#243746}.sample-point-label{font-size:12px;fill:var(--sample-label-color,#243746);paint-order:stroke;stroke:#fff;stroke-width:4px;stroke-linejoin:round}.sample-chart circle{fill:var(--sample-dot-color,#2475c0);stroke:#fff;stroke-width:1.5}'));
        svg.append(svgNode('rect', {width, height: fullHeight, fill: '#fff'}));
        heading.forEach((line, index) => svg.append(svgNode('text', {x: 24, y: 25 + index * 21, fill: '#243746', 'font-size': 16, 'font-weight': 600}, line)));
        svg.append(svgNode('text', {x: 24, y: top - 10, fill: '#536573', 'font-size': 14}, title));
        const drawing = svgNode('g', {transform: `translate(0 ${top})`});
        for (const child of chart.children) drawing.append(child.cloneNode(true));
        svg.append(drawing);
        [
            '* All eligible randomized-trial estimates share the pooled effect’s direction.',
            '# At least one does not, including an exactly null estimate. Direction uses point estimates, not significance.',
            'Markers use the selected evidence in this panel. Exposures without trials remain included and unmarked.'
        ].forEach((line, index) => svg.append(svgNode('text', {x: 24, y: height + top + 20 + index * 17, fill: '#536573', 'font-size': 12}, line)));
        return svg;
    }
    function downloadChart(chart, panel, title) {
        const svg = fullSizeChart(chart, title);
        const blob = new Blob(['<?xml version="1.0" encoding="UTF-8"?>\n', new XMLSerializer().serializeToString(svg)], {type: 'image/svg+xml;charset=utf-8'});
        const url = URL.createObjectURL(blob), link = element('a');
        link.href = url;
        link.download = `${normalize(repository)}-${normalize(chart.getAttribute('data-disease-key'))}-${panel.measurement}-${panel.design}-sample-size.svg`;
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    function plotLink(label, action) {
        const link = element('a', label, 'sample-plot-link');
        link.href = '#';
        link.addEventListener('click', event => {event.preventDefault(); action();});
        return link;
    }
    function viewChart(chart, panel, title) {
        const dialog = element('dialog', undefined, 'sample-fullsize-dialog');
        dialog.setAttribute('aria-label', `Full-size plot: ${title}`);
        const toolbar = element('div', undefined, 'sample-fullsize-toolbar');
        toolbar.append(element('h2', title));
        const download = plotLink('Download full-size SVG', () => downloadChart(chart, panel, title));
        const close = element('button', 'Close');
        close.type = 'button';
        close.addEventListener('click', () => dialog.close());
        toolbar.append(download, close);
        const canvas = element('div', undefined, 'sample-fullsize-canvas');
        canvas.append(fullSizeChart(chart, title));
        dialog.append(toolbar, canvas);
        dialog.addEventListener('close', () => dialog.remove());
        document.body.append(dialog); dialog.showModal();
    }
    function renderPanel(panel, maximumSampleSize) {
        const card = element('article', undefined, 'sample-plot-card');
        const title = `${panel.measurement === 'dietary' ? 'Dietary only' : 'All exposure measurements'} · ${panel.design === 'retrospective' ? 'Retrospective' : 'Prospective'}`;
        card.append(element('h2', title));
        const measure = panel.effect_label || panel.effect_measure || (panel.design === 'retrospective' ? 'OR' : 'RR');
        const logRatio = ['RR', 'OR'].includes(measure.toUpperCase());
        const tableEffect = point => logRatio ? Math.log(number(point.effect_size)) : number(point.effect_size);
        const effectHeading = logRatio ? `Effect size ln(${measure.toUpperCase()})` : 'Effect size';
        const eligible = (panel.points || []).filter(p => number(p.study_count) >= 2 && number(p.total_n) > 0 && Number.isFinite(number(p.total_n)) && Number.isFinite(number(p.effect_size)) && (!logRatio || number(p.effect_size) > 0));
        const points = eligible.filter(p => number(p.total_n) <= maximumSampleSize);
        const aboveMaximum = eligible.length - points.length;
        card.append(element('p', `${points.length} exposures · ${panel.design === 'retrospective' ? 'Unmatched case-control planning' : 'Prospective planning'} · Maximum N = ${fmt(maximumSampleSize)}${aboveMaximum ? ` · ${aboveMaximum} above maximum` : ''}`, 'sample-panel-note'));
        if (panel.note) card.append(element('p', panel.note, 'sample-panel-note'));
        if (points.length) {
            const chart = drawChart(points, panel, maximumSampleSize);
            chart.setAttribute('data-disease', $('sample-summary-disease').selectedOptions[0]?.textContent || '');
            chart.setAttribute('data-disease-key', $('sample-summary-disease').value);
            chart.setAttribute('data-planning-context', $('sample-summary-context').textContent);
            chart.setAttribute('data-maximum-sample-size', maximumSampleSize);
            const scroll = element('div', undefined, 'sample-chart-scroll'); scroll.append(chart); card.append(scroll);
            const toolbar = element('div', undefined, 'sample-panel-controls');
            const key = `${panel.measurement}-${panel.design}`;
            const selection = rankings.get(key) || {field: 'total_n', direction: 'asc'};
            toolbar.append(plotLink('View full-size plot', () => viewChart(chart, panel, title)), plotLink('Download full-size SVG', () => downloadChart(chart, panel, title)));
            card.append(toolbar);
            const details = element('details'); details.append(element('summary', 'View exposures, sample sizes and study counts'));
            const wrapper = element('div', undefined, 'sample-table-scroll'), table = element('table'), head = element('thead'), header = element('tr');
            const rankHeader = element('th', 'Rank'); rankHeader.scope = 'col'; header.append(rankHeader);
            const sortableHeaders = [];
            for (const [field, label] of [['exposure', 'Exposure'], ['effect_size', effectHeading], ['total_n', 'Total N'], ['study_count', 'Studies']]) {
                const th = element('th'), button = element('button', label, 'sample-sort-button');
                th.scope = 'col'; button.type = 'button'; button.setAttribute('data-sort-field', field);
                const indicator = element('span', '▲▼', 'sample-sort-indicator'); indicator.setAttribute('aria-hidden', 'true');
                button.append(indicator); th.append(button); header.append(th);
                sortableHeaders.push({th, button, indicator, field, label});
                button.addEventListener('click', () => {
                    selection.direction = selection.field === field && selection.direction === 'asc' ? 'desc' : 'asc';
                    selection.field = field; rank();
                });
            }
            head.append(header); table.append(head); const body = element('tbody');
            const rank = () => {
                rankings.set(key, selection);
                // Sort unrounded values on the displayed scale. The
                // scatter coordinates and included exposures stay unchanged.
                const compareNames = (a, b) => String(a.exposure).localeCompare(String(b.exposure), undefined, {sensitivity: 'base', numeric: true});
                const sortValue = point => selection.field === 'effect_size' ? tableEffect(point) : number(point[selection.field]);
                const sorted = [...points].sort((a, b) => (selection.field === 'exposure' ? compareNames(a, b) : sortValue(a) - sortValue(b)) * (selection.direction === 'desc' ? -1 : 1) || compareNames(a, b));
                for (const {th, button, indicator, field, label} of sortableHeaders) {
                    const active = field === selection.field;
                    th.setAttribute('aria-sort', active ? (selection.direction === 'asc' ? 'ascending' : 'descending') : 'none');
                    indicator.textContent = active ? (selection.direction === 'asc' ? '▲' : '▼') : '▲▼';
                    button.setAttribute('aria-label', `Sort by ${label}, ${active && selection.direction === 'asc' ? 'descending' : 'ascending'}`);
                }
                body.replaceChildren();
                sorted.forEach((point, index) => {
                    const row = element('tr');
                    [String(index + 1), point.exposure, effect(tableEffect(point)), fmt(point.total_n), fmt(point.study_count)].forEach((value, index) => {
                        const cell = element('td', value);
                        if (index === 1) {
                            cell.setAttribute('style', `color:${plotColor(point.label_color || point.color || point.dot_color)}`);
                            if (trialMarker(point)) {
                                const marker = element('sup', trialMarker(point), 'sample-rct-marker');
                                marker.setAttribute('title', trialNote(point));
                                cell.append(marker);
                            }
                        }
                        row.append(cell);
                    });
                    body.append(row);
                });
            };
            rank();
            table.append(body); wrapper.append(table); details.append(wrapper); card.append(details);
        } else card.append(element('p', aboveMaximum ? `No exposures require ${fmt(maximumSampleSize)} or fewer participants. Increase the maximum sample size to show more exposures.` : panel.reason || 'No exposures have at least two eligible studies and a finite required sample size.'));
        if (panel.omitted?.length) {
            const details = element('details'); details.append(element('summary', `Unavailable sample sizes (${panel.omitted.length})`));
            const list = element('ul'); panel.omitted.forEach(item => list.append(element('li', `${item.exposure}: ${item.reason}`))); details.append(list); card.append(details);
        }
        return card;
    }
    async function update() {
        if (!form.reportValidity()) return;
        const maximumSampleSize = number($('sample-maximum-size').value);
        if (!Number.isSafeInteger(maximumSampleSize) || maximumSampleSize < 1) {
            setStatus('Maximum sample size must be a positive whole number.', true);
            return;
        }
        controller?.abort(); controller = new AbortController();
        const currentController = controller;
        const query = params();
        for (const [key, value] of new FormData(form)) {if (String(value).trim()) query.set(key, value); else query.delete(key);}
        // A displayed app baseline is not a manual incidence override.
        if (!baselineEdited) query.delete('baseline_percent');
        // Catalog and main summary scope parameters may use different IDs.
        query.delete('scope');
        query.delete('exposure');
        const url = new URL(location.href); url.search = query.toString(); history.replaceState({}, '', url);
        setStatus('Loading sample size plots…'); plots.setAttribute('aria-busy', 'true');
        $('sample-summary-submit').disabled = true;
        try {
            const data = await json(`/api/sample-size-summary?${query}`, {signal: currentController.signal});
            if (currentController !== controller) return;
            const panels = ['all', 'dietary'].flatMap(measurement => ['prospective', 'retrospective'].map(design => data.panels?.find(panel => panel.measurement === measurement && panel.design === design) || {measurement, design, points: []}));
            const base = number(data.baseline_percent);
            $('sample-summary-context').textContent = [Number.isFinite(base) ? `Baseline incidence ${base.toLocaleString(undefined, {maximumSignificantDigits: 6})}%` : 'Baseline incidence unavailable', `${query.get('control_exposure_percent')}% exposure among controls`, `allocation 1:${query.get('allocation_ratio')}`, `${Number(query.get('power')) * 100}% power`, `α = ${query.get('alpha')}`, `${query.get('sidedness') === 'one' ? 'one' : 'two'}-sided`, `Maximum sample size ${fmt(maximumSampleSize)}`, data.note || '', ...(data.notices || [])].filter(Boolean).join(' · ');
            plots.replaceChildren(...panels.map(panel => renderPanel(panel, maximumSampleSize)));
            setStatus(`Sample size summary updated for ${$('sample-summary-disease').selectedOptions[0]?.textContent || data.disease}.`);
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
