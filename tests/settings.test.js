/* Run: node /private/tmp/metauro-settings-check.js /path/to/static/analysis-settings.js
   No browser or npm dependencies. Synthetic fixtures only. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const sourceCode = fs.readFileSync(process.argv[2], 'utf8');
const storage = new Map();
const catalogFor = source => {
    const prostate = source === 'metavir';
    const ids = prostate ? ['prostate_cancer', 'aggressive_prostate_cancer'] : ['bladder_cancer', 'renal_cell_carcinoma'];
    const labels = prostate ? ['Prostate cancer', 'Aggressive prostate cancer'] : ['Bladder cancer', 'Renal cell carcinoma'];
    const sexes = (prostate ? ['male'] : ['both', 'male', 'female']).map(id => ({id,label:id==='both'?'Both Sexes':id==='male'?'Male':'Female'}));
    return {repository: prostate ? 'MetaVir' : 'metaPee',
        defaults:{age:60,years:10,sex:prostate?'male':'both',race:'all_races_ethnicities'},
        source:{label:'Synthetic baseline',submission:'fixture',data_period:'fixture',release_date:'fixture',url:'https://example.org'},
        ages:[30,40,50,60,70,80],years:[5,10,15,20,25,30,35,40,45,50],sexes,
        races:[{id:'all_races_ethnicities',label:'All Races / Ethnicities'},{id:'non_hispanic_black',label:'Non-Hispanic Black'}],
        diseases: ids.map((id,index)=>({id,label:labels[index],...(index?{manual_required:true,unsupported_reason:'No exact subtype baseline'}:{})})),
        records: sexes.flatMap(({id:sex})=>['all_races_ethnicities','non_hispanic_black'].map(race=>({
            disease:ids[0],sex,race,age:60,years:10,risk_percent:prostate?5:race==='non_hispanic_black'?3:2,
            source:'synthetic'})))};
};
function element(id, extra={}) {
    const handlers={}, classes=new Set(), attrs={};
    return {id,dataset:{},value:'',textContent:'',children:[],options:[],handlers,
        classList:{toggle(name,on){on?classes.add(name):classes.delete(name);},contains:name=>classes.has(name),remove:name=>classes.delete(name)},
        addEventListener(type,fn){(handlers[type] ||= []).push(fn);},
        dispatch(type){for(const fn of handlers[type] || [])fn({target:this,preventDefault(){}});},
        replaceChildren(...children){this.children=children;this.options=children;},append(...children){this.children.push(...children);},
        setAttribute(name,value){attrs[name]=value;},getAttribute:name=>attrs[name],removeAttribute(name){delete attrs[name];},
        showModal(){this.open=true;},close(){this.open=false;},...extra};
}
async function runtime({source='metapee',selector='disease',scope=0,blockedStorage=false,page='analysis',search=''}={}) {
    const catalog=catalogFor(source), prefix=`/evidence/${source}`, fetches=[],events=[],redirects=[];
    const option=index=>({value:selector==='summary-scope'?catalog.diseases[index].id:catalog.diseases[index].label,
        textContent:catalog.diseases[index].label,dataset:selector==='summary-scope'?{}:{
            disease:index&&source==='metavir'?catalog.diseases[0].label:catalog.diseases[index].label,
            ...(index&&source==='metavir'?{subcategory:catalog.diseases[index].id}:{})}});
    const nodes={};
    nodes[selector]=element(selector,{value:option(scope).value,selectedOptions:[option(scope)]});
    const ids=['analysis-settings-dialog','open-analysis-settings','close-analysis-settings',
        'settings-baseline-source','settings-age','settings-years','settings-sex','settings-race','settings-manual-risk',
        'settings-effect-measure','settings-forest-scale','settings-study-design','settings-exposure-rate','power-controls-per-case',
        'settings-manual-field','settings-risk-value','settings-risk-context','settings-disease-name','settings-disease-definition','settings-include-preprints','settings-keep-latest-cohort','evidence-keep-latest-cohort',
        'settings-baseline-note','settings-exposure-rate-field','pooled-incidence'];
    for(const id of ids)nodes[id]=element(id);
    nodes['analysis-settings-dialog'].dataset.repository='metaUro';
    const window={MetaUro:{source,prefix,page},dispatchEvent:event=>events.push(event),addEventListener(){}};
    const sandbox={window,URL,URLSearchParams,document:{readyState:'complete',getElementById:id=>nodes[id]||null,
        createElement:tag=>element(tag),addEventListener(){}},
        location:{pathname:`${prefix}/`,search,href:`http://localhost${prefix}/${search}`,assign(url){redirects.push(url);},replace(url){redirects.push(url);}},
        localStorage:{getItem:key=>{if(blockedStorage)throw Error('storage blocked');return storage.get(key)||null;},
            setItem:(key,value)=>{if(blockedStorage)throw Error('storage blocked');storage.set(key,value);},removeItem:key=>storage.delete(key)},
        fetch:async url=>{fetches.push(url);return {ok:true,json:async()=>url.includes('seer-baseline')?catalog:{}};},
        setTimeout:fn=>{fn();return 1;},clearTimeout(){},
        Event:class{constructor(type){this.type=type;}},CustomEvent:class{constructor(type,args){this.type=type;this.detail=args?.detail;}}};
    vm.createContext(sandbox);vm.runInContext(sourceCode,sandbox);
    assert.ok(window.AnalysisSettings,'AnalysisSettings is exported');
    await Promise.race([window.AnalysisSettings.ready,new Promise((_,reject)=>setTimeout(()=>reject(Error('Settings.ready did not settle')),250))]);
    assert.deepEqual(fetches,[`${prefix}/static/seer-baseline.json`,`${prefix}/static/settings-exposure-hierarchy.json`],'catalogs use active source prefix');
    return {settings:window.AnalysisSettings,nodes,events,catalog,redirects,select(index){
        const next=option(index);Object.assign(nodes[selector],{value:next.value,selectedOptions:[next]});nodes[selector].dispatch('change');
    }};
}
const cases=[];
function test(name,fn){cases.push({name,fn});}
function manual(app,percent){app.settings.set('manual_risk_percent',percent);app.settings.set('baseline_source','manual');}
const clone=value=>JSON.parse(JSON.stringify(value));
test('independent sources, endpoints, population selections, and reloads',async()=>{
    storage.clear();
    const urinary=await runtime();assert.equal(urinary.settings.p0(),0.02);
    urinary.settings.set('sex','female');manual(urinary,8);urinary.select(1);
    assert.equal(urinary.settings.get().manual_risk_percent,null);assert.ok(Number.isNaN(urinary.settings.p0()));
    manual(urinary,0.8);urinary.select(0);assert.equal(urinary.settings.p0(),0.08);
    const prostate=await runtime({source:'metavir'});
    assert.equal(prostate.settings.get().sex,'male');assert.equal(prostate.settings.p0(),0.05);
    manual(prostate,12);prostate.select(1);
    assert.ok(Number.isNaN(prostate.settings.p0()));assert.equal(prostate.settings.get().manual_risk_percent,null);
    prostate.settings.set('manual_risk_percent',0.5);assert.equal(prostate.settings.p0(),0.005);
    assert.equal(prostate.nodes['settings-manual-field'].classList.contains('settings-hidden'),false);
    const reloadUrinary=await runtime();assert.equal(reloadUrinary.settings.p0(),0.08);assert.equal(reloadUrinary.settings.get().sex,'female');
    reloadUrinary.select(1);assert.equal(reloadUrinary.settings.p0(),0.008);
    const reloadProstate=await runtime({source:'metavir',scope:1});assert.equal(reloadProstate.settings.p0(),0.005);
    reloadProstate.select(0);assert.equal(reloadProstate.settings.p0(),0.12);
    assert.ok(storage.has('metaUro:metapee')&&storage.has('metaUro:metavir'),'source-specific metaUro storage keys');
});
for(const selector of ['summary-scope','summary-disease'])test(`${selector} resolves and changes endpoint`,async()=>{
    storage.clear();const app=await runtime({selector});assert.equal(app.settings.p0(),0.02);
    manual(app,7);app.select(1);assert.ok(Number.isNaN(app.settings.p0()));
    assert.equal(app.nodes['settings-disease-name'].textContent,'Renal cell carcinoma');
    manual(app,0.7);app.select(0);assert.equal(app.settings.p0(),0.07);
});
test('population preview, conversion, and persistence',async()=>{
    storage.clear();const app=await runtime();const before=app.settings.convert(2,'OR','RR');
    app.settings.set('race','non_hispanic_black');assert.equal(app.settings.p0(),0.03);
    assert.notEqual(app.settings.convert(2,'OR','RR'),before);
    assert.match(app.nodes['settings-risk-context'].textContent,/Both Sexes/);
    assert.match(app.nodes['settings-risk-context'].textContent,/Non-Hispanic Black/);
    assert.equal((await runtime()).settings.get().race,'non_hispanic_black');
});
for(const raw of ['{broken','null','[]','42','"garbage"',JSON.stringify({age:'oops',years:900,sex:'other',race:'unknown',effect_measure:'BOGUS',forest_scale:'bad',study_design:'bad',exposure_rate:4,controls_per_case:-1,scope_baselines:'bad'})])test(`corrupt storage is normalized: ${raw.slice(0,30)}`,async()=>{
    storage.clear();storage.set('metaUro:metapee',raw);const app=await runtime();const state=clone(app.settings.get());
    assert.ok(app.catalog.ages.includes(state.age));assert.ok(app.catalog.years.includes(state.years));assert.ok(state.age+state.years<=90);
    assert.ok(app.catalog.sexes.some(row=>row.id===state.sex));assert.ok(app.catalog.races.some(row=>row.id===state.race));
    assert.ok(['RR','OR','HR'].includes(state.effect_measure));assert.ok(['both','log','original'].includes(state.forest_scale));
    assert.ok(['prospective','retrospective'].includes(state.study_design));assert.ok(state.exposure_rate>0&&state.exposure_rate<1);
    assert.ok(Number.isFinite(state.controls_per_case)&&state.controls_per_case>0);assert.ok(app.settings.baseline().available);
});
test('invalid per-endpoint manual storage cannot poison a fresh analysis',async()=>{
    storage.clear();storage.set('metaUro:metapee',JSON.stringify({scope_baselines:{bladder_cancer:{baseline_source:'manual',manual_risk_percent:'not-a-number'}}}));
    const app=await runtime();assert.equal(app.settings.get().baseline_source,'seer');assert.equal(app.settings.p0(),0.02);
});
test('unavailable localStorage still permits edits and baseline preview',async()=>{
    storage.clear();const app=await runtime({blockedStorage:true});manual(app,6);assert.equal(app.settings.p0(),0.06);
});
test('inline cohort choice shares settings, persistence, reset, and change events',async()=>{
    storage.clear();const app=await runtime();
    assert.equal(app.nodes['evidence-keep-latest-cohort'].value,'true');
    app.nodes['evidence-keep-latest-cohort'].value='true';app.nodes['evidence-keep-latest-cohort'].dispatch('change');
    assert.equal(app.settings.get().keep_latest_cohort,true);
    assert.equal(app.nodes['settings-keep-latest-cohort'].value,'true');
    assert.ok(app.events.some(event=>event.type==='analysissettingschange'&&event.detail.keep_latest_cohort===true));
    const reload=await runtime();assert.equal(reload.nodes['evidence-keep-latest-cohort'].value,'true');
    reload.nodes['settings-keep-latest-cohort'].value='false';reload.nodes['settings-keep-latest-cohort'].dispatch('change');
    assert.equal(reload.nodes['evidence-keep-latest-cohort'].value,'false');
    reload.settings.set('keep_latest_cohort',true);assert.equal(reload.nodes['evidence-keep-latest-cohort'].value,'true');
    reload.settings.reset();assert.equal(reload.nodes['evidence-keep-latest-cohort'].value,'true');
});
test('preprint source choice defaults off and survives reload, summary navigation and reset',async()=>{
    storage.clear();const app=await runtime();assert.equal(app.settings.get().include_preprints,false);
    app.nodes['settings-include-preprints'].value='true';app.nodes['settings-include-preprints'].dispatch('change');
    assert.equal(app.settings.get().include_preprints,true);
    assert.ok(app.events.some(event=>event.type==='analysissettingschange'&&event.detail.include_preprints===true));
    const summary=await runtime({page:'summary',search:'?scope=bladder_cancer&lang=en'});
    assert.equal(new URL(summary.redirects[0]).searchParams.get('include_preprints'),'true');
    assert.equal(new URL(summary.redirects[0]).searchParams.get('scope'),'bladder_cancer');
    const explicitOff=await runtime({page:'summary',search:'?include_preprints=false'});
    assert.equal(explicitOff.settings.get().include_preprints,false);assert.equal(explicitOff.redirects.length,0);
    explicitOff.settings.set('include_preprints',true);assert.equal(new URL(explicitOff.redirects.at(-1)).searchParams.get('include_preprints'),'true');
    assert.equal((await runtime({source:'metavir'})).settings.get().include_preprints,false);
    app.settings.reset();assert.equal(app.settings.get().include_preprints,false);
});
(async()=>{
    let failed=0;for(const {name,fn} of cases){try{await fn();console.log(`PASS ${name}`);}catch(error){failed++;console.error(`FAIL ${name}: ${error.stack}`);}}
    console.log(`${cases.length-failed}/${cases.length} settings checks passed`);process.exitCode=failed?1:0;
})().catch(error=>{console.error(error);process.exitCode=1;});
