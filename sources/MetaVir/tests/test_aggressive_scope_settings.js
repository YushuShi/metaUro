/* A manual risk belongs to one disease endpoint, including after reload. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../static/settings.js'), 'utf8');
const catalog = {
    repository: 'MetaVir', defaults: {sex:'male', race:'all_races_ethnicities'},
    ages: [60], years: [10], sexes: [{id:'male'}], races: [{id:'all_races_ethnicities'}],
    diseases: [{id:'prostate_cancer',label:'Prostate cancer'},
               {id:'aggressive_prostate_cancer',label:'Aggressive prostate cancer',manual_required:true,unsupported_reason:'Enter aggressive incidence'}],
    records: [{disease:'prostate_cancer',sex:'male',race:'all_races_ethnicities',age:60,years:10,risk_percent:5}]
};
const options = {
    general: {value:'Prostate cancer',textContent:'Prostate cancer',dataset:{disease:'Prostate cancer'}},
    aggressive: {value:'Aggressive prostate cancer',textContent:'    Aggressive prostate cancer',dataset:{disease:'Prostate cancer',subcategory:'aggressive_prostate_cancer'}}
};
const saved = new Map();
async function runtime(initial='general') {
    const listeners = {};
    const nodes = {
        disease: {dataset:{},value:options[initial].value,selectedOptions:[options[initial]],addEventListener(type,fn){listeners[type]=fn;}},
        'settings-manual-field': {classList:{toggle(name,hidden){this.hidden=hidden;}}}
    };
    const window = {dispatchEvent(){}};
    const sandbox = {window, document:{readyState:'complete',getElementById:id=>nodes[id] || null},
        location:{pathname:'/'},localStorage:{getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)},
        fetch:async url=>({ok:true,json:async()=>url.includes('seer-baseline')?catalog:{}}),
        Event:class{},CustomEvent:class{},setTimeout(){},clearTimeout(){}};
    vm.createContext(sandbox);vm.runInContext(source,sandbox);
    await window.AnalysisSettings.ready;
    return {settings:window.AnalysisSettings, nodes, select(key){
        nodes.disease.value=options[key].value;nodes.disease.selectedOptions=[options[key]];listeners.change();
    }};
}
(async()=>{
    const first=await runtime();
    assert.equal(first.settings.p0(),0.05);
    first.settings.set('manual_risk_percent',20);first.settings.set('baseline_source','manual');
    assert.equal(first.settings.p0(),0.2);
    first.select('aggressive');
    assert.ok(Number.isNaN(first.settings.p0()));
    assert.equal(first.settings.get().manual_risk_percent,null);
    assert.equal(first.nodes['settings-manual-field'].classList.hidden,false);
    first.settings.set('manual_risk_percent',0.5);
    assert.equal(first.settings.p0(),0.005);
    first.select('general');
    assert.equal(first.settings.p0(),0.2);
    first.select('aggressive');
    assert.equal(first.settings.p0(),0.005);
    const reloaded=await runtime('aggressive');
    assert.equal(reloaded.settings.p0(),0.005);
    reloaded.select('general');assert.equal(reloaded.settings.p0(),0.2);
    console.log('Aggressive scope baseline isolation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
