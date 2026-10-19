const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const files = process.argv.slice(2);
if (!files.length) files.push('runtime_code/MetaVir/static/script.js', 'runtime_code/metaPee/static/script.js');
const rows = [
    {id: 'clinical', Design: 'Clinical Trials', Timing: 'Prospective'},
    {id: 'rct', Design: 'RCT', Timing: 'prospective'},
    {id: 'randomized', Design: 'Randomized, double-blind, placebo-controlled trial'},
    {id: 'randomised', Design: 'randomised controlled trial'},
    {id: 'intervention', Design: 'randomized comparative intervention study'},
    {id: 'prospective', Design: 'Prospective_cohort study', Timing: 'Retrospective'},
    {id: 'retrospective', Design: 'Retrospective cohort study', Timing: 'Prospective'},
    {id: 'nested', Design: 'Prospective nested case-control study within a randomized trial', Timing: 'Retrospective'},
    {id: 'cohort-timing', Design: 'cohort', Timing: 'prospective; 10-year surveillance'},
    {id: 'case-control-timing', Design: 'case_control', Timing: 'retrospective'},
    {id: 'case-control-unspecified', Design: 'case-control study'},
    {id: 'observational-trial', Design: 'Prospective cohort analysis of the PLCO randomized trial'},
    {id: 'nested-trial', Design: 'case-control study nested within a randomized controlled trial'},
    {id: 'cross-sectional-trial', Design: 'cross-sectional analysis within a clinical trial'},
    {id: 'emulation', Design: 'Retrospective target trial emulation using matched cohorts'},
    {id: 'emulation-timing', Design: 'Target trial-emulation', Timing: 'Retrospective'},
    {id: 'retrospective-trial-timing', Design: 'Clinical trial', Timing: 'Retrospective'},
    {id: 'unicode', Design: 'Prospective\u2013cohort study'},
    {id: 'biomarker-trial', Design: 'Prospective biomarker analysis nested within a randomized supplementation trial'},
    {id: 'trial-observational-followup', Design: 'randomized controlled trial with post-intervention observational follow-up'},
    {id: 'randomized-treatment', Design: 'Randomized prospective treatment study'},
    {id: 'nonrandomized', Design: 'Non-randomized community intervention study'},
    {id: 'missing'},
];
const expected = {
    All: rows.map(row => row.id),
    Prospective: ['prospective', 'nested', 'cohort-timing', 'observational-trial', 'unicode', 'biomarker-trial', 'randomized-treatment'],
    Retrospective: ['retrospective', 'case-control-timing', 'emulation', 'emulation-timing'],
    'Clinical Trials': ['clinical', 'rct', 'randomized', 'randomised', 'intervention', 'retrospective-trial-timing', 'trial-observational-followup', 'randomized-treatment'],
};

for (const relative of files) {
    const source = fs.readFileSync(path.join(root, relative), 'utf8');
    const start = source.indexOf('    function applyFilters() {');
    const end = source.indexOf('\n    }', start) + 6;
    assert(start >= 0 && end > start, `Missing applyFilters in ${relative}`);
    const context = {
        allStudies: rows, currentStudies: [],
        elements: {filterTiming: {value: 'All'}, filterQuality: {value: 'All'}},
        window: {MetaBreathSmoking: {matches: () => true}},
        selectedSmokingStatus: () => 'all',
        sortCurrentStudies() {}, renderStudiesTable() {}, setTimeout() {},
    };
    vm.createContext(context);
    vm.runInContext(source.slice(start, end), context);
    for (const [selection, ids] of Object.entries(expected)) {
        context.elements.filterTiming.value = selection;
        vm.runInContext('applyFilters();', context);
        assert.deepStrictEqual(Array.from(context.currentStudies, row => row.id), ids,
            `${relative}: ${selection} must use recorded Design and preserve observational analyses within trials`);
    }
    context.elements.filterTiming.value = 'Clinical Trial';
    vm.runInContext('applyFilters();', context);
    assert.deepStrictEqual(Array.from(context.currentStudies, row => row.id), expected['Clinical Trials']);
    if (relative === 'runtime_code/MetaVir/static/script.js') {
        const selenium = JSON.parse(fs.readFileSync(path.join(root,
            'sources/MetaVir/Cached_results/selenium/prostate_cancer_incidence_true_core.json'), 'utf8'));
        const pmids = ['19066370', '21896650', '22887343'];
        context.allStudies = selenium.studies.filter(row => pmids.includes(String(row.PMID)));
        assert.strictEqual(context.allStudies.length, 3, 'All three original selenium records are present');
        context.elements.filterTiming.value = 'Clinical Trials';
        vm.runInContext('applyFilters();', context);
        assert.deepStrictEqual(Array.from(context.currentStudies, row => String(row.PMID)).sort(), pmids.sort(),
            'The original saved selenium evidence must pass without rewriting its Design metadata');
    }
    console.log(`Study design filter regression passed: ${relative}`);
}
