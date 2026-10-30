/* Carry the main calculator's current planning choices into the summary. */
(() => {
    'use strict';
    let settingsChanged = false;
    for (const name of ['analysissettingschange', 'planning-settings-change']) window.addEventListener(name, () => {settingsChanged = true;});
    document.addEventListener('click', event => {
        const link = event.target.closest('a[data-sample-size-link]');
        if (!link) return;
        const url = new URL(link.href, location.href);
        const state = window.AnalysisSettings?.get?.() || window.MetaNeuroSettings?.get?.() || {};
        if (settingsChanged) for (const [key, value] of new URLSearchParams(location.search)) {
            if (!['disease', 'scope', 'exposure'].includes(key)) url.searchParams.set(key, value);
        }
        for (const [key, value] of Object.entries(state)) {
            if (value !== null && typeof value !== 'object') url.searchParams.set(key, String(value));
        }
        if (!settingsChanged) for (const [key, value] of new URLSearchParams(location.search)) {
            if (!['disease', 'scope', 'exposure'].includes(key)) url.searchParams.set(key, value);
        }
        if (state.baselineContext) url.searchParams.set('baseline_context', JSON.stringify(state.baselineContext));
        const disease = ['sample-summary-disease', 'disease', 'summary-disease', 'summary-scope', 'selection-disease', 'selection-scope'].map(id => document.getElementById(id)).find(Boolean);
        if (disease?.value) url.searchParams.set('disease', disease.value);
        const outcome = document.getElementById('outcome');
        if (outcome?.value) url.searchParams.set('outcome', outcome.value);
        const read = (...ids) => ids.map(id => document.getElementById(id)).find(Boolean)?.value;
        const fields = {
            alpha: read('sample-alpha', 'power-alpha'),
            power: read('sample-power', 'power-value'),
            allocation_ratio: read('sample-allocation', 'power-controls-per-case', 'power-allocation-ratio') ?? state.controls_per_case ?? state.controlsPerCase,
            control_exposure_percent: read('sample-control-exposure', 'power-exposure-percentage') ?? state.controlExposurePercent ?? (state.exposure_rate == null ? undefined : state.exposure_rate * 100),
            sidedness: read('sample-sidedness') || (read('power-sides') === '1' ? 'one' : 'two'),
            arms: read('power-arms'),
        };
        for (const [key, value] of Object.entries(fields)) if (value !== undefined && value !== '') url.searchParams.set(key, String(value));
        link.href = url.pathname + url.search;
    });
})();
