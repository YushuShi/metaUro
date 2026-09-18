/* Keep estimated cases distinct from reported counts and current with Settings. */
(() => {
    'use strict';
    const selections = new WeakMap();
    function count(value, allowZero = true) {
        if (value === null || value === undefined || typeof value === 'boolean' || String(value).trim() === '') return null;
        const number = Number(String(value).replace(/,/g, ''));
        return Number.isSafeInteger(number) && number >= (allowZero ? 0 : 1) ? number : null;
    }
    function minimum(value) { return count(value) ?? 0; }
    function refresh(study, baseline) {
        const info = baseline && typeof baseline === 'object' ? baseline : {p0: baseline};
        const risk = info.p0 === null || info.p0 === undefined ? NaN : Number(info.p0);
        const reported = count(study.Cases);
        const size = ['Sample Size', 'Participants', 'total_n'].map(key => count(study[key], false)).find(value => value !== null);
        let estimated = null, provenance = null;
        if (reported === null && size !== undefined && Number.isFinite(risk) && risk >= 0 && risk <= 1) {
            const value = size * risk, floor = Math.floor(value);
            estimated = Math.min(size, value - floor === 0.5 ? floor + (floor % 2) : Math.round(value));
            provenance = Object.fromEntries(['source', 'disease', 'sex', 'race', 'age', 'years'].filter(key => key in info).map(key => [key, info[key]]));
            if (provenance.disease && typeof provenance.disease === 'object') provenance.disease = provenance.disease.id || provenance.disease.label;
            Object.assign(provenance, {method:'sample_size_times_selected_baseline_risk',total_n:size,risk,risk_percent:risk*100,rounding:'nearest_integer_ties_to_even'});
        }
        Object.assign(study, {
            'Estimated Cases':estimated,estimated_cases:estimated,
            'Cases Estimated':estimated !== null,cases_estimated:estimated !== null,
            'Estimated Cases Method':estimated !== null ? 'N × selected baseline cumulative incidence' : null,
            'Case Estimation':provenance,case_estimation:provenance,
            'Case Estimation Incidence':estimated !== null ? risk : null
        });
        return {reported,estimated,value:reported ?? estimated};
    }
    function meetsMinimum(value, cutoff) { return minimum(cutoff) === 0 || (value !== null && Number.isFinite(value) && value >= minimum(cutoff)); }
    function rememberSelection(studies) {
        document.querySelectorAll('.study-checkbox').forEach(checkbox => {
            const study = studies[Number(checkbox.dataset.index)];
            if (!study || checkbox.disabled) return;
            if (checkbox.checked !== checkbox.defaultChecked) selections.set(study, checkbox.checked);
            else selections.delete(study);
        });
    }
    function restoreSelection(studies) {
        document.querySelectorAll('.study-checkbox').forEach(checkbox => {
            const study = studies[Number(checkbox.dataset.index)];
            if (!checkbox.disabled && study && selections.has(study)) checkbox.checked = selections.get(study);
        });
    }
    const api = {count,minimum,refresh,meetsMinimum,rememberSelection,restoreSelection};
    if (typeof window !== 'undefined') window.CaseCounts = api;
    if (typeof module !== 'undefined') module.exports = api;
})();
