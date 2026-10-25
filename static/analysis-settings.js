/* Shared analysis settings: published finite-interval baselines and explicit provenance. */
(() => {
    'use strict';
    if (window.AnalysisSettings) return;
    const $ = id => document.getElementById(id);
    const diseaseInput = () => $('disease') || $('summary-disease') || $('summary-scope');
    const defaults = {baseline_source:'seer',age:60,years:10,sex:'both',race:'all_races_ethnicities',manual_risk_percent:null,effect_measure:'RR',forest_scale:'both',study_design:'prospective',exposure_rate:0.5,controls_per_case:1,keep_latest_cohort:false};
    let state = {...defaults}, catalog = null, loadError = '', lastResponse = null, changeTimer;
    let activeDiseaseScope = '', scopeBaselines = {};
    let readyResolve;
    const ready = new Promise(resolve => {readyResolve = resolve;});
    const normalize = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    const number = value => value === null || value === undefined || String(value).trim() === '' ? NaN : Number(value);
    const fields = {baseline_source:'settings-baseline-source',age:'settings-age',years:'settings-years',sex:'settings-sex',race:'settings-race',manual_risk_percent:'settings-manual-risk',effect_measure:'settings-effect-measure',forest_scale:'settings-forest-scale',study_design:'settings-study-design',exposure_rate:'settings-exposure-rate',controls_per_case:'power-controls-per-case',keep_latest_cohort:'settings-keep-latest-cohort'};

    function disease() {
        if (!catalog) return null;
        const input = diseaseInput(), option = input?.selectedOptions?.[0];
        // A subtype is its own endpoint. Never fall back to its parent baseline.
        const subtype = option?.dataset.subcategory;
        if (subtype && catalog.repository === 'metaFemina') {
            const parent=normalize(option?.dataset.disease || option?.dataset.scopeKey);
            const prefix=parent.startsWith('breast') ? 'breast' : /^(uterus|uterine)/.test(parent) ? 'uterus' : /^(ovary|ovarian)/.test(parent) ? 'ovary' : '';
            if (!prefix) return null;
            const slug=normalize(subtype);
            if (/^(breast|uterus|ovary)_/.test(slug) && !slug.startsWith(`${prefix}_`)) return null;
            const scoped=slug.startsWith(`${prefix}_`) ? slug : `${prefix}_${slug}`;
            return catalog.diseases.find(item=>item.id===scoped) || null;
        }
        const identities = subtype ? [subtype, option.value, option.textContent] : [input?.value,option?.dataset.disease,input?.dataset.disease,option?.dataset.scopeKey,option?.textContent];
        return (catalog.diseases || []).find(item => [item.id,item.label,...(item.aliases || [])].some(alias => identities.some(value => value && normalize(value) === normalize(alias)))) || null;
    }
    function baseline() {
        const item = disease();
        if (state.baseline_source === 'manual') {
            const value = number(state.manual_risk_percent);
            return Number.isFinite(value) && value > 0 && value < 100
                ? {available:true,p0:value/100,risk_percent:value,source:'manual',disease:item,reason:'User-specified reference risk'}
                : {available:false,p0:NaN,disease:item,reason:'Enter a manual risk greater than 0% and less than 100%.'};
        }
        if (!catalog) return {available:false,p0:NaN,disease:item,reason:loadError || 'Loading published SEER values…'};
        if (state.age + state.years > 90) return {available:false,p0:NaN,disease:item,reason:'The observation interval must end by age 90.'};
        if (!item) return {available:false,p0:NaN,reason:'No exact SEER disease definition matches the selected endpoint.'};
        const row = (catalog.records || []).find(row => row.disease === item.id && row.sex === state.sex && row.race === state.race && Number(row.age) === state.age && Number(row.years) === state.years);
        const value = number(row?.risk_percent);
        if (row && Number.isFinite(value) && value >= 0 && value < 100) return {available:true,p0:value/100,risk_percent:value,source:row.source,disease:item,record:row};
        return {available:false,p0:NaN,disease:item,reason:item.unsupported_reason || 'SEER does not publish this exact disease, sex, race/ethnicity, age and interval combination.'};
    }
    function p0() {return baseline().p0;}
    function get() {return {...state};}
    function diseaseScope() {
        const input=diseaseInput(), option=input?.selectedOptions?.[0];
        return normalize(option?.dataset.subcategory || option?.dataset.disease || input?.value || 'prostate_cancer');
    }
    function rememberBaseline() {
        if(activeDiseaseScope) scopeBaselines[activeDiseaseScope]={baseline_source:state.baseline_source,manual_risk_percent:state.manual_risk_percent};
    }
    function changeDiseaseScope() {
        rememberBaseline();
        activeDiseaseScope=diseaseScope();
        Object.assign(state,scopeBaselines[activeDiseaseScope] || {baseline_source:'seer',manual_risk_percent:null});
        save();sync();
    }
    function storageKey() {return `metaUro:${window.MetaUro.source}`;}
    function save() {rememberBaseline();try {localStorage.setItem(storageKey(),JSON.stringify({...state,scope_baselines:scopeBaselines}));} catch (_) { /* Settings still work when browser storage is unavailable. */ }}
    function setField(key,value) {
        state[key] = key==='keep_latest_cohort' ? value==='true' : ['age','years','manual_risk_percent','exposure_rate','controls_per_case'].includes(key) ? (value === '' ? null : Number(value)) : value;
        if(key==='manual_risk_percent' && disease()?.manual_required) state.baseline_source=value==='' ? 'seer' : 'manual';
        if (state.age + state.years > 90) state.years = Math.max(...(catalog?.years || [5,10,15,20,25,30,35,40,45,50]).filter(y => state.age + y <= 90));
        save();sync();
        clearTimeout(changeTimer);
        changeTimer = setTimeout(() => window.dispatchEvent(new CustomEvent('analysissettingschange',{detail:get()})),180);
    }
    function fillOptions(id,values) {
        const select = $(id);if (!select) return;
        select.replaceChildren(...values.map(value => {const option=document.createElement('option');option.value=String(value.id ?? value);option.textContent=value.label ?? String(value);return option;}));
    }
    function syncBaseline() {
        const info = baseline(), input = $('pooled-incidence');
        if (input) {input.value=info.available ? String(info.risk_percent) : '';input.readOnly=true;input.title='Change the age, interval, population or manual reference risk in Settings.';input.placeholder='Unavailable';}
        const labels = ['estimated-rr-baseline','baseline-incidence-used','pooled-incidence-label'];
        labels.forEach(id => {if ($(id)) $(id).textContent = info.available ? `${info.risk_percent}% reference risk over ${state.years} years` : 'Reference risk unavailable';});
        return info;
    }
    function sync() {
        for (const [key,id] of Object.entries(fields)) if ($(id)) $(id).value = key==='exposure_rate' ? (state[key]===null ? '' : state[key]*100) : state[key] ?? '';
        if ($('power-study-design')) $('power-study-design').value=state.study_design;
        if ($('power-exposure-percentage')) $('power-exposure-percentage').value=state.exposure_rate===null ? '' : state.exposure_rate*100;
        const retrospective=state.study_design==='retrospective';
        for (const id of ['power-exposure-field','power-allocation-field']) if ($(id)) $(id).hidden=!retrospective;
        for (const id of ['pooled-incidence-field','power-arms-field','power-expected-cases']) if ($(id)) $(id).hidden=retrospective;
        for (const id of ['power-title-measure','pooled-power-measure']) if ($(id)) $(id).textContent=retrospective ? 'OR' : 'RR';
        if ($('settings-years')) for (const option of $('settings-years').options) option.disabled = state.age + Number(option.value) > 90;
        $('settings-manual-field')?.classList.toggle('settings-hidden',state.baseline_source !== 'manual' && !disease()?.manual_required);
        $('settings-exposure-rate-field')?.classList.toggle('settings-hidden',state.study_design !== 'retrospective');
        const display = $('display-measure');if (display) display.value = state.effect_measure;
        const info=syncBaseline();
        if ($('settings-disease-name')) $('settings-disease-name').textContent=info.disease?.label || diseaseInput()?.selectedOptions?.[0]?.textContent || diseaseInput()?.value || 'Select a disease on the main page';
        if ($('settings-disease-definition')) $('settings-disease-definition').textContent=info.disease?.definition || '';
        if ($('settings-risk-value')) $('settings-risk-value').textContent=info.available ? `${info.risk_percent}%` : 'Unavailable';
        const population = [(catalog?.sexes || []).find(x=>x.id===state.sex)?.label || state.sex,(catalog?.races || []).find(x=>x.id===state.race)?.label || state.race].join(' · ');
        if ($('settings-risk-context')) $('settings-risk-context').textContent=`${population}. Age ${state.age}, observed for ${state.years} years, through age ${state.age+state.years}. ${info.available ? (state.baseline_source==='manual' ? 'Manual reference risk.' : 'Directly published SEER value.') : info.reason}`;
        const note=info.available ? 'The selected reference risk is used for effect-measure conversion, missing-case estimation and prospective planning. Case-control planning uses OR and exposure prevalence.' : `${info.reason} HR ≈ RR ≈ OR is used as an identity approximation. Prospective sample size requires a baseline; case-control planning can use the approximate OR and control exposure percentage.`;
        if ($('settings-baseline-note')) $('settings-baseline-note').textContent=note;
        if ($('analysis-baseline-notice')) {$('analysis-baseline-notice').textContent=note;$('analysis-baseline-notice').classList.toggle('baseline-unavailable',!info.available);}
        for (const container of [$('analysis-baseline-citation'),$('settings-source-citation')].filter(Boolean)) {
            container.replaceChildren();
            const text=document.createElement('span');
            if (state.baseline_source==='manual') text.textContent=info.disease?.manual_required ? 'Baseline source: manual reference risk for the selected disease scope.' : 'Baseline source: manual reference risk. Published SEER values are available by choosing SEER in Settings.';
            else if (catalog) {
                text.textContent=`Baseline source: ${catalog.source.label || 'SEER*Explorer'}; ${catalog.source.submission}; ${Array.isArray(catalog.source.data_period) ? catalog.source.data_period.join(', ') : catalog.source.data_period}; released ${catalog.source.release_date}. `;
                const link=document.createElement('a');link.href=info.disease?.source_url || catalog.source.url;link.target='_blank';link.rel='noopener';link.textContent=info.disease?.label ? `Source for ${info.disease.label}` : 'SEER source';container.append(text,link);continue;
            } else text.textContent=loadError || 'Loading SEER source citation…';
            container.append(text);
        }
    }
    function measure(measure) {
            const label=String(measure || '').toUpperCase().replace(/[^A-Z]+/g,' ').trim();
            // Preserve the app's constant-rate treatment of IRR as HR.
            if (/\b(IRR|INCIDENCE RATE RATIO)\b/.test(label)) return catalog?.repository==='metaGut' ? 'RR' : 'HR';
            if (/\b(AOR|OR|ODDS RATIO)\b/.test(label)) return 'OR';
            if (/\b(AHR|HR|HAZARD RATIO)\b/.test(label)) return 'HR';
            if (/\b(ARR|RR|RELATIVE RISK|RISK RATIO)\b/.test(label)) return 'RR';
            return label;
    }
    function convert(value,from,to,base=p0()) {
        const x=number(value);if (!Number.isFinite(x) || x<=0) return NaN;

        from=measure(from);to=measure(to);
        if (!['RR','OR','HR'].includes(from) || !['RR','OR','HR'].includes(to)) return NaN;
        if (from===to || !Number.isFinite(base) || base<=0 || base>=1) return x;
        let rr=from==='OR' ? x/(1-base+base*x) : from==='HR' ? -Math.expm1(x*Math.log1p(-base))/base : x;
        if (to==='RR') return rr;
        if (base*rr>=1) return NaN;
        return to==='OR' ? rr*(1-base)/(1-base*rr) : Math.log1p(-base*rr)/Math.log1p(-base);
    }
    function plotUrl(value) {if (!value) return '';return /^(data:|blob:|https?:|\/)/.test(value) ? value : `/${value}`;}
    function updatePlots(data) {
        const plots=data.forest_plots || {}, scale=state.forest_scale;
        const primary=plots.log;
        if ($('forest-plot') && (primary || data.plot_url)) $('forest-plot').src=plotUrl(primary || data.plot_url);
        const main=$('download-forest-png');
        if (main) {const url=plotUrl(primary || data.plot_url);if(url)main.href=url;else main.removeAttribute('href');main.download=`${disease()?.id || 'analysis'}-${state.effect_measure}-${scale}-forest.png`;main.setAttribute('aria-disabled',url ? 'false' : 'true');}
        const funnel=$('download-funnel-png'), funnelUrl=plotUrl(data.funnel_plot_url);
        if ($('funnel-plot') && funnelUrl) $('funnel-plot').src=funnelUrl;
        if (funnel) {
            if(funnelUrl)funnel.href=funnelUrl;else funnel.removeAttribute('href');
            funnel.download=`${disease()?.id || normalize($('disease')?.selectedOptions?.[0]?.textContent || $('disease')?.value) || 'analysis'}-${normalize($('exposure')?.value) || 'exposure'}-funnel.png`;
            funnel.setAttribute('aria-disabled',funnelUrl ? 'false' : 'true');
        }
        if ($('forest-scale-label')) $('forest-scale-label').textContent=`${state.effect_measure} · logarithmic axis · ${scale==='both' ? 'original and natural-log' : scale==='log' ? 'natural-log' : 'original'} effect and CI labels`;
    }
    function receive(data) {
        if (!data.headline) data={...data,forest_plots:{},plot_url:null,forest_plot:null,funnel_plot_url:null,baujat_plot_url:null};
        $('pooled-power-analysis')?.classList.toggle('hidden',!data.headline);
        const primary=data.forest_plots?.log;
        if(primary) data={...data,plot_url:primary};
        if(primary && data.forest_plot) data={...data,forest_plot:{...data.forest_plot,url:primary}};
        lastResponse=data;sync();updatePlots(data);
        if (!data.headline) return data;
        const canonical=data.canonical_rr || data.headline.canonical_rr;
        return canonical ? {...data,headline:{...data.headline,...canonical,baseline_risk:p0()}} : data;
    }
    async function downloadPlot(event,kind) {
        event.preventDefault();
        const button=$(`download-${kind}-png`);
        if(button?.getAttribute('aria-disabled')==='true')return;
        const images=[$(`${kind}-plot`)];
        try {
            await Promise.all(images.map(img=>img.complete && img.naturalWidth ? Promise.resolve() : img.decode()));
            const width=images[0].naturalWidth,height=images[0].naturalHeight;
            const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
            const ctx=canvas.getContext('2d');ctx.fillStyle='#ffffff';ctx.fillRect(0,0,width,height);ctx.font='20px sans-serif';ctx.textBaseline='top';
            ctx.drawImage(images[0],0,0);
            const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(!blob)throw Error('PNG encoding failed');
            const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=button.download;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        } catch(error) {if($('analysis-baseline-notice'))$('analysis-baseline-notice').textContent=`PNG download is unavailable: ${error.message}`;}
    }
    function zQuantile(p) {
        // Acklam rational approximation, checked against NormalDist.inv_cdf.
        if (!(p>0 && p<1)) return NaN;
        const a=[-39.69683028665376,220.9460984245205,-275.9285104469687,138.3577518672690,-30.66479806614716,2.506628277459239];
        const b=[-54.47609879822406,161.5858368580409,-155.6989798598866,66.80131188771972,-13.28068155288572];
        const c=[-0.007784894002430293,-0.3223964580411365,-2.400758277161838,-2.549732539343734,4.374664141464968,2.938163982698783];
        const d=[0.007784695709041462,0.3224671290700398,2.445134137142996,3.754408661907416];
        const poly=(coeff,x)=>coeff.reduce((sum,v)=>sum*x+v,0);
        if(p<0.02425 || p>0.97575){const q=Math.sqrt(-2*Math.log(p<0.5?p:1-p));return (p<0.5?1:-1)*poly(c,q)/(poly(d,q)*q+1);}
        const q=p-0.5,r=q*q;return poly(a,r)*q/(poly(b,r)*r+1);
    }
    function power(elements,headline) {
        if (!headline || !elements.pooledPowerAnalysis) return;
        const write=(key,value)=>{if(elements[key])elements[key].textContent=value;};
        elements.pooledPowerAnalysis.classList.remove('hidden');
        const key={lower:'ci_low',upper:'ci_upp',pi_lower:'pi_low',pi_upper:'pi_upp'}[elements.powerEffect?.value] || 'pooled_es';
        const rr=number(headline[key]), base=p0();
        const retrospective=state.study_design==='retrospective';
        const targetMeasure=retrospective?'OR':'RR';
        const estimate=retrospective?convert(rr,'RR','OR',base):rr;
        const values=['pooled_es','ci_low','ci_upp'].map(k=>retrospective?convert(headline[k],'RR','OR',base):number(headline[k]));
        const format=v=>Number.isFinite(v)?v.toFixed(3):'NA';
        if ($('power-effect-summary')) $('power-effect-summary').textContent=`Pooled ${targetMeasure}: ${format(values[0])} (95% CI: ${format(values[1])}–${format(values[2])})`;
        for(const id of ['power-title-measure','pooled-power-measure']) if($(id))$(id).textContent=targetMeasure;
        write('pooledPowerEsText',Number.isFinite(estimate) ? estimate.toFixed(3) : 'NA');
        const fail=reason=>{write('pooledPowerNTotal',`Unavailable: ${reason}`);write('pooledPowerPerGroup','');write('pooledPowerCases','—');};
        if (!retrospective && !(base>0 && base<1)) return fail('no reference risk');
        if (!(rr>0) || !Number.isFinite(rr)) return fail('selected effect estimate is unavailable');
        const alpha=number(elements.powerAlpha?.value ?? 0.05), target=number(elements.powerValue?.value ?? 0.8), sides=number(elements.powerSides?.value ?? 2);
        if (!(alpha>0 && alpha<1 && target>0.5 && target<1 && [1,2].includes(sides))) return fail('invalid power or significance settings');
        write('alphaTextDisplay',String(alpha));write('powerTextDisplay',`${Math.round(target*100)}% power`);write('sidesTextDisplay',sides===1 ? 'one-sided' : 'two-sided');
        let p1=base,p2=base*rr,arms=Number(elements.powerArms?.value || 2);
        if (elements.powerArms) elements.powerArms.disabled=retrospective;
        if (retrospective) {
            const odds=estimate,q0=number(state.exposure_rate),ratio=number(state.controls_per_case);
            if (!(q0>0 && q0<1) || !Number.isFinite(odds) || !(odds>0)) return fail('enter a control exposure percentage between 0 and 100 and a valid OR');
            if (!(ratio>0) || !Number.isFinite(ratio)) return fail('case-control ratio must be positive');
            const q1=1/(1+((1-q0)/q0)/odds),diff=q1-q0;
            if(Math.abs(diff)<1e-12)return fail('OR is 1');
            const avg=(q1+ratio*q0)/(1+ratio),za=zQuantile(1-alpha/sides),zb=zQuantile(target);
            const nCases=Math.ceil((za*Math.sqrt((1+1/ratio)*avg*(1-avg))+zb*Math.sqrt(q1*(1-q1)+q0*(1-q0)/ratio))**2/diff**2);
            const nControls=Math.ceil(ratio*nCases);
            if(!Number.isFinite(nCases+nControls) || nCases<1)return fail('sample size is not finite');
            write('pooledPowerNTotal',(nCases+nControls).toLocaleString());
            write('pooledPowerPerGroup',`(${nCases.toLocaleString()} cases and ${nControls.toLocaleString()} controls)`);
            write('pooledPowerCases',nCases.toLocaleString());
            if($('sample-design-note'))$('sample-design-note').textContent=`Unmatched case-control planning from OR; ${q0*100}% of controls exposed; case:control ratio 1:${ratio}.${Number.isFinite(base)?'':' OR uses the stated HR/RR/OR identity approximation because no baseline is available.'}`;
            return;
        }
        if (!(p2>=0 && p2<=1)) return fail('effect implies a risk outside 0–100%');
        const diff=p2-p1;if (Math.abs(diff)<1e-12) return fail('effect is 1');
        const za=zQuantile(1-alpha/sides),zb=zQuantile(target),avg=(p1+p2)/2;
        const n=arms===1 ? Math.ceil((za*Math.sqrt(p1*(1-p1))+zb*Math.sqrt(p2*(1-p2)))**2/diff**2) : Math.ceil((za*Math.sqrt(2*avg*(1-avg))+zb*Math.sqrt(p1*(1-p1)+p2*(1-p2)))**2/diff**2);
        if (!Number.isFinite(n) || n<1) return fail('sample size is not finite');
        write('pooledPowerNTotal',(n*arms).toLocaleString());
        write('pooledPowerPerGroup',arms===1 ? '(single arm)' : `(${n.toLocaleString()} per group)`);
        write('pooledPowerCases',Math.ceil(n*(arms===1 ? p2 : p1+p2)).toLocaleString());
        if ($('sample-design-note')) $('sample-design-note').textContent=`Prospective planning from RR; event risks use age ${state.age} over ${state.years} years and the selected reference population.`;
    }
    function renderHierarchy(map) {
        const target=$('settings-exposure-hierarchy');if (!target) return;
        target.replaceChildren();
        const parents={};for(const [child,list] of Object.entries(map || {})) for(const parent of list) (parents[parent] ||= []).push(child);
        for(const parent of Object.keys(parents).sort()) {
            const detail=document.createElement('details'),summary=document.createElement('summary'),list=document.createElement('ul');summary.textContent=`${parent} (${parents[parent].length})`;
            for(const child of parents[parent].sort()) {const li=document.createElement('li');li.textContent=child;list.append(li);}detail.append(summary,list);target.append(detail);
        }
    }
    async function setup() {
        const dialog=$('analysis-settings-dialog');
        $('open-analysis-settings')?.addEventListener('click',()=>{sync();dialog.showModal();});
        $('close-analysis-settings')?.addEventListener('click',()=>dialog.close());
        $('download-forest-png')?.addEventListener('click',event=>downloadPlot(event,'forest'));
        $('download-funnel-png')?.addEventListener('click',event=>downloadPlot(event,'funnel'));
        dialog?.addEventListener('click',event=>{if(event.target===dialog)dialog.close();});
        for(const [key,id] of Object.entries(fields)) $(id)?.addEventListener('change',event=>setField(key,key==='exposure_rate' && event.target.value!=='' ? Number(event.target.value)/100 : event.target.value));
        $('power-study-design')?.addEventListener('change',event=>setField('study_design',event.target.value));
        $('power-exposure-percentage')?.addEventListener('change',event=>setField('exposure_rate',event.target.value==='' ? '' : Number(event.target.value)/100));
        $('display-measure')?.addEventListener('change',event=>setField('effect_measure',event.target.value));
        diseaseInput()?.addEventListener('change',changeDiseaseScope);
        $('settings-hierarchy-search')?.addEventListener('input',event=>{
            const term=event.target.value.trim().toLowerCase();
            for(const detail of $('settings-exposure-hierarchy').children) {detail.hidden=!!term && !detail.textContent.toLowerCase().includes(term);detail.open=!!term && !detail.hidden;}
        });
        const results=await Promise.allSettled([fetch(`${window.MetaUro.prefix}/static/seer-baseline.json`).then(r=>{if(!r.ok)throw Error(`SEER catalog returned ${r.status}`);return r.json();}),fetch(`${window.MetaUro.prefix}/static/settings-exposure-hierarchy.json`).then(r=>{if(!r.ok)throw Error('Hierarchy unavailable');return r.json();})]);
        if(results[0].status==='fulfilled') {
            catalog=results[0].value;state={...defaults,...catalog.defaults};
            try {
                const saved=JSON.parse(localStorage.getItem(storageKey()) || 'null');
                if(saved && typeof saved === 'object' && !Array.isArray(saved)) {
                    const {scope_baselines:storedBaselines,...savedState}=saved;
                    state=sanitizeState(savedState);
                    scopeBaselines={};
                    if(storedBaselines && typeof storedBaselines==='object' && !Array.isArray(storedBaselines)) {
                        for(const [scope,value] of Object.entries(storedBaselines)) scopeBaselines[scope]=sanitizeBaseline(value);
                    }
                }
            } catch (_) {}
            activeDiseaseScope=diseaseScope();
            Object.assign(state,scopeBaselines[activeDiseaseScope] || {baseline_source:'seer',manual_risk_percent:null});
            fillOptions(fields.age,catalog.ages);fillOptions(fields.years,catalog.years);fillOptions(fields.sex,catalog.sexes);fillOptions(fields.race,catalog.races);
            if (!(catalog.ages || []).includes(state.age)) state.age=60;
            if (!(catalog.years || []).includes(state.years) || state.age+state.years>90) state.years=Math.min(10,90-state.age);
            if (!(catalog.sexes || []).some(row=>row.id===state.sex)) state.sex=catalog.defaults.sex;
            if (!(catalog.races || []).some(row=>row.id===state.race)) state.race=catalog.defaults.race;
        } else {loadError='The published SEER catalog could not be loaded. A manual reference risk can still be entered.';fillOptions(fields.age,[30,35,40,45,50,55,60,65,70,75,80]);fillOptions(fields.years,[5,10,15,20,25,30,35,40,45,50]);}
        if(results[1].status==='fulfilled')renderHierarchy(results[1].value);
        else if($('settings-exposure-hierarchy'))$('settings-exposure-hierarchy').textContent='The exposure hierarchy could not be loaded.';
        sync();readyResolve();window.dispatchEvent(new Event('analysissettingsready'));
    }
    function sanitizeBaseline(value) {
        const risk=number(value?.manual_risk_percent);
        return value?.baseline_source==='manual' && Number.isFinite(risk) && risk>0 && risk<100
            ? {baseline_source:'manual',manual_risk_percent:risk}
            : {baseline_source:'seer',manual_risk_percent:null};
    }
    function sanitizeState(saved) {
        const next={...defaults,...catalog?.defaults};
        const choices={effect_measure:['RR','HR','OR'],forest_scale:['both','log','original'],study_design:['prospective','retrospective'],sex:(catalog?.sexes || []).map(x=>x.id),race:(catalog?.races || []).map(x=>x.id),age:catalog?.ages || [],years:catalog?.years || []};
        for(const [key,allowed] of Object.entries(choices)) if(allowed.includes(saved[key])) next[key]=saved[key];
        if(Number.isFinite(saved.exposure_rate) && saved.exposure_rate>0 && saved.exposure_rate<1)next.exposure_rate=saved.exposure_rate;
        if(Number.isFinite(saved.controls_per_case) && saved.controls_per_case>0)next.controls_per_case=saved.controls_per_case;
        if(next.age+next.years>90)next.years=Math.max(...(catalog?.years || [5,10]).filter(x=>x+next.age<=90));
        return next;
    }
    function reset() {
        state={...defaults,...catalog?.defaults};scopeBaselines={};activeDiseaseScope=diseaseScope();save();sync();
        window.dispatchEvent(new CustomEvent('analysissettingschange',{detail:get()}));
    }
    window.AnalysisSettings={ready,get,baseline,p0,sync,syncBaseline,convert,measure,receive,power,plotUrl,set:setField,reset};
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',setup,{once:true});else setup();
})();
