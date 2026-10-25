let latestAnalysisRequest = 0;

let currentStudies = [];
let allStudies = []; // Store full dataset
let currentSort = { field: 'Quality Score', direction: 'asc' };
let lastHeadlineData = null; // Store original RR results for transformation
let lastHeadlineStudyCount = null;
let lastAnalysisContext = null;

const EXPOSURE_PRESENTATION_ALIASES = Object.freeze({
    'Olive oil intake': 'Olive oil',
    '橄榄油摄入量': '橄榄油',
    '橄欖油攝取量': '橄欖油',
    'Inname van olijfolie': 'Olijfolie',
    '올리브유 섭취': '올리브유'
});

function presentationText(source) {
    const text = String(source);
    return EXPOSURE_PRESENTATION_ALIASES[text] || text;
}

function uiText(source, variables = {}) {
    const translated = window.MetaPeeI18n
        ? window.MetaPeeI18n.t(source, variables)
        : String(source).replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key) => (
        Object.prototype.hasOwnProperty.call(variables, key) ? String(variables[key]) : match
    ));
    return presentationText(translated);
}
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[character]));
}
let reviewStoreWarningShown = false;

// Global Error Handler for Debugging
window.onerror = function (msg, url, line, col, error) {
    console.error("Global Error:", msg, "at", url, ":", line);
    const errorMsg = document.getElementById('error-message');
    if (errorMsg) {
        errorMsg.textContent = uiText('JavaScript Error: {message} (at line {line})', { message: msg, line });
        errorMsg.classList.remove('hidden');
    }
};

document.addEventListener('DOMContentLoaded', async () => {
    console.log("MetaPee JS Loaded");

    // Elements
    const elements = {
        outcome: document.getElementById('outcome'),
        stageFilterContainer: document.getElementById('stage-filter-container'),
        exposure: document.getElementById('exposure'),
        exposureOptions: document.getElementById('exposure-options'),
        disease: document.getElementById('disease'),
        analyzeBtn: document.getElementById('analyze-btn'),
        updateBtn: document.getElementById('update-btn'),
        loading: document.getElementById('loading'),
        results: document.getElementById('results'),
        errorMsg: document.getElementById('error-message'),
        filterTiming: document.getElementById('filter-timing'),
        filterMinCases: document.getElementById('filter-min-cases'),
        filterStage: document.getElementById('filter-stage'),
        filterQuality: document.getElementById('filter-quality'),
        filterMeasure: document.getElementById('filter-measure'),
        filterExposureType: document.getElementById('filter-exposure-type'),
        selectAllCheckbox: document.getElementById('select-all-checkbox'),
        selectAllBtn: document.getElementById('select-all-btn'),
        exportStudiesBtn: document.getElementById('export-studies-btn'),
        deselectAllBtn: document.getElementById('deselect-all-btn'),
        studiesTbody: document.querySelector('#studies-table tbody'),
        forestPlot: document.getElementById('forest-plot'),
        funnelPlot: document.getElementById('funnel-plot'),
        headlineResult: document.getElementById('headline-result'),
        pooledEs: document.getElementById('pooled-es'),
        pooledCi: document.getElementById('pooled-ci'),
        interpretation: document.getElementById('interpretation'),
        lastUpdated: document.getElementById('last-updated'),
        heterogeneityStats: document.getElementById('heterogeneity-stats'),
        valI2: document.getElementById('val-i2'),
        valTau2: document.getElementById('val-tau2'),
        valEggers: document.getElementById('val-eggers'),
        funnelInterpretation: document.getElementById('funnel-interpretation'),
        resultsInterpretation: document.getElementById('results-interpretation'),
        influenceSection: document.getElementById('influence-section'),
        baujatPlot: document.getElementById('baujat-plot'),
        looTbody: document.getElementById('loo-tbody'),
        synonymsBox: document.getElementById('synonyms-box'),
        synonymsPills: document.getElementById('synonyms-pills'),
        displayMeasure: document.getElementById('display-measure'),
        pooledLabel: document.getElementById('pooled-label'),
        pooledPowerAnalysis: document.getElementById('pooled-power-analysis'),
        pooledIncidence: document.getElementById('pooled-incidence'),
        pooledPowerEsText: document.getElementById('pooled-power-es-text'),
        pooledPowerNTotal: document.getElementById('pooled-power-n-total'),
        pooledPowerCases: document.getElementById('pooled-power-cases'),
        powerAlpha: document.getElementById('power-alpha'),
        powerValue: document.getElementById('power-value'),
        powerSides: document.getElementById('power-sides'),
        powerEffect: document.getElementById('power-effect'),
        powerArms: document.getElementById('power-arms'),
        powerTextDisplay: document.getElementById('power-text-display'),
        alphaTextDisplay: document.getElementById('alpha-text-display'),
        sidesTextDisplay: document.getElementById('sides-text-display'),
        piContainer: document.getElementById('pi-container'),
        pooledPi: document.getElementById('pooled-pi'),
        pooledPowerPerGroup: document.getElementById('pooled-power-per-group'),
        interactiveAnalysisNote: document.getElementById('interactive-analysis-note')
    };
    window.addEventListener('analysissettingsready', () => updateBaselineIncidence());
    let lastKeepLatestCohort = window.AnalysisSettings?.get().keep_latest_cohort || false;
    window.addEventListener('analysissettingsready', () => {lastKeepLatestCohort=window.AnalysisSettings.get().keep_latest_cohort;});
    window.addEventListener('analysissettingschange', () => {
        const latest = window.AnalysisSettings.get().keep_latest_cohort;
        if (latest !== lastKeepLatestCohort) {
            lastKeepLatestCohort = latest;
            if (allStudies.length || currentStudies.length) {runAnalysis();return;}
        }
        updateBaselineIncidence();
        if (currentStudies.length) {
            refreshCaseSelection();
            if (elements.updateBtn) {elements.updateBtn.disabled = false;elements.updateBtn.click();}
        } else if (elements.loading && !elements.loading.classList.contains('hidden')) runAnalysis();
    });


    function meetsDefaultJbiThreshold(study) {
        const rating = String(study['Quality Score'] || '').trim().toLowerCase();
        return rating === 'good' || rating === 'moderate';
    }

    function canonicalEffectMeasure(value) {
        const effectType = String(value || '')
            .toUpperCase()
            .replace(/[^A-Z]+/g, ' ')
            .trim();
        const aliases = {
            'RISK RATIO': 'RR',
            'RELATIVE RISK': 'RR',
            'INCIDENCE RATE RATIO': 'IRR',
            'ODDS RATIO': 'OR',
            'HAZARD RATIO': 'HR'
        };
        if (aliases[effectType]) return aliases[effectType];
        if (/\b(?:IRR|INCIDENCE RATE RATIO)\b/.test(effectType)) return 'IRR';
        if (/\b(?:OR|ODDS RATIO)\b/.test(effectType)) return 'OR';
        if (/\b(?:HR|HAZARD RATIO)\b/.test(effectType)) return 'HR';
        if (/\b(?:RR|RRR|RISK RATIO|RELATIVE RISK(?: RATIO)?)\b/.test(effectType)) return 'RR';
        return effectType;
    }

    function studyValue(study, keys, fallback = null) {
        for (const key of keys) {
            const value = study ? study[key] : null;
            if (value === undefined || value === null) continue;
            const normalized = String(value).trim().toLowerCase();
            if (!normalized || ['na', 'n/a', 'nan', 'none', 'null', '-'].includes(normalized)) continue;
            return value;
        }
        return fallback;
    }

    function studyOriginalEffect(study) {
        return {
            type: studyValue(study, ['Original Effect Type', 'Effect Type'], 'NA'),
            size: studyValue(study, ['Original Effect Size', 'Effect Size']),
            lower: studyValue(study, ['Original Lower CI', 'Lower CI']),
            upper: studyValue(study, ['Original Upper CI', 'Upper CI'])
        };
    }

    function studyStandardEffect(study) {
        return {
            type: studyValue(study, ['Effect Type', 'Original Effect Type'], 'NA'),
            size: studyValue(study, ['Effect Size', 'Original Effect Size']),
            lower: studyValue(study, ['Lower CI', 'Original Lower CI']),
            upper: studyValue(study, ['Upper CI', 'Original Upper CI'])
        };
    }

    function ratioValueToRr(value, effectType, baselineRisk) {
        return window.AnalysisSettings.convert(value,effectType,'RR',baselineRisk);
    }

    function studyConvertedEffect(study) {
        const original = studyOriginalEffect(study);
        const baselineRisk = selectedBaselineRisk();
        return {type:'RR',size:ratioValueToRr(original.size,original.type,baselineRisk),lower:ratioValueToRr(original.lower,original.type,baselineRisk),upper:ratioValueToRr(original.upper,original.type,baselineRisk),baselineRisk,conversionApplied:canonicalEffectMeasure(original.type)!=='RR',derived:true};
    }

    function formattedEffectValue(value, digits = 3) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric.toFixed(digits) : 'NA';
    }

    function hasEligibleEffectMeasurement(study) {
        const standard = studyStandardEffect(study);
        const eligibleType = ['RR', 'IRR', 'OR', 'HR']
            .includes(canonicalEffectMeasure(standard.type));
        const effect = Number(standard.size);
        const lower = Number(standard.lower);
        const upper = Number(standard.upper);
        return eligibleType && effect > 0 && lower > 0 && upper > 0 && lower <= effect && effect <= upper;
    }

    function itemizedJbiEntries(study) {
        if (!study.JBI || typeof study.JBI !== 'object' || Array.isArray(study.JBI)) return [];
        return Object.entries(study.JBI)
            .filter(([question]) => /^q\d+$/i.test(question))
            .sort(([a], [b]) => {
                const numberA = parseInt(a.replace(/[^0-9]/g, '')) || 0;
                const numberB = parseInt(b.replace(/[^0-9]/g, '')) || 0;
                return numberA - numberB;
            })
            .map(([question, answer]) => ({
                question: question.toUpperCase(),
                answer: String(answer || 'Unclear')
            }));
    }

    const jbiTooltip = document.createElement('div');
    jbiTooltip.className = 'jbi-tooltip';
    jbiTooltip.id = 'jbi-item-tooltip';
    jbiTooltip.setAttribute('role', 'tooltip');
    jbiTooltip.hidden = true;
    document.body.appendChild(jbiTooltip);

    function hideJbiTooltip() {
        jbiTooltip.hidden = true;
        jbiTooltip.replaceChildren();
    }

    function showJbiTooltip(anchor, qualityScore, entries) {
        jbiTooltip.replaceChildren();

        const heading = document.createElement('div');
        heading.className = 'jbi-tooltip-heading';
        heading.textContent = uiText('JBI Assessment:');
        jbiTooltip.appendChild(heading);

        const rating = document.createElement('div');
        rating.className = 'jbi-tooltip-rating';
        rating.textContent = uiText('Overall rating: {rating}', { rating: uiText(qualityScore) });
        jbiTooltip.appendChild(rating);

        if (entries.length) {
            const list = document.createElement('dl');
            list.className = 'jbi-tooltip-list';
            entries.forEach((entry) => {
                const question = document.createElement('dt');
                question.textContent = entry.question;
                const answer = document.createElement('dd');
                answer.textContent = uiText(entry.answer);
                list.append(question, answer);
            });
            jbiTooltip.appendChild(list);
        } else {
            const unavailable = document.createElement('div');
            unavailable.className = 'jbi-tooltip-unavailable';
            unavailable.textContent = uiText('Itemized JBI assessment is unavailable.');
            jbiTooltip.appendChild(unavailable);
        }

        jbiTooltip.hidden = false;
        jbiTooltip.style.visibility = 'hidden';
        const anchorRect = anchor.getBoundingClientRect();
        const tooltipRect = jbiTooltip.getBoundingClientRect();
        const margin = 8;
        const left = Math.min(
            window.innerWidth - tooltipRect.width - margin,
            Math.max(margin, anchorRect.left + (anchorRect.width - tooltipRect.width) / 2)
        );
        let top = anchorRect.bottom + margin;
        if (top + tooltipRect.height > window.innerHeight - margin) {
            top = Math.max(margin, anchorRect.top - tooltipRect.height - margin);
        }
        jbiTooltip.style.left = `${left}px`;
        jbiTooltip.style.top = `${top}px`;
        jbiTooltip.style.visibility = 'visible';
    }
    let exposures = [];

    function selectedExposureValue() {
        if (!elements.exposure) return '';
        const stored = elements.exposure.dataset.canonicalExposure;
        if (stored) return stored;
        const visibleValue = elements.exposure.value.trim();
        const resolved = exposures.find((exposure) => (
            exposure.toLowerCase() === visibleValue.toLowerCase()
            || uiText(exposure).toLowerCase() === visibleValue.toLowerCase()
        ));
        return resolved || visibleValue;
    }

    function setSelectedExposure(canonicalExposure) {
        if (!elements.exposure) return;
        const canonical = String(canonicalExposure || '').trim();
        if (!canonical) {
            delete elements.exposure.dataset.canonicalExposure;
            elements.exposure.value = '';
            return;
        }
        elements.exposure.dataset.canonicalExposure = canonical;
        elements.exposure.value = uiText(canonical);
    }

    function localizedAnalysisLabel(value) {
        const raw = String(value || '').trim();
        if (!raw) return raw;
        const normalized = raw.replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
        const exposureMatch = exposures.find((exposure) => (
            exposure.toLowerCase() === normalized.toLowerCase()
        ));
        if (exposureMatch) return uiText(exposureMatch);

        const candidates = [raw, normalized];
        if (normalized) {
            candidates.push(normalized.charAt(0).toUpperCase() + normalized.slice(1).toLowerCase());
        }
        for (const candidate of candidates) {
            const translated = uiText(candidate);
            if (translated !== candidate) return translated;
        }
        return normalized;
    }

    function headlineDirection(measure, increased) {
        if (measure === 'OR') return uiText(increased ? 'increased odds' : 'decreased odds');
        if (measure === 'HR') return uiText(increased ? 'increased hazard' : 'decreased hazard');
        return uiText(increased ? 'increased risk' : 'decreased risk');
    }

    function resultsDirection(measure, increased) {
        if (measure === 'OR') return uiText(increased ? 'higher odds' : 'lower odds');
        if (measure === 'HR') return uiText(increased ? 'higher hazard' : 'lower hazard');
        return uiText(increased ? 'higher risk' : 'lower risk');
    }

    function heterogeneityInterpretation(i2, includeBetweenStudy) {
        let level;
        let implication;
        if (i2 < 25) {
            level = includeBetweenStudy ? 'low between-study heterogeneity' : 'low heterogeneity';
            implication = 'indicating consistent findings across the included studies';
        } else if (i2 < 50) {
            level = includeBetweenStudy ? 'moderate between-study heterogeneity' : 'moderate heterogeneity';
            implication = 'suggesting some variability in the results across studies';
        } else if (i2 < 75) {
            level = includeBetweenStudy ? 'substantial between-study heterogeneity' : 'substantial heterogeneity';
            implication = 'reflecting considerable variability across studies';
        } else {
            level = includeBetweenStudy ? 'very high between-study heterogeneity' : 'very high heterogeneity';
            implication = 'reflecting highly inconsistent results across studies';
        }
        return { level: uiText(level), implication: uiText(implication) };
    }

    function renderHeadlineInterpretation(measure, es, low, upp) {
        if (!elements.interpretation || !lastHeadlineData || !lastHeadlineData.interpretation) return;
        elements.interpretation.classList.remove('notranslate');
        elements.interpretation.removeAttribute('translate');

        const original = String(lastHeadlineData.interpretation);

        if (/statistically significant/i.test(original)) {
            // The server determines significance from the unrounded estimate.
            // Preserve that decision instead of recalculating it from the
            // two-decimal CI displayed in the browser.
            const isSignificant = !/not statistically significant/i.test(original);
            if (!isSignificant) {
                elements.interpretation.textContent = uiText('Not statistically significant');
                return;
            }
            const increased = /increased/i.test(original) || (!/decreased/i.test(original) && es > 1);
            elements.interpretation.textContent = uiText('Statistically significant ({direction})', {
                direction: headlineDirection(measure, increased)
            });
            return;
        }

        elements.interpretation.textContent = uiText(original);
    }

    function renderResultsInterpretation(measure, es, low, upp) {
        if (!elements.resultsInterpretation || !lastHeadlineData) return;
        elements.resultsInterpretation.classList.remove('notranslate');
        elements.resultsInterpretation.removeAttribute('translate');

        const original = String(lastHeadlineData.results_interpretation || '');
        elements.resultsInterpretation.style.display = original ? 'block' : 'none';
        if (!original) {
            elements.resultsInterpretation.textContent = '';
            return;
        }

        if (!/^The pooled analysis of \d+ studies yielded/i.test(original)) {
            elements.resultsInterpretation.textContent = uiText(original);
            return;
        }

        const i2 = Number(lastHeadlineData.i2);
        if (![es, low, upp, i2].every(Number.isFinite)) {
            elements.resultsInterpretation.textContent = uiText(original);
            return;
        }

        const countMatch = original.match(/^The pooled analysis of (\d+) studies/i);
        const count = countMatch ? countMatch[1] : String(lastHeadlineStudyCount || '');
        // The saved interpretation reflects the server's unrounded test;
        // displayed CI limits may round to exactly 1.00.
        const isSignificant = !/indicating no statistically significant association/i.test(original);
        const increased = es > 1;
        // Keep the labels tied to the analysis response. A user may edit the
        // controls before changing language or effect measure.
        const exposure = localizedAnalysisLabel(
            lastAnalysisContext ? lastAnalysisContext.exposure : selectedExposureValue()
        );
        const disease = localizedAnalysisLabel(
            lastAnalysisContext ? lastAnalysisContext.disease : selectedDiseaseValue()
        );
        const common = {
            count,
            estimate: es.toFixed(2),
            low: low.toFixed(2),
            high: upp.toFixed(2),
            exposure,
            disease,
            i2: i2.toFixed(2)
        };

        if (isSignificant) {
            const heterogeneity = heterogeneityInterpretation(i2, true);
            common.direction = resultsDirection(measure, increased);
            common.heterogeneity = heterogeneity.level;
            common.implication = heterogeneity.implication;
            const template = i2 >= 50
                ? 'The pooled analysis of {count} studies yielded an overall effect size of {estimate} (95% CI: {low}–{high}), suggesting that {exposure} is associated with a {direction} of {disease}. However, this association should be interpreted with caution, as there was {heterogeneity} (I² = {i2}%), {implication}.'
                : 'The pooled analysis of {count} studies yielded an overall effect size of {estimate} (95% CI: {low}–{high}), suggesting that {exposure} is associated with a {direction} of {disease}. These findings were supported by {heterogeneity} (I² = {i2}%), {implication}.';
            elements.resultsInterpretation.textContent = uiText(template, common);
            return;
        }

        const heterogeneity = heterogeneityInterpretation(i2, false);
        common.relationship = uiText(increased ? 'positive' : 'inverse');
        common.heterogeneity = heterogeneity.level;
        common.implication = heterogeneity.implication;
        elements.resultsInterpretation.textContent = uiText(
            'The pooled analysis of {count} studies yielded an effect size of {estimate} (95% CI: {low}–{high}), indicating no statistically significant association between {exposure} and {disease}. Although the point estimate suggests a potential {relationship} relationship, the confidence interval includes the null. These findings were accompanied by {heterogeneity} (I² = {i2}%), {implication}.',
            common
        );
    }

    function renderFunnelInterpretation() {
        if (!elements.funnelInterpretation || !lastHeadlineData) return;
        elements.funnelInterpretation.classList.remove('notranslate');
        elements.funnelInterpretation.removeAttribute('translate');
        const original = String(lastHeadlineData.funnel_interpretation || '');
        if (!original) {
            elements.funnelInterpretation.textContent = uiText('No interpretation available.');
            return;
        }

        let match = original.match(
            /^Egger’s test does not indicate significant funnel plot asymmetry \(p=([^)]*)\) for the meta-analysis of (.+) and (.+)\. There is no statistical evidence of small-study effects based on this test, although the absence of significance does not rule out publication bias\.$/i
        );
        if (match) {
            elements.funnelInterpretation.textContent = uiText(
                "Egger’s test does not indicate significant funnel plot asymmetry (p={p}) for the meta-analysis of {exposure} and {disease}. There is no statistical evidence of small-study effects based on this test, although the absence of significance does not rule out publication bias.",
                {
                    p: match[1],
                    exposure: localizedAnalysisLabel(match[2]),
                    disease: localizedAnalysisLabel(match[3])
                }
            );
            return;
        }

        match = original.match(
            /^Egger’s test indicates significant funnel plot asymmetry \(p=([^)]*)\) for the meta-analysis of (.+) and (.+)\. This finding suggests the presence of small-study effects, which may arise from publication bias or other sources of heterogeneity related to study size\. The pooled estimate should therefore be interpreted with appropriate caution\.$/i
        );
        if (match) {
            elements.funnelInterpretation.textContent = uiText(
                "Egger’s test indicates significant funnel plot asymmetry (p={p}) for the meta-analysis of {exposure} and {disease}. This finding suggests the presence of small-study effects, which may arise from publication bias or other sources of heterogeneity related to study size. The pooled estimate should therefore be interpreted with appropriate caution.",
                {
                    p: match[1],
                    exposure: localizedAnalysisLabel(match[2]),
                    disease: localizedAnalysisLabel(match[3])
                }
            );
            return;
        }

        match = original.match(/^Egger's test p-value: (.+)\.$/i);
        if (match) {
            elements.funnelInterpretation.textContent = uiText("Egger's test p-value: {p}.", { p: match[1] });
            return;
        }

        match = original.match(/^Formal Egger's testing requires at least (\d+) eligible studies\.$/i);
        if (match) {
            elements.funnelInterpretation.textContent = uiText(
                "Formal Egger's testing requires at least {count} eligible studies.",
                { count: match[1] }
            );
            return;
        }

        match = original.match(/^Egger’s test was not performed because fewer than (\d+) studies were available\. With a small number of studies, the test generally has insufficient power to reliably detect funnel plot asymmetry\.$/i);
        if (match) {
            elements.funnelInterpretation.textContent = uiText(
                'Egger’s test was not performed because fewer than {count} studies were available. With a small number of studies, the test generally has insufficient power to reliably detect funnel plot asymmetry.',
                { count: match[1] }
            );
            return;
        }

        if (/^Egger’s test could not be reliably estimated for this meta-analysis\.$/i.test(original)) {
            elements.funnelInterpretation.textContent = uiText(
                'Egger’s test could not be reliably estimated for this meta-analysis.'
            );
            return;
        }

        if (/^Egger's test could not be estimated from the available studies\.$/i.test(original)) {
            elements.funnelInterpretation.textContent = uiText(
                "Egger's test could not be estimated from the available studies."
            );
            return;
        }

        if (/^No interpretation available\.$/i.test(original)) {
            elements.funnelInterpretation.textContent = uiText('No interpretation available.');
            return;
        }

        elements.funnelInterpretation.textContent = uiText(original);
    }

    window.addEventListener('metapee:languagechange', () => {
        if (!elements.exposure) return;
        const canonical = elements.exposure.dataset.canonicalExposure;
        if (canonical) setSelectedExposure(canonical);
        if (elements.exposureOptions && elements.exposureOptions.style.display === 'block') {
            elements.exposure.dispatchEvent(new Event('input'));
        }
    });

    // Verify critical elements
    for (const [name, el] of Object.entries(elements)) {
        if (!el && !['stageFilterContainer', 'funnelPlot'].includes(name)) {
            console.warn(`Critical element missing: ${name}`);
        }
    }

    // Initialize Last Updated - removed default call to avoid showing current time on load

    // Toggle Stage Filter
    function toggleStageFilter() {
        if (!elements.outcome || !elements.stageFilterContainer) return;
        const outcome = elements.outcome.value;
        if (outcome === 'Incidence') {
            elements.stageFilterContainer.classList.add('hidden');
            elements.stageFilterContainer.style.display = 'none';
        } else {
            elements.stageFilterContainer.classList.remove('hidden');
            elements.stageFilterContainer.style.display = 'block';
        }
    }

    if (elements.outcome) {
        elements.outcome.addEventListener('change', toggleStageFilter);
        toggleStageFilter();
    }

    // Exposure Input Handling
    if (elements.exposure) {
        setSelectedExposure('Vitamin A'); // Default as requested

        let synonymsMap = {}; // { "soy": {core: "...", downstream: "..."}, ... }
        let isReadOnly = false;
        let cachedExposures = [];
        let allExposures = [];
        let cachedResultsByDisease = {};
        const defaultExposurePlaceholder = elements.exposure.getAttribute('placeholder') || '';

        function safePathComponent(val) {
            return (val || '').toLowerCase()
                .replace(/[^a-z0-9]/g, '_')
                .replace(/_+/g, '_')
                .replace(/(^_|_$)/g, '');
        }

        try {
            const [expRes, synRes, configRes] = await Promise.all([
                fetch('/api/exposures'),
                fetch('/api/synonyms'),
                fetch('/api/config')
            ]);
            if (expRes.ok) {
                exposures = await expRes.json();
                allExposures = exposures.slice();
            }
            if (synRes.ok) synonymsMap = await synRes.json();
            if (configRes.ok) {
                const configData = await configRes.json();
                isReadOnly = configData.read_only;
                cachedExposures = configData.cached_exposures || [];
                cachedResultsByDisease = configData.cached_results_by_disease || {};
                if (configData.last_updated) {
                    updateLastUpdated(configData.last_updated);
                }
            }
        } catch (e) {
            console.error("Error fetching exposures/synonyms/config:", e);
        }

        if (isReadOnly && cachedExposures.length && !Object.keys(cachedResultsByDisease).length) {
            // Filter exposures list to only include those present in cachedExposures
            exposures = exposures.filter(e => {
                const safeE = safePathComponent(e);
                return cachedExposures.some(cached => {
                    const safeCached = safePathComponent(cached);
                    return safeE === safeCached || safeCached.includes(safeE) || safeE.includes(safeCached);
                });
            });

        }

        function selectedDiseaseScopeKey() {
            const option = elements.disease && elements.disease.selectedOptions
                ? elements.disease.selectedOptions[0]
                : null;
            return option ? option.dataset.scopeKey : '';
        }

        function refreshAvailableExposures() {
            const previouslySelected = selectedExposureValue();
            const scopedResults = cachedResultsByDisease[selectedDiseaseScopeKey()];
            if (isReadOnly && Array.isArray(scopedResults)) {
                const available = new Set(scopedResults.map(value => String(value).toLowerCase()));
                exposures = allExposures.filter(exposure => available.has(exposure.toLowerCase()));
            } else if (allExposures.length) {
                exposures = allExposures.slice();
            }

            const retained = exposures.find(exposure => (
                exposure.toLowerCase() === String(previouslySelected || '').toLowerCase()
            ));
            setSelectedExposure(retained || exposures[0] || '');
            elements.exposure.disabled = exposures.length === 0;
            elements.exposure.placeholder = exposures.length
                ? uiText(defaultExposurePlaceholder)
                : uiText('No saved results are available for this disease.');
            if (elements.analyzeBtn) elements.analyzeBtn.disabled = exposures.length === 0;
            updateSynonymsBox(selectedExposureValue());
            if (elements.exposureOptions) {
                elements.exposureOptions.innerHTML = '';
                elements.exposureOptions.style.display = 'none';
            }
        }

        refreshAvailableExposures();
        if (elements.disease) {
            elements.disease.addEventListener('change', refreshAvailableExposures);
        }

        // Show synonyms for current exposure value on load
        updateSynonymsBox(selectedExposureValue());

        function getSynEntry(val) {
            const lc = (val || '').toLowerCase().trim();
            // 1. Direct match with key
            let matchKey = Object.keys(synonymsMap).find(k => k.toLowerCase() === lc);

            // 2. Resolve to canonical key if the value is listed inside any core synonym list
            if (!matchKey) {
                matchKey = Object.keys(synonymsMap).find(k => {
                    const entry = synonymsMap[k];
                    if (!entry || !entry.core) return false;
                    const coreTerms = entry.core.split(',').map(t => t.trim().toLowerCase());
                    return coreTerms.includes(lc);
                });
            }

            // 3. Fallback to substring matching
            if (!matchKey) {
                matchKey = Object.keys(synonymsMap).find(k => lc.includes(k.toLowerCase()) || k.toLowerCase().includes(lc));
            }

            if (!matchKey) return null;
            const entry = synonymsMap[matchKey];
            return entry;
        }

        function cleanAndNormalizeTerms(terms, val) {
            const seen = new Set();
            const result = [];

            // Helper to clean apostrophes and trim
            const clean = (s) => (s || '').replace(/[’â€™Ã¢â‚¬â„¢]/g, "'").trim().toLowerCase();
            const valClean = clean(val);

            terms.forEach(term => {
                const cleaned = clean(term);
                if (!cleaned || cleaned === valClean || seen.has(cleaned)) {
                    return;
                }
                seen.add(cleaned);

                // Capitalize the term nicely for display
                let formatted = term.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');

                // Keep some special acronyms uppercase
                if (formatted.toLowerCase() === '5-htp') formatted = '5-HTP';
                else if (formatted.toLowerCase() === 'bcaas') formatted = 'BCAAs';
                else if (formatted.toLowerCase() === 'cbd') formatted = 'CBD';
                else if (formatted.toLowerCase() === 'hmb') formatted = 'HMB';
                else if (formatted.toLowerCase() === 'msm') formatted = 'MSM';
                else if (formatted.toLowerCase() === 'nac') formatted = 'NAC';

                result.push(formatted);
            });

            return result;
        }

        function updateSynonymsBox(val) {
            if (!elements.synonymsBox || !elements.synonymsPills) return;
            const entry = getSynEntry(val);
            if (!entry && !val) {
                elements.synonymsBox.style.display = 'none';
                return;
            }

            const canonicalVal = val.trim();
            const primaryVal = uiText(canonicalVal);
            const rawSynonyms = entry && entry.core ? entry.core.split(',') : [];
            const cleanedSynonyms = cleanAndNormalizeTerms(rawSynonyms, canonicalVal)
                .filter(term => presentationText(term).toLocaleLowerCase() !== primaryVal.toLocaleLowerCase());
            const coreTerms = [primaryVal].concat(cleanedSynonyms);

            const rawDownstream = entry && entry.downstream ? entry.downstream.split(',') : [];
            const downstreamTerms = cleanAndNormalizeTerms(rawDownstream, canonicalVal);

            const corePills = coreTerms.map((t, i) => `<span style="
                display:inline-block; padding: 3px 10px; border-radius: 20px;
                font-size: 0.8rem; font-weight: ${i === 0 ? 700 : 400};
                background: rgba(194,65,12,${i === 0 ? '0.15' : '0.06'});
                border: 1px solid rgba(194,65,12,${i === 0 ? '0.45' : '0.2'});
                color: #333;
            ">${t}</span>`).join('');

            const downstreamPills = downstreamTerms.map(t => `<span style="
                display:inline-block; padding: 3px 10px; border-radius: 20px;
                font-size: 0.8rem; font-weight: 400;
                background: rgba(100,100,100,0.07);
                border: 1px solid rgba(150,150,150,0.35);
                color: #555;
            ">${t}</span>`).join('');

            elements.synonymsPills.innerHTML = corePills + (downstreamPills ? '<span style="font-size:0.7rem;color:#999;align-self:center;padding:0 4px;">|</span>' + downstreamPills : '');
            elements.synonymsBox.style.display = (coreTerms.length > 0 || downstreamTerms.length > 0) ? 'block' : 'none';
        }

        function getDropdownMatches(val) {
            if (!val) return exposures.map(e => ({ label: e, value: e, hint: null }));

            const lc = val.toLowerCase();
            const results = [];
            const seen = new Set();

            exposures.forEach(exp => {
                if (exp.toLowerCase().includes(lc) || uiText(exp).toLowerCase().includes(lc)) {
                    results.push({ label: exp, value: exp, hint: null });
                    seen.add(exp.toLowerCase());
                }
            });

            // Also match against synonyms
            Object.entries(synonymsMap).forEach(([key, entry]) => {
                if (!entry) return;
                const synStr = typeof entry === 'string' ? entry : [entry.core, entry.downstream].filter(Boolean).join(',');
                const syns = synStr.split(',').map(s => s.trim()).filter(Boolean);
                const matchedSyn = syns.find(s => s.toLowerCase().includes(lc));
                if (!matchedSyn) return;

                // Find the canonical exposure name (case-insensitive key match)
                const canonical = exposures.find(e => e.toLowerCase() === key.toLowerCase());
                if (!canonical) return;
                if (!seen.has(canonical.toLowerCase())) {
                    results.push({ label: canonical, value: canonical, hint: matchedSyn });
                    seen.add(canonical.toLowerCase());
                }
            });

            return results;
        }

        elements.exposure.addEventListener('input', () => {
            const val = elements.exposure.value;
            const stored = elements.exposure.dataset.canonicalExposure;
            if (stored && val.trim().toLowerCase() !== stored.toLowerCase()
                && val.trim().toLowerCase() !== uiText(stored).toLowerCase()) {
                delete elements.exposure.dataset.canonicalExposure;
            }
            updateSynonymsBox(selectedExposureValue());
            const matches = getDropdownMatches(val.toLowerCase ? val : val);

            if (elements.exposureOptions) {
                elements.exposureOptions.innerHTML = '';
                if (matches.length > 0) {
                    matches.forEach(match => {
                        const div = document.createElement('div');
                        if (match.hint) {
                            div.innerHTML = `${uiText(match.label)} <span style="font-size:0.8em; opacity:0.65; font-style:italic;">— ${match.hint}</span>`;
                        } else {
                            div.textContent = uiText(match.label);
                        }
                        div.addEventListener('click', () => {
                            setSelectedExposure(match.value);
                            elements.exposureOptions.style.display = 'none';
                            updateSynonymsBox(match.value);
                        });
                        elements.exposureOptions.appendChild(div);
                    });
                    elements.exposureOptions.style.display = 'block';
                } else {
                    elements.exposureOptions.style.display = 'none';
                }
            }
        });

        elements.exposure.addEventListener('focus', () => {
            elements.exposure.dispatchEvent(new Event('input'));
        });

        document.addEventListener('click', (e) => {
            if (elements.exposureOptions && !elements.exposure.contains(e.target) && !elements.exposureOptions.contains(e.target)) {
                elements.exposureOptions.style.display = 'none';
            }
        });
    }

    // Select All Logic (Fixed Nesting)
    if (elements.selectAllCheckbox) {
        elements.selectAllCheckbox.addEventListener('change', (e) => {
            const checkboxes = document.querySelectorAll('.study-checkbox:not(:disabled)');
            checkboxes.forEach(cb => cb.checked = e.target.checked);
        });
    }

    // Delegated Change Listener for Study Checkboxes
    document.addEventListener('change', (e) => {
        if (e.target && e.target.classList.contains('study-checkbox')) {
            if (elements.selectAllCheckbox) {
                const checkboxes = document.querySelectorAll('.study-checkbox:not(:disabled)');
                const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
                elements.selectAllCheckbox.checked = checkedCount === checkboxes.length;
                elements.selectAllCheckbox.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
            }
        }
    });

    function firstStudyValue(study, keys) {
        for (const key of keys) {
            const value = study[key];
            if (value !== undefined && value !== null && String(value).trim()) {
                return String(value).trim();
            }
        }
        return '';
    }

    function studyIdentifier(study, type) {
        const isPmcid = type === 'PMCID';
        const direct = firstStudyValue(study, isPmcid
            ? ['PMCID', 'pmcid', 'PMC ID', 'pmc_id']
            : ['PMID', 'pmid']);
        const searchable = [
            direct,
            study.Study,
            study.Reference,
            study.Link,
            study.full_text_url,
            study['Full Text Link']
        ].filter(Boolean).join(' ');
        if (isPmcid) {
            const match = searchable.match(/\bPMC\s*:?\s*(\d+)\b/i);
            if (match) return `PMC${match[1]}`;
            return /^\d+$/.test(direct) ? `PMC${direct}` : '';
        }
        if (direct) {
            const directMatch = direct.match(/\d+/);
            if (directMatch) return directMatch[0];
        }
        const match = searchable.match(/(?:\bPMID\s*:?\s*|pubmed\.ncbi\.nlm\.nih\.gov\/)(\d+)\b/i);
        return match ? match[1] : '';
    }

    function csvCell(value) {
        let text = value === undefined || value === null ? '' : String(value);
        if (/^[\s]*[=+@]/.test(text) || (/^[\s]*-/.test(text) && Number.isNaN(Number(text)))) {
            text = `'${text}`;
        }
        return `"${text.replace(/"/g, '""')}"`;
    }

    function exportStudiesToCsv() {
        if (!currentStudies.length) {
            alert(uiText('No studies to export.'));
            return;
        }
        const columns = [
            'Include subexposures', 'Source exposure',
            'Row', 'Selected', 'Study', 'PMID', 'PMCID',
            'Original Effect Type', 'Original Effect Size', 'Original Lower CI', 'Original Upper CI',
            'Converted Effect Type', 'Converted Effect Size', 'Converted Lower CI', 'Converted Upper CI',
            'Conversion Applied', 'Conversion Baseline Risk (%)',
            'Sample Size', 'Cases', 'Estimated Cases', 'Estimated Cases Method', 'Case Estimation Baseline', 'Minimum Cases',
            'Quality Score', 'Verified', 'Exclusion Flags', 'Article', 'Comparison',
            'Design', 'Timing', 'Continent', 'Gender', 'Smoking Status', 'Ethnicity',
            'Exposure Measurement', 'Journal', 'Year', 'Link'
        ].map(column => uiText(column));
        const rows = currentStudies.map((study, index) => {
            const checkbox = document.querySelector(`.study-checkbox[data-index="${index}"]`);
            const original = studyOriginalEffect(study);
            const converted = studyConvertedEffect(study);
            const baselinePercent = Number(studyValue(study, ['Conversion Baseline Risk Percent']));
            return [
                window.includeSubexposuresMode ? window.includeSubexposuresMode() : 'yes',
                study['Direct Exposure'] || study.subexposure_source || '',
                index + 1,
                uiText(checkbox && checkbox.checked && !checkbox.disabled ? 'Yes' : 'No'),
                study.Study,
                studyIdentifier(study, 'PMID'),
                studyIdentifier(study, 'PMCID'),
                original.type,
                original.size,
                original.lower,
                original.upper,
                Number.isFinite(converted.size) ? converted.type : 'NA',
                Number.isFinite(converted.size) ? converted.size : 'NA',
                Number.isFinite(converted.lower) ? converted.lower : 'NA',
                Number.isFinite(converted.upper) ? converted.upper : 'NA',
                uiText(converted.conversionApplied ? 'Yes' : 'No'),
                Number.isFinite(baselinePercent) && baselinePercent > 0
                    ? baselinePercent
                    : (Number.isFinite(converted.baselineRisk) ? converted.baselineRisk * 100 : 'NA'),
                study.Participants || study['Sample Size'],
                study.Cases,
                study['Estimated Cases'],
                study['Estimated Cases Method'],
                study['Case Estimation'] ? JSON.stringify(study['Case Estimation']) : '',
                window.CaseCounts.minimum(elements.filterMinCases?.value),
                study['Quality Score'],
                study.verifications || 0,
                study.exclusion_flags || 0,
                study.Reference,
                study.comparison_type,
                study.Design,
                study.Timing,
                study.Continent,
                studyValue(study, ['Gender', 'gender'], 'NA'),
                studyValue(study, ['Smoking Status', 'smoking_status', 'Smoking status'], 'NA'),
                study.Ethnicity || study.ethnicity || 'Mixed',
                study.exposure_measurement_type || 'unclear',
                study.Journal,
                study.Year,
                study.Link
            ];
        });
        const csv = [columns, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n');
        const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' });
        const scope = [selectedDiseaseValue(), selectedExposureValue()]
            .filter(Boolean)
            .join('_')
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '_')
            .replace(/^_+|_+$/g, '');
        const downloadUrl = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = downloadUrl;
        anchor.download = `metapee_extracted_studies_subexposures-${window.includeSubexposuresMode ? window.includeSubexposuresMode() : 'yes'}${scope ? `_${scope}` : ''}.csv`;
        anchor.style.display = 'none';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    }

    // Select All / Export / Deselect All Buttons
    if (elements.selectAllBtn) {
        elements.selectAllBtn.addEventListener('click', () => {
            const checkboxes = document.querySelectorAll('.study-checkbox:not(:disabled)');
            checkboxes.forEach(cb => cb.checked = true);
            if (elements.selectAllCheckbox) {
                elements.selectAllCheckbox.checked = checkboxes.length > 0;
                elements.selectAllCheckbox.indeterminate = false;
            }
        });
    }
    if (elements.exportStudiesBtn) {
        elements.exportStudiesBtn.addEventListener('click', exportStudiesToCsv);
    }
    if (elements.deselectAllBtn) {
        elements.deselectAllBtn.addEventListener('click', () => {
            const checkboxes = document.querySelectorAll('.study-checkbox:not(:disabled)');
            checkboxes.forEach(cb => cb.checked = false);
            if (elements.selectAllCheckbox) {
                elements.selectAllCheckbox.checked = false;
                elements.selectAllCheckbox.indeterminate = false;
            }
        });
    }

    // Filtering
    if (elements.filterTiming) elements.filterTiming.addEventListener('change', applyFilters);
    if (elements.filterMinCases) elements.filterMinCases.addEventListener('input', refreshCaseSelection);
    if (elements.filterStage) elements.filterStage.addEventListener('change', applyFilters);
    if (elements.filterQuality) elements.filterQuality.addEventListener('change', applyFilters);
    if (elements.filterMeasure) elements.filterMeasure.addEventListener('change', applyFilters);
    if (elements.filterExposureType) {
        elements.filterExposureType.addEventListener('change', () => {
            applyFilters();
            if (elements.updateBtn) {
                elements.updateBtn.click();
            }
        });
    }

    // Quality chooses inclusion; all grades remain visible for review.
    function selectedQualityFilter() {
        return elements.filterQuality ? elements.filterQuality.value : 'Moderate+';
    }

    function fairQualityRequested() {
        return selectedQualityFilter() === 'All';
    }

    function qualitySelectionRow(study) {
        if (!fairQualityRequested() || study.fair_quality_opt_in_available !== true) return study;
        // The server supplies only the fields its quality-only review permits.
        return { ...study, ...(study.fair_quality_selection_overrides || {}) };
    }

    function matchesQualitySelection(study) {
        const grade = String(study['Quality Score'] || '').trim().toLowerCase();
        if (grade === 'fair') return fairQualityRequested() && study.fair_quality_opt_in_available === true;
        if (selectedQualityFilter() === 'Good') return grade === 'good';
        return grade === 'good' || grade === 'moderate';
    }

    function clearCohortSelectionForFilters() {
        if (!window.AnalysisSettings?.get()?.keep_latest_cohort) return;
        for (const study of [...allStudies, ...currentStudies]) {
            delete study.cohort_latest_excluded;
            delete study.cohort_selection_reason;
            delete study['Cohort Pooling Selected'];
            delete study['Cohort Selection Reason'];
        }
    }
    function mergeCohortSelection(data) {
        const rowKey = row => JSON.stringify([String(row.PMID || row.pmid || ''), String(row.Study || ''), ...['Effect Size','Lower CI','Upper CI'].map(k => Number(row[k]))]);
        const returned = new Map((data.studies || []).map(row => [rowKey(row), row]));
        const flagFields = new Set(['cohort_latest_excluded','cohort_selection_reason','Cohort Pooling Selected','Cohort Selection Reason','cohort_selection_evaluated']);
        const fields = [...flagFields,'Cohort Name','Cohort Publication Date','cohort_provenance','cohort_record_metadata_id','cohort_record_metadata','cohort_record_metadata_source'];
        for (const study of [...allStudies, ...currentStudies]) {
            const row = returned.get(rowKey(study));
            if (!row) continue;
            for (const key of fields) {
                if (flagFields.has(key) && window.AnalysisSettings?.get()?.keep_latest_cohort && row.cohort_selection_evaluated !== true) continue;
                if (Object.prototype.hasOwnProperty.call(row,key)) study[key] = row[key]; else delete study[key];
            }
        }
    }

    function applyFilters() {
        clearCohortSelectionForFilters();
        const timing = elements.filterTiming ? elements.filterTiming.value : 'All';
        const stage = elements.filterStage ? elements.filterStage.value : 'All';
        const measure = elements.filterMeasure ? elements.filterMeasure.value : 'All';
        const exposureType = elements.filterExposureType ? elements.filterExposureType.value : 'Anything';

        function matchesStudyDesign(study, selected) {
            const normalize = value => String(value || '').trim().toLowerCase()
                .replace(/[_\u2010-\u2015\u2212]/g, '-').replace(/\s+/g, ' ');
            const filter = normalize(selected);
            if (!filter || filter === 'all') return true;

            const design = normalize(study.Design);
            // An observational analysis within a trial retains its own recorded design.
            const trialPosition = design.search(/\b(?:rcts?|trials?)\b/);
            const observationalPosition = design.search(/\bobservational\b/);
            const observational = /\b(?:cohort|case[ -]?(?:control|referent)|cross[ -]?sectional|ecological|biomarker[ -]analysis)\b/.test(design) ||
                (observationalPosition >= 0 && (trialPosition < 0 || observationalPosition < trialPosition));
            const clinicalTrial = !observational && !/\btrial[ -]emulation\b/.test(design) && (
                trialPosition >= 0 ||
                (/\brandomi[sz]ed\b/.test(design) && !/\bnon[ -]?randomi[sz]ed\b/.test(design) &&
                    /\b(?:intervention|controlled|treatment)\b/.test(design))
            );
            if (/^clinical trials?$/.test(filter)) return clinicalTrial;
            if (filter !== 'prospective' && filter !== 'retrospective') return false;

            const temporal = filter === 'prospective' ? /\bprospective\b/ : /\bretrospective\b/;
            // Design is authoritative; Timing is only a legacy temporal fallback.
            if (/\b(?:prospective|retrospective)\b/.test(design)) return temporal.test(design);
            if (clinicalTrial) return false;
            return temporal.test(normalize(study.Timing));
        }

        currentStudies = allStudies.filter(study => {
            if (!matchesStudyDesign(study, timing)) return false;
            if (stage !== 'All' && study.Stage !== stage) return false;
            // Quality affects selection, not whether an extracted row is visible.
            if (measure !== 'All' && canonicalEffectMeasure(study['Effect Type']) !== measure) return false;

            const studyExpType = study.exposure_measurement_type || 'unclear';
            if (exposureType === 'Dietary intake only') {
                if (studyExpType !== 'dietary_intake') return false;
            } else if (exposureType === 'Human biospecimen only') {
                if (studyExpType !== 'human_biospecimen') return false;
            }
            return true;
        });

        sortCurrentStudies();
        renderStudiesTable();
    }

    function selectedDiseaseOption() {
        return elements.disease && elements.disease.selectedOptions
            ? elements.disease.selectedOptions[0]
            : null;
    }

    function selectedDiseaseValue() {
        const option = selectedDiseaseOption();
        return option && option.dataset.disease
            ? option.dataset.disease
            : (elements.disease ? elements.disease.value : '');
    }

    function selectedBaselineRisk() {
        return window.AnalysisSettings.p0();
    }

    function updateBaselineIncidence() {
        window.AnalysisSettings.syncBaseline();
        updatePooledPowerAnalysis();
    }

    if (elements.disease) {
        elements.disease.addEventListener('change', () => {
            updateBaselineIncidence();
        });
        updateBaselineIncidence();
    }

    if (elements.analyzeBtn) {
        elements.analyzeBtn.addEventListener('click', runAnalysis);
    }

    function selectedStudiesFromTable() {
        return Array.from(document.querySelectorAll('.study-checkbox'))
            .filter((checkbox) => checkbox.checked && !checkbox.disabled)
            .map((checkbox) => currentStudies[Number(checkbox.dataset.index)])
            .filter(Boolean);
    }

    async function requestReanalysis(selectedStudies, analysisContext) {
        const context = analysisContext || lastAnalysisContext || {
            disease: selectedDiseaseValue(),
            exposure: selectedExposureValue()
        };
        const response = await fetch('/reanalyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                analysis_settings: window.AnalysisSettings.get(),
                min_cases: window.CaseCounts.minimum(elements.filterMinCases?.value),
                studies: selectedStudies,
                disease: context.disease,
                exposure: context.exposure,
                outcome: elements.outcome ? elements.outcome.value : 'Incidence',
                exclude_meta: true,
                quality_filter: selectedQualityFilter(),
                allow_fair_quality: fairQualityRequested()
            })
        });
        const data = await response.json();
        if (!response.ok || data.error) {
            throw new Error(uiText(data.error || `Server returned ${response.status}: ${response.statusText}`));
        }
        return data;
    }

    // Run Analysis Function
    async function runAnalysis() {
        await window.AnalysisSettings.ready;
        const requestSequence = ++latestAnalysisRequest;
        const requestModeVersion = window.subexposureModeVersion ? window.subexposureModeVersion() : 0;
        const staleRequest = () => requestSequence !== latestAnalysisRequest || (window.subexposureModeVersion && requestModeVersion !== window.subexposureModeVersion());
        const disease = selectedDiseaseValue();
        const exposure = selectedExposureValue();
        const outcome = elements.outcome.value;
        const excludeMeta = true;

        if (!disease || !exposure) {
            alert(uiText('Please enter both disease and exposure.'));
            return;
        }

        elements.loading.classList.remove('hidden');
        elements.results.classList.add('hidden');
        elements.errorMsg.classList.add('hidden');

        try {
            const response = await fetch('/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                quality_filter: selectedQualityFilter(), allow_fair_quality: fairQualityRequested(),
                analysis_settings: window.AnalysisSettings.get(),
                min_cases: window.CaseCounts.minimum(elements.filterMinCases?.value),
                    disease,
                    exposure,
                    outcome,
                    exclude_meta: excludeMeta
                })
            });

            let data;
            try {
                data = await response.json();
            } catch (jsonErr) {
                // Ignore json parse error if not JSON
            }

            if (staleRequest()) return;
            if (!response.ok) {
                const errMsg = (data && data.error) ? data.error : `Server returned ${response.status}: ${response.statusText}`;
                throw new Error(errMsg);
            }

            if (data && data.error) {
                elements.errorMsg.textContent = data.error;
                elements.errorMsg.classList.remove('hidden');
            } else {
                if (data.review_store_available === false && !reviewStoreWarningShown) {
                    reviewStoreWarningShown = true;
                    alert(uiText('Article flag status is temporarily unavailable. The evidence results are still shown, but do not rely on the displayed flag counts.'));
                }
                allStudies = Array.isArray(data.studies) ? data.studies : [];
                applyFilters();
                const analysisContext = { disease, exposure };
                const selectedStudies = selectedStudiesFromTable();
                const displayedAnalysis = (fairQualityRequested() || window.AnalysisSettings?.get()?.keep_latest_cohort) && selectedStudies.length
                    ? await requestReanalysis(selectedStudies, analysisContext) : data;
                updateResultsUI(displayedAnalysis, analysisContext);
                elements.results.classList.remove('hidden');
            }
        } catch (e) {
            if (staleRequest() || e.name === 'AbortError') return;
            elements.errorMsg.textContent = uiText(
                'Error: {message}. Please check your connection and server status.',
                { message: e.message }
            );
            elements.errorMsg.classList.remove('hidden');
            console.error("Analyze error:", e);
        } finally {
            if (staleRequest()) return;
            elements.loading.classList.add('hidden');
        }
    }

    // Update (Re-analyze) Logic
    if (elements.updateBtn) {
        elements.updateBtn.addEventListener('click', async () => {
            const requestSequence = ++latestAnalysisRequest;
            const requestModeVersion = window.subexposureModeVersion ? window.subexposureModeVersion() : 0;
            const staleRequest = () => requestSequence !== latestAnalysisRequest || (window.subexposureModeVersion && requestModeVersion !== window.subexposureModeVersion());
            const selectedStudies = selectedStudiesFromTable();

            if (selectedStudies.length === 0) {
                updateResultsUI({headline:null, studies:currentStudies, plot_url:null, funnel_plot_url:null, baujat_plot_url:null,
                    summary_html:'<p>No studies meet the selected pooling criteria.</p>'});
                return;
            }

            elements.updateBtn.textContent = uiText('Updating...');
            elements.updateBtn.disabled = true;
            const analysisContext = lastAnalysisContext || {
                disease: selectedDiseaseValue(),
                exposure: selectedExposureValue()
            };

            try {
                const data = await requestReanalysis(selectedStudies, analysisContext);
                if (staleRequest()) return;
                updateResultsUI(data, analysisContext);
            } catch (e) {
                if (staleRequest() || e.name === 'AbortError') return;
                console.error("Update error:", e);
                alert(`${uiText('Failed to update analysis.')} ${e.message}`);
            } finally {
                elements.updateBtn.textContent = uiText('Update Analysis');
                elements.updateBtn.disabled = false;
            }
        });
    }

    function selectedCaseBaseline(study) {
        const baseline = {...window.AnalysisSettings.get(), ...window.AnalysisSettings.baseline()};
        return baseline;
    }

    function refreshCaseSelection() {
        clearCohortSelectionForFilters();
        window.CaseCounts.rememberSelection(currentStudies);
        renderStudiesTable();
        window.CaseCounts.restoreSelection(currentStudies);
    }

    window.updateStudyCounts = (index, field, value) => {
        currentStudies[index][field] = window.CaseCounts.count(value, field === 'Cases');
        if (field === 'Sample Size') currentStudies[index].Participants = currentStudies[index][field];
        refreshCaseSelection();
    };

    // Render Table
    function renderStudiesTable() {
        if (!elements.studiesTbody) return;
        elements.studiesTbody.innerHTML = '';

        currentStudies.forEach((study, index) => {
            const sourceStudy = study;
            study = qualitySelectionRow(study);
            const tr = document.createElement('tr');
            const outcomeTerm = 'Cases';
            const originalEffect = studyOriginalEffect(study);
            const convertedEffect = studyConvertedEffect(study);
            const convertedEffectLabel = convertedEffect.conversionApplied
                ? uiText('Converted RR')
                : uiText('Analysis RR');
            const convertedBaselinePercent = Number.isFinite(convertedEffect.baselineRisk)
                ? (convertedEffect.baselineRisk * 100).toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
                : 'NA';
            const studyGender = studyValue(study, ['Gender', 'gender'], 'NA');
            const studySmokingStatus = studyValue(
                study,
                ['Smoking Status', 'smoking_status', 'Smoking status'],
                'NA'
            );

            if (study.verification_status === 'review_requested') tr.style.backgroundColor = '#fff8e1';


            const caseCounts = window.CaseCounts.refresh(study, selectedCaseBaseline(study));
            if (study !== sourceStudy) window.CaseCounts.refresh(sourceStudy, selectedCaseBaseline(sourceStudy));
            const casesVal = caseCounts.reported ?? NaN;
            const estCasesVal = caseCounts.estimated ?? NaN;
            const finalCasesVal = caseCounts.value ?? NaN;
            const passesMinimum = window.CaseCounts.meetsMinimum(caseCounts.value, elements.filterMinCases?.value);
            const eligibleEffectMeasurement = hasEligibleEffectMeasurement(study);
            const minCases = elements.filterMinCases ? (parseInt(elements.filterMinCases.value) || 0) : 0;
            const qualityScore = String(study['Quality Score'] || 'Fair');
            const qualityIsEligible = matchesQualitySelection(study);
            const sampleSizeValue = Number(String(study['Sample Size'] ?? '').replace(/,/g, ''));
            const hasRequiredSampleSize = Number.isInteger(sampleSizeValue) && sampleSizeValue > 0;
            const jbiEntries = itemizedJbiEntries(study);
            const isChecked = (study.cohort_latest_excluded !== true && study.disease_hierarchy_pooling_selected !== false && study.subexposure_pooling_selected !== false && qualityIsEligible && hasRequiredSampleSize && eligibleEffectMeasurement && passesMinimum) ? 'checked' : '';

            // Build Exclusion Reason Title
            let unselectedReason = "";
            if (!isChecked) {
                if (study.cohort_latest_excluded === true) unselectedReason = study.cohort_selection_reason;
                else 
                if (study.disease_hierarchy_pooling_selected === false && study.disease_hierarchy_pooling_reason) unselectedReason = study.disease_hierarchy_pooling_reason;
                else if (study.subexposure_pooling_selected === false && study.subexposure_pooling_reason) unselectedReason = study.subexposure_pooling_reason;
                else                 if (!qualityIsEligible) unselectedReason = uiText('Excluded by default: JBI rating is below Moderate');
                else if (!eligibleEffectMeasurement) unselectedReason = uiText('Excluded from meta-analysis: effect measurement must be RR, IRR, OR, or HR');
                else if (isNaN(finalCasesVal)) unselectedReason = uiText('Excluded: Cases not specified or invalid');
                else if (finalCasesVal < minCases) {
                    const isEst = isNaN(casesVal);
                    unselectedReason = uiText('Excluded: Cases < {minimum}{estimated}', {
                        minimum: minCases,
                        estimated: isEst ? uiText(' (estimated)') : ''
                    });
                }
            }

            if (study.verification_status === 'review_requested') {
                tr.style.backgroundColor = '#fff8e1';
            } else if (!isChecked) {
                tr.style.opacity = '0.6'; // Make initially excluded rows look slightly faded
                tr.style.backgroundColor = 'rgba(200, 200, 200, 0.15)';
            }

            tr.innerHTML = `
                <td style="text-align: center; padding: 0.75rem; vertical-align: middle;">
                    <button class="expand-btn" data-index="${index}" style="background: none; border: none; color: var(--primary); padding: 0; cursor: pointer; font-size: 1.1rem; width: 100%; display: flex; align-items: center; justify-content: center; transition: transform 0.2s;" onclick="window.toggleStudyDetails(${index}, this)">▶</button>
                </td>
                <td><input type="checkbox" class="study-checkbox" data-index="${index}" ${passesMinimum && study.cohort_latest_excluded !== true ? '' : 'disabled'} ${isChecked} ${study.display_only === true || study.subexposure_pooling_selected === false || (qualityScore.toLowerCase() === 'fair' && !study.fair_quality_opted_in) ? 'disabled' : ''}></td>
                <td>${index + 1}</td>
                <td title="${unselectedReason}">
                    <a href="${study.Link}" target="_blank" class="notranslate" translate="no" style="color: var(--primary); text-decoration: none; font-weight: 800;">
                        ${(() => {
                            let parts = study.Study.split(' ');
                            let first = parts[0].toLowerCase();
                            if (first === 'van' || first === 'de' || first === 'la' || first === 'al') {
                                let nameParts = [];
                                for (let p of parts) {
                                    if (p === 'et' || p.startsWith('(')) break;
                                    nameParts.push(p);
                                }
                                if (nameParts.length > 1) nameParts.pop();
                                return nameParts.join(' ') + ' et al.';
                            }
                            return parts[0] + ' et al.';
                        })()}
                    </a>
                    ${/^[0-9a-f]{64}$/.test(String(study.disease_hierarchy_projection_id || '')) ? `<div class="disease-evidence-origin" style="font-size:0.72em; margin-top:4px;" title="${escapeHtml(study.disease_hierarchy_pooling_reason || '')}">${escapeHtml(study['Disease Evidence Source'])}${study.disease_hierarchy_inherited === true ? ' · ' + uiText('included in general category') : ''}${study.disease_hierarchy_pooling_selected === false ? ' · ' + uiText('not selected for pooling') : ''}</div>` : ''}
                </td>
                <td>
                    <div style="display: flex; flex-direction: column; gap: 2px;">
                        <div style="display: flex; align-items: center; gap: 2px;">
                            <span class="notranslate" translate="no" style="font-size: 0.7em; min-width: 20px;" title="${uiText('Original effect reported by the paper')}">${originalEffect.type}</span>
                            <input type="number" step="0.001" value="${formattedEffectValue(originalEffect.size)}"
                                   style="width: 60px; font-size: 0.8em;"
                                   aria-label="${uiText('Original Effect Size')}"
                                   onchange="currentStudies[${index}]['Original Effect Size'] = parseFloat(this.value); currentStudies[${index}]['Effect Size'] = parseFloat(this.value)">
                        </div>
                        <div style="font-size: 0.65em; color: #777; padding-left: 2px;"
                             title="${uiText('Analysis RR using {risk}% selected finite-interval reference risk', { risk: convertedBaselinePercent })}">
                            ${convertedEffectLabel}: <span class="notranslate" translate="no">${formattedEffectValue(convertedEffect.size)}</span>
                        </div>
                    </div>
                </td>
                <td>
                    <div style="display: flex; flex-direction: column; gap: 2px;">
                        <div style="display: flex; gap: 2px;">
                            <input type="number" step="0.001" value="${formattedEffectValue(originalEffect.lower)}" aria-label="${uiText('Original Lower CI')}" style="width: 50px; font-size: 0.8em;" onchange="currentStudies[${index}]['Original Lower CI'] = parseFloat(this.value); currentStudies[${index}]['Lower CI'] = parseFloat(this.value)">
                            <input type="number" step="0.001" value="${formattedEffectValue(originalEffect.upper)}" aria-label="${uiText('Original Upper CI')}" style="width: 50px; font-size: 0.8em;" onchange="currentStudies[${index}]['Original Upper CI'] = parseFloat(this.value); currentStudies[${index}]['Upper CI'] = parseFloat(this.value)">
                        </div>
                        <div style="font-size: 0.65em; color: #777;">
                            ${uiText('Analysis RR 95% CI')}: <span class="notranslate" translate="no">${formattedEffectValue(convertedEffect.lower)}–${formattedEffectValue(convertedEffect.upper)}</span>
                        </div>
                    </div>
                </td>
                <td>
                    <div style="display:flex; flex-direction:column; gap:3px;">
                        <div style="display:flex; align-items:center; gap:3px; font-size:0.75em;">
                            <span style="opacity:0.65;">N:</span>
                            <input type="number" step="1" value="${parseInt(String(study.Participants || study['Sample Size'] || '').replace(/,/g,'')||0) || ''}"
                                   style="width:68px; font-size:0.95em;"
                                   onchange="window.updateStudyCounts(${index}, 'Sample Size', this.value)">
                        </div>
                        <div style="display:flex; align-items:center; gap:3px; font-size:0.75em;">
                            <span style="opacity:0.65;">${outcomeTerm}:</span>
                            <input type="number" step="1"
                                   value="${caseCounts.reported ?? ''}"
                                   placeholder="${study['Estimated Cases'] !== undefined && study['Estimated Cases'] !== null ? 'est. ' + study['Estimated Cases'] : ''}"
                                   style="width:68px; font-size:0.95em;"
                                   onchange="window.updateStudyCounts(${index}, 'Cases', this.value)">
                        </div>
                    </div>
                </td>
                <td>
                    <div class="quality-badge quality-${qualityScore.toLowerCase()}"
                         tabindex="0" aria-describedby="jbi-item-tooltip"
                         style="font-size: 0.7rem; padding: 2px 6px; border-radius: 12px; color: white;">
                         ${uiText(qualityScore)}
                    </div>
                </td>
                <td>
                    <span class="review-count" title="Saved verification count" aria-label="Verified: ${study.verifications || 0}">✓ ${study.verifications || 0}</span>
                </td>
                <td>
                    <span class="review-count review-count--exclude" title="Saved exclusion-flag count" aria-label="Exclude flags: ${study.exclusion_flags || 0}">⚑ ${study.exclusion_flags || 0}</span>
                </td>
                <td class="notranslate" translate="no" style="font-size: 0.75em;" title="${unselectedReason}">${study.Reference || '-'}</td>
                <td style="font-size: 0.75em;" title="${unselectedReason}">
                    <b>Context:</b> ${study.comparison_type || '-'}<br>
                    <b>Design:</b> ${study.Design || '-'}<br>
                    <b>Location:</b> ${study.Continent || '-'}<br>
                    <b>${uiText('Gender')}:</b> ${studyGender}<br>
                    <b>${uiText('Smoking Status')}:</b> ${studySmokingStatus}<br>
                    <b>Ethnicity:</b> ${study.Ethnicity || study.ethnicity || 'Mixed'}<br>
                    <b>Exposure quantification:</b><br>
                    <select ${study.exposure_measurement_overridden === true ? 'disabled title="User-approved exposure classification"' : ''} onchange="currentStudies[${index}]['exposure_measurement_type'] = this.value; (function(selectEl) {
                        const val = selectEl.value;
                        if (val === 'dietary_intake') selectEl.style.color = '#2e7d32';
                        else if (val === 'human_biospecimen') selectEl.style.color = '#c2410c';
                        else selectEl.style.color = '#e65100';
                    })(this)" style="font-weight: bold; border: 1px solid #ccc; border-radius: 4px; padding: 2px; font-size: 0.8em; max-width: 100%; box-sizing: border-box; color: ${
                        (() => {
                            const t = study.exposure_measurement_type || 'unclear';
                            if (t === 'dietary_intake') return '#2e7d32';
                            if (t === 'human_biospecimen') return '#c2410c';
                            return '#e65100';
                        })()
                    }">
                        <option value="unclear" ${(!study.exposure_measurement_type || study.exposure_measurement_type === 'unclear') ? 'selected' : ''}>Unclear</option>
                        <option value="dietary_intake" ${study.exposure_measurement_type === 'dietary_intake' ? 'selected' : ''}>Dietary intake only</option>
                        <option value="human_biospecimen" ${study.exposure_measurement_type === 'human_biospecimen' ? 'selected' : ''}>Biospecimen</option>
                    </select>
                </td>
                <td class="notranslate" translate="no" style="font-size: 0.75em;" title="${unselectedReason}">${study.Journal || '-'}</td>
                <td class="notranslate" translate="no" style="font-size: 0.75em;" title="${unselectedReason}">${study.Year || '-'}</td>
            `;

            const qualityBadge = tr.querySelector('.quality-badge');
            if (qualityBadge) {
                const showTooltip = () => showJbiTooltip(qualityBadge, qualityScore, jbiEntries);
                qualityBadge.addEventListener('mouseenter', showTooltip);
                qualityBadge.addEventListener('mouseleave', hideJbiTooltip);
                qualityBadge.addEventListener('focus', showTooltip);
                qualityBadge.addEventListener('blur', hideJbiTooltip);
                qualityBadge.addEventListener('keydown', (event) => {
                    if (event.key === 'Escape') {
                        hideJbiTooltip();
                        qualityBadge.blur();
                    }
                });
            }

            // Build Details row
            if (study.subexposure_inherited) {
                const sourceCell = tr.querySelector('td:nth-child(4)');
                if (sourceCell) {
                    const sourceNote = document.createElement('div');
                    sourceNote.style.cssText = 'font-size:.7rem;opacity:.75;margin-top:.2rem';
                    sourceNote.textContent = 'From ' + (study['Direct Exposure'] || study.subexposure_source);
                    sourceCell.appendChild(sourceNote);
                }
            }
            const detailsTr = document.createElement('tr');
            detailsTr.className = 'details-row hidden';
            detailsTr.id = `details-row-${index}`;
            if (study.verification_status === 'review_requested') detailsTr.style.backgroundColor = '#fff8e1';
            else if (!isChecked) detailsTr.style.backgroundColor = 'rgba(200, 200, 200, 0.15)';
            else detailsTr.style.backgroundColor = '#fafafa';

            // Get supporting text dictionary safely
            const policyMarkers = [
                study.exposure_measurement_overridden === true ? 'Dietary intake only: user-approved exposure classification (original: ' + (study.exposure_measurement_type_original || 'not recorded') + ')' : '',
                study.direction_imputed === true ? 'Direction imputed: ' + (study.direction_imputation_reason || 'higher versus lower / yes versus no') : '',
                study.effect_size_imputed === true ? 'Point estimate computed from CI assuming log symmetry' : '',
                study.effect_is_adjusted === true ? 'Adjusted ratio estimate' : ''
            ].filter(Boolean).join(' · ');
            const est = study.extraction_supporting_text || {
                "sample_size": "",
                "effect_size": "",
                "effect_direction": "",
                "p_value": "",
                "confidence_interval": "",
                "outcome_definition": "",
                "exposure_definition": ""
            };

            detailsTr.innerHTML = `
                <td colspan="14" style="padding: 1.25rem 2rem; border-bottom: 1px solid var(--border);">
                    <div class="snippets-container" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.25rem; border-left: 4px solid var(--primary); padding-left: 1.25rem; margin-left: 0.5rem;">
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">${uiText('Original Effect Reported by the Paper')}</span>
                            <span class="notranslate" translate="no" style="font-size: 0.88rem; color: #111; line-height: 1.45;">${originalEffect.type} ${formattedEffectValue(originalEffect.size)} (95% CI ${formattedEffectValue(originalEffect.lower)}–${formattedEffectValue(originalEffect.upper)})</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">${convertedEffectLabel}</span>
                            <span class="notranslate" translate="no" style="font-size: 0.88rem; color: #111; line-height: 1.45;">RR ${formattedEffectValue(convertedEffect.size)} (95% CI ${formattedEffectValue(convertedEffect.lower)}–${formattedEffectValue(convertedEffect.upper)})</span>
                            <span style="font-size: 0.76rem; color: #555;">${uiText('Selected finite-interval reference risk used: {risk}%', { risk: convertedBaselinePercent })}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">${uiText('Population Characteristics')}</span>
                            <span style="font-size: 0.88rem; color: #111; line-height: 1.45;"><strong>${uiText('Gender')}:</strong> <span class="notranslate" translate="no">${studyGender}</span></span>
                            <span style="font-size: 0.88rem; color: #111; line-height: 1.45;"><strong>${uiText('Smoking Status')}:</strong> <span class="notranslate" translate="no">${studySmokingStatus}</span></span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">Exposure Measurement Explanation</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${policyMarkers ? '<strong class="poolability-assumptions">' + policyMarkers.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</strong><br>' : ''}${study.exposure_measurement_supporting_text ? `<span class="notranslate" translate="no">"${study.exposure_measurement_supporting_text}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">Sample Size Quote</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${est.sample_size ? `<span class="notranslate" translate="no">"${est.sample_size}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">Effect Size Quote</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${est.effect_size ? `<span class="notranslate" translate="no">"${est.effect_size}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">Effect Direction Quote</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${est.effect_direction ? `<span class="notranslate" translate="no">"${est.effect_direction}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">P-Value Quote</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${est.p_value ? `<span class="notranslate" translate="no">"${est.p_value}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">Confidence Interval Quote</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${est.confidence_interval ? `<span class="notranslate" translate="no">"${est.confidence_interval}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">Outcome Definition Quote</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${est.outcome_definition ? `<span class="notranslate" translate="no">"${est.outcome_definition}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                        <div class="snippet-item" style="display: flex; flex-direction: column; gap: 0.25rem;">
                            <span style="font-weight: 700; color: #555; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;">Exposure Definition Quote</span>
                            <span style="font-size: 0.88rem; font-style: italic; color: #111; line-height: 1.45;">${est.exposure_definition ? `<span class="notranslate" translate="no">"${est.exposure_definition}"</span>` : '<i>Not available</i>'}</span>
                        </div>
                    </div>
                </td>
            `;

            elements.studiesTbody.appendChild(tr);
            const cohortDetails = document.createElement('div');
            cohortDetails.className = 'snippet-item notranslate';
            cohortDetails.setAttribute('translate', 'no');
            const cohortTitle = document.createElement('strong');
            cohortTitle.textContent = 'Cohort / study name';
            const cohortText = document.createElement('p');
            cohortText.textContent = `${study['Cohort Name'] || 'Not reported'} · Publication: ${study['Cohort Publication Date'] || study.Year || 'Not recorded'}${study.cohort_selection_reason ? ' · ' + study.cohort_selection_reason : ''}`;
            cohortDetails.append(cohortTitle, cohortText);
            detailsTr.querySelector('.snippets-container')?.prepend(cohortDetails);
            elements.studiesTbody.appendChild(detailsTr);
        });
        updateSortIcons();
    }

    // Toggle details row visibility
    window.toggleStudyDetails = (index, btn) => {
        const detailsRow = document.getElementById(`details-row-${index}`);
        if (!detailsRow) return;

        if (detailsRow.classList.contains('hidden')) {
            detailsRow.classList.remove('hidden');
            btn.textContent = '▼';
            btn.style.color = 'var(--primary-hover)';
        } else {
            detailsRow.classList.add('hidden');
            btn.textContent = '▶';
            btn.style.color = 'var(--primary)';
        }
    };

    // Verify Study (attached to window for inline onclick)
    window.verifyStudy = async (pmid, btn) => {
        if (!pmid) return;
        const study = currentStudies.find(s => String(s.PMID) === String(pmid));
        const disease = selectedDiseaseValue();
        const exposure = selectedExposureValue();
        const outcome = elements.outcome.value;

        btn.disabled = true;
        try {
            const res = await fetch('/verify', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pmid, study_data: study, disease, exposure, outcome })
            });
            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.error || `Server returned ${res.status}`);
            }
            if (res.ok) {
                btn.textContent = `✓ ${data.count}`;
                if (study) study.verifications = data.count;
                if (data.review_requested) {
                    if (study) study.verification_status = 'review_requested';
                    btn.style.background = '#f57c00';
                    if (data.notification_sent) {
                        alert(uiText('Two matching submissions were received. Developers have been emailed for review; results remain unchanged.'));
                    } else if (data.notification_pending) {
                        alert(uiText('Two matching submissions were saved and the review email is pending; results remain unchanged.'));
                    } else if (!data.notification_already_sent) {
                        alert(uiText('Developer review was requested, but the notification email could not be sent. Results remain unchanged.'));
                    }
                }
            }
        } catch (e) {
            console.error("Verify error:", e);
            alert(uiText('The verification was not saved: {message}', { message: e.message }));
        } finally {
            btn.disabled = false;
        }
    };

    window.excludeStudy = async (pmid, btn) => {
        if (!pmid) return;
        if (!confirm(uiText('Flag this study for developer review? Two flags will email developers.'))) return;

        btn.disabled = true;
        try {
            const disease = selectedDiseaseValue();
            const exposure = selectedExposureValue();
            const outcome = elements.outcome.value;

            const res = await fetch('/exclude', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pmid, study_data: currentStudies.find(s => String(s.PMID) === String(pmid)), disease, exposure, outcome })
            });
            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.error || `Server returned ${res.status}`);
            }
            if (res.ok) {
                const study = currentStudies.find(s => String(s.PMID) === String(pmid));
                if (study) study.exclusion_flags = data.exclusions;
                btn.textContent = `⚑ ${data.exclusions}`;
                btn.style.background = '#f57c00';
                if (data.already_flagged) {
                    alert(uiText('Your flag was already recorded for this article.'));
                }
                if (data.review_requested) {
                    if (study) study.verification_status = 'review_requested';
                    if (data.notification_sent) {
                        alert(uiText('The review threshold was reached. Developers have been emailed; results remain unchanged.'));
                    } else if (data.notification_already_sent) {
                        alert(uiText('Developers were already notified about these flags. Results remain unchanged.'));
                    } else if (data.notification_pending) {
                        alert(uiText('The review request was saved and its email notification is pending. Results remain unchanged.'));
                    } else {
                        alert(uiText('Developer review was requested, but the notification email could not be sent. Results remain unchanged.'));
                    }
                }
            }
        } catch (e) {
            console.error("Exclude error:", e);
            alert(uiText('The flag was not saved: {message}', { message: e.message }));
        } finally {
            btn.disabled = false;
        }
    };

    // Sorting
    function sortCurrentStudies() {
        const field = currentSort.field;
        const qualityOrder = { Good: 0, Moderate: 1, Fair: 2, Poor: 3 };

        currentStudies.sort((a, b) => {
            let valA = a[field], valB = b[field];
            if (field === 'Effect Size') {
                valA = Number(studyOriginalEffect(a).size);
                valB = Number(studyOriginalEffect(b).size);
            }
            if (field === 'Sample Size') {
                valA = parseInt(String(a.Participants || a['Sample Size'] || '').replace(/,/g, ''), 10);
                valB = parseInt(String(b.Participants || b['Sample Size'] || '').replace(/,/g, ''), 10);
                if (Number.isNaN(valA)) valA = currentSort.direction === 'asc' ? Infinity : -Infinity;
                if (Number.isNaN(valB)) valB = currentSort.direction === 'asc' ? Infinity : -Infinity;
            }
            if (field === 'Quality Score') {
                valA = qualityOrder[a[field]] ?? qualityOrder.Fair;
                valB = qualityOrder[b[field]] ?? qualityOrder.Fair;
            }
            if (valA < valB) return currentSort.direction === 'asc' ? -1 : 1;
            if (valA > valB) return currentSort.direction === 'asc' ? 1 : -1;
            return 0;
        });
    }

    window.handleSort = (field) => {
        if (currentSort.field === field) currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
        else { currentSort.field = field; currentSort.direction = 'asc'; }

        sortCurrentStudies();
        renderStudiesTable();
    };

    function updateSortIcons() {
        document.querySelectorAll('th.sortable').forEach(th => th.classList.remove('sort-asc', 'sort-desc'));
        const map = { 'Study': 'th-study', 'Effect Size': 'th-es', 'Sample Size': 'th-n-cases', 'Quality Score': 'th-quality', 'Year': 'th-year', 'Journal': 'th-journal' };
        const id = map[currentSort.field];
        if (id) {
            const el = document.getElementById(id);
            if (el) el.classList.add(currentSort.direction === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    }

    function updateLastUpdated(ts = null) {
        if (!elements.lastUpdated) return;
        let date;
        if (ts) {
            if (typeof ts === 'number' || !isNaN(Number(ts))) {
                // It's a Unix timestamp (seconds since epoch)
                date = new Date(Number(ts) * 1000);
            } else if (typeof ts === 'string') {
                // It's a string timestamp (e.g. YYYY-MM-DD HH:MM:SS)
                // Split on non-digit characters to construct a local Date object
                const parts = ts.split(/[- :T]/);
                if (parts.length >= 6) {
                    date = new Date(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5]);
                } else {
                    const isoStr = ts.replace(' ', 'T');
                    date = new Date(isoStr);
                    if (isNaN(date.getTime())) {
                        date = new Date(ts);
                    }
                }
            }
        } else {
            return;
        }
        elements.lastUpdated.textContent = isNaN(date.getTime()) ? ts : date.toLocaleString();
    }
    function clearPreviousAnalysis(data) {
        lastHeadlineData = null;
        lastHeadlineStudyCount = null;
        for (const name of ['headlineResult', 'heterogeneityStats', 'influenceSection', 'pooledPowerAnalysis']) {
            if (elements[name]) elements[name].classList.add('hidden');
        }
        for (const name of ['pooledEs', 'pooledCi', 'valI2', 'valTau2', 'valEggers']) {
            if (elements[name]) elements[name].textContent = 'NA';
        }
        if (elements.looTbody) elements.looTbody.innerHTML = '';
        for (const [name, field] of [['forestPlot','plot_url'], ['funnelPlot','funnel_plot_url'], ['baujatPlot','baujat_plot_url']]) {
            if (elements[name] && !data[field]) elements[name].removeAttribute('src');
            if (elements[name]) elements[name].classList.toggle('hidden', !data[field]);
        }
        const notice = document.getElementById('pooling-status-notice');
        if (notice) notice.classList.toggle('hidden', Boolean(data.headline));
    }

    function updateResultsUI(data, analysisContext = null) {
        mergeCohortSelection(data);
        if (currentStudies.length) {
            window.CaseCounts.rememberSelection(currentStudies);
            renderStudiesTable();
            window.CaseCounts.restoreSelection(currentStudies);
        }
        data = window.AnalysisSettings.receive(data);
        clearPreviousAnalysis(data);
        if (analysisContext) {
            lastAnalysisContext = {
                disease: analysisContext.disease,
                exposure: analysisContext.exposure
            };
        }
        if (elements.forestPlot && data.plot_url) elements.forestPlot.src = window.AnalysisSettings.plotUrl(data.plot_url);
        if (elements.funnelPlot && data.funnel_plot_url) elements.funnelPlot.src = window.AnalysisSettings.plotUrl(data.funnel_plot_url);
        if (elements.interactiveAnalysisNote) {
            elements.interactiveAnalysisNote.classList.toggle(
                'hidden', !data.interactive_reanalysis
            );
        }

        if (data.headline && elements.headlineResult) {
            elements.headlineResult.classList.remove('hidden');
            elements.pooledEs.textContent = data.headline.pooled_es;
            elements.pooledCi.textContent = `${data.headline.ci_low}, ${data.headline.ci_upp}`;

            // Since the pooled result is now explicitly calculated as an RR in the backend,
            // we no longer need to show the estimated OR/HR conversions for the headline.
            const rrContainer = document.getElementById('estimated-rr-container');
            if (rrContainer) {
                rrContainer.style.display = 'none';
            }

            // Save original data for measure transformation toggle
            lastHeadlineData = JSON.parse(JSON.stringify(data.headline));
            const interpretationCount = String(data.headline.results_interpretation || '').match(
                /^The pooled analysis of (\d+) studies/i
            );
            lastHeadlineStudyCount = Number(data.headline.n_studies)
                || (interpretationCount ? Number(interpretationCount[1]) : null)
                || (Array.isArray(data.studies) ? data.studies.length : null);
            if (elements.displayMeasure) {
                // Reset to RR whenever new data is loaded
                elements.displayMeasure.value = window.AnalysisSettings.get().effect_measure;
            }
            applyResultMeasureTransformation();

            // Heterogeneity & Egger's
            if (elements.heterogeneityStats) {
                elements.heterogeneityStats.classList.remove('hidden');
                elements.valI2.textContent = data.headline.i2 !== null ? data.headline.i2 : '-';
                elements.valTau2.textContent = data.headline.tau2 !== null ? data.headline.tau2 : '-';
                elements.valEggers.textContent = (data.headline.eggers_p !== null && data.headline.eggers_p !== undefined) ? data.headline.eggers_p.toFixed(4) : '-';
            }

            // Pooled Power Analysis Update
            updatePooledPowerAnalysis();
            if (elements.pooledIncidence && !elements.pooledIncidence.hasListener) {
                elements.pooledIncidence.addEventListener('input', updatePooledPowerAnalysis);
                elements.pooledIncidence.hasListener = true;
            }
            if (elements.powerAlpha && !elements.powerAlpha.hasListener) {
                elements.powerAlpha.addEventListener('input', updatePooledPowerAnalysis);
                elements.powerAlpha.hasListener = true;
            }
            if (elements.powerValue && !elements.powerValue.hasListener) {
                elements.powerValue.addEventListener('input', updatePooledPowerAnalysis);
                elements.powerValue.hasListener = true;
            }
            if (elements.powerSides && !elements.powerSides.hasListener) {
                elements.powerSides.addEventListener('change', updatePooledPowerAnalysis);
                elements.powerSides.hasListener = true;
            }
            if (elements.powerEffect && !elements.powerEffect.hasListener) {
                elements.powerEffect.addEventListener('change', updatePooledPowerAnalysis);
                elements.powerEffect.hasListener = true;
            }
            if (elements.powerArms && !elements.powerArms.hasListener) {
                elements.powerArms.addEventListener('change', updatePooledPowerAnalysis);
                elements.powerArms.hasListener = true;
            }

            // Influence & Sensitivity
            if (elements.influenceSection) {
                if (data.baujat_plot_url || (data.headline.loo_results && data.headline.loo_results.length > 0)) {
                    elements.influenceSection.classList.remove('hidden');
                    if (elements.baujatPlot && data.baujat_plot_url) {
                        elements.baujatPlot.src = window.AnalysisSettings.plotUrl(data.baujat_plot_url);
                    }
                    if (elements.looTbody) {
                        elements.looTbody.innerHTML = '';
                        (data.headline.loo_results || []).forEach(res => {
                            const tr = document.createElement('tr');
                            tr.innerHTML = `
                                <td class="notranslate" translate="no">${res.omitted}</td>
                                <td data-loo-significant="${Boolean(res.is_significant)}" style="color: ${res.is_significant ? '#b91c1c' : '#666'}">
                                    ${(res.pooled_es !== undefined && res.pooled_es !== null) ? res.pooled_es.toFixed(2) : '-'} (${(res.ci_low !== undefined && res.ci_low !== null) ? res.ci_low.toFixed(2) : '-'}, ${(res.ci_upp !== undefined && res.ci_upp !== null) ? res.ci_upp.toFixed(2) : '-'})
                                </td>
                            `;
                            elements.looTbody.appendChild(tr);
                        });
                    }
                } else {
                    elements.influenceSection.classList.add('hidden');
                }
            }
        }
        updateLastUpdated(data.last_run);


        // Update Table Head
        const thCases = document.getElementById('th-n-cases');
        if (thCases && elements.outcome) {
            const term = 'Cases';
            thCases.innerHTML = `N<span class="sort-icon"></span> / ${term}`;
        }
    }

    // Measure Transformation Logic
    if (elements.displayMeasure) {
        elements.displayMeasure.addEventListener('change', applyResultMeasureTransformation);
    }

    function applyResultMeasureTransformation() {
        if (!lastHeadlineData || !elements.pooledEs || !elements.pooledCi) return;
        const measure = window.AnalysisSettings.get().effect_measure;
        const convert = value => window.AnalysisSettings.convert(value, 'RR', measure);
        const es = convert(lastHeadlineData.pooled_es), low = convert(lastHeadlineData.ci_low), upp = convert(lastHeadlineData.ci_upp);
        const pi_low = convert(lastHeadlineData.pi_low), pi_upp = convert(lastHeadlineData.pi_upp);
        const fmt = value => Number.isFinite(value) ? value.toFixed(2) : 'N/A';
        const label = {RR:'Pooled Relative Risk (RR)',HR:'Pooled Hazard Ratio (HR)',OR:'Pooled Odds Ratio (OR)'}[measure];
        if (elements.pooledLabel) elements.pooledLabel.textContent = uiText(label);
        elements.pooledEs.textContent = fmt(es);
        elements.pooledCi.textContent = `${fmt(low)}, ${fmt(upp)}`;
        if (elements.piContainer && elements.pooledPi) {
            elements.pooledPi.textContent = `${fmt(pi_low)}, ${fmt(pi_upp)}`;
            elements.piContainer.style.display = Number.isFinite(pi_low) && Number.isFinite(pi_upp) ? 'inline' : 'none';
        }
        if (typeof renderHeadlineInterpretation === 'function') renderHeadlineInterpretation(measure,es,low,upp);
        else if (elements.interpretation) elements.interpretation.textContent = `${measure}: ${fmt(es)} (95% CI ${fmt(low)}–${fmt(upp)}).`;
        if (typeof renderResultsInterpretation === 'function') renderResultsInterpretation(measure,es,low,upp);
        else if (elements.resultsInterpretation) elements.resultsInterpretation.textContent = `Pooled ${measure} ${fmt(es)} (95% CI ${fmt(low)}–${fmt(upp)}), using the reference risk and conversion assumptions in Settings.`;
        if (typeof renderFunnelInterpretation === 'function') renderFunnelInterpretation();
        updatePooledPowerAnalysis();
    }

    function updatePooledPowerAnalysis() {
        window.AnalysisSettings.power(elements, lastHeadlineData);
    }

    // --- Tab Switching Logic (Removed as only one tab remains) ---

    // --- Power Calculator Logic (Helper for integrated results calculator) ---
    function getZ(p) {
        if (p < 0.5) return -getZ(1 - p);
        const t = Math.sqrt(-2.0 * Math.log(1.0 - p));
        const c0 = 2.515517, c1 = 0.802853, c2 = 0.010328;
        const d1 = 1.432788, d2 = 0.189269, d3 = 0.001308;
        return t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t);
    }

    window.addEventListener('metapee:languagechange', () => {
        if (elements.updateBtn) {
            elements.updateBtn.textContent = uiText(elements.updateBtn.disabled ? 'Updating...' : 'Update Analysis');
        }
        if (currentStudies.length) renderStudiesTable();
        if (lastHeadlineData) applyResultMeasureTransformation();
    });
});
