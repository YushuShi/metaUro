/* Run: node tests/navigation.test.js
   Exercises URL-carried settings and source requests without browser dependencies. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const navigationCode = fs.readFileSync(path.join(root, 'static/metauro.js'), 'utf8');
const scopes = [
    {source:'metaPee',id:'bladder_cancer',label:'Bladder cancer',prefix:'/evidence/urinary',group_id:'urinary',group:'Urinary tract'},
    {source:'metaPee',id:'renal_cell_carcinoma',label:'Renal cell carcinoma',prefix:'/evidence/urinary',group_id:'urinary',group:'Urinary tract'},
    {source:'MetaVir',id:'prostate_cancer',label:'Prostate cancer',prefix:'/evidence/prostate',group_id:'prostate',group:'Prostate'},
    {source:'MetaVir',id:'aggressive_prostate_cancer',label:'Aggressive prostate cancer',prefix:'/evidence/prostate',group_id:'prostate',group:'Prostate'},
];
class TestEvent {
    constructor(type, options={}) { this.type=type;Object.assign(this,options); }
    stopImmediatePropagation() { this.stopped=true; }
    preventDefault() { this.defaultPrevented=true; }
}
function runtime({page='evidence',scope=0,mode='no',track='combined',sourceScript=false,selector='disease'}={}) {
    const current=scopes[scope], nodes={}, links=[], documentHandlers={}, windowHandlers={}, assigned=[], fetches=[];
    const pathname=page==='settings'?'/settings':`${current.prefix}/${page==='summary'?'summary':track==='chatgpt'?'chatgpt':''}`;
    let currentUrl=new URL(`http://localhost${pathname}?scope=${current.id}&include_subexposures=${mode}${track==='chatgpt'?'&track=chatgpt':''}`);
    let resets=0;
    function dispatchDocument(event, capture) {
        for(const handler of documentHandlers[event.type] || []) {
            if(handler.capture===capture&&!event.stopped)handler.fn(event);
        }
    }
    function element(id, extra={}) {
        const handlers={};
        return {id,dataset:{},value:'',textContent:'',tagName:'A',href:'/',
            addEventListener(type,fn){(handlers[type] ||= []).push(fn);},
            dispatchEvent(event){
                event.target=this;dispatchDocument(event,true);
                for(const fn of handlers[event.type] || [])if(!event.stopped)fn(event);
                if(event.bubbles&&!event.stopped)dispatchDocument(event,false);
                return !event.defaultPrevented;
            },
            getAttribute(name){return this[name] ?? null;},setAttribute(name,value){this[name]=String(value);},...extra};
    }
    function option(item) {return {value:item.label,dataset:{source:item.source,scopeKey:item.id}};}
    nodes[selector]=element(selector,{selectedOptions:[option(current)]});
    nodes.exposure=element('exposure',{value:'Vitamin D'});
    if(page==='settings') {
        nodes['include-subexposures']=element('include-subexposures',{value:mode});
        nodes['reset-settings']=element('reset-settings');
        nodes['settings-save-status']=element('settings-save-status');
    }
    for(const name of ['settings','summary','selection','evidence','home']) {
        links.push(element(name,{dataset:{uroNav:name==='home'?'evidence':name}}));
    }
    const window={MetaUro:{source:current.source,prefix:current.prefix,current,scopes,page,track},
        AnalysisSettings:{reset(){resets++;}},
        fetch:async(input,options={})=>{fetches.push({input,options});return {ok:true};},
        addEventListener(type,fn){(windowHandlers[type] ||= []).push(fn);},
        dispatchEvent(event){for(const fn of windowHandlers[event.type] || [])fn(event);}};
    const sandbox={window,URL,Event:TestEvent,CustomEvent:TestEvent,
        location:{get href(){return currentUrl.href;},get origin(){return currentUrl.origin;},assign(url){assigned.push(new URL(url,currentUrl));}},
        history:{replaceState(_state,_title,url){currentUrl=new URL(url,currentUrl);}},
        document:{getElementById:id=>nodes[id] || null,createElement:tag=>element(tag),head:{append(){}},
            querySelectorAll:query=>query==='[data-uro-nav]'||query==='a[href], object[data]'?links:[],
            addEventListener(type,fn,options){(documentHandlers[type] ||= []).push({fn,capture:options===true||Boolean(options?.capture)});}}};
    vm.createContext(sandbox);vm.runInContext(navigationCode,sandbox);
    if(sourceScript)vm.runInContext(fs.readFileSync(path.join(root,'sources',current.source,'static/subexposures.js'),'utf8'),sandbox);
    dispatchDocument(new TestEvent('DOMContentLoaded'),false);
    return {nodes,links,assigned,fetches,window,get url(){return currentUrl;},get resets(){return resets;},
        link:name=>new URL(links.find(link=>link.id===name).href,currentUrl),
        changeMode(value){nodes['include-subexposures'].value=value;nodes['include-subexposures'].dispatchEvent(new TestEvent('change',{bubbles:true}));},
        select(index){nodes[selector].selectedOptions=[option(scopes[index])];nodes[selector].dispatchEvent(new TestEvent('change',{bubbles:true}));},
        reset(){nodes['reset-settings'].dispatchEvent(new TestEvent('click',{bubbles:true}));}};
}
const cases=[];
function test(name,fn){cases.push({name,fn});}
test('exclusion follows navigation and cross-source disease changes without a main-page selector',()=>{
    const app=runtime();assert.equal(app.nodes['include-subexposures'],undefined);
    for(const link of app.links)assert.equal(new URL(link.href,app.url).searchParams.get('include_subexposures'),'no');
    app.select(2);const target=app.assigned[0];
    assert.equal(target.pathname,'/evidence/prostate/');
    assert.equal(target.searchParams.get('scope'),'prostate_cancer');
    assert.equal(target.searchParams.get('include_subexposures'),'no');
    assert.equal(target.searchParams.get('exposure'),'Vitamin D');
});
test('summary scope changes preserve exclusion without a selector',()=>{
    const app=runtime({page:'summary',selector:'summary-scope'});app.select(1);
    assert.equal(app.link('settings').searchParams.get('scope'),'renal_cell_carcinoma');
    assert.equal(app.link('summary').searchParams.get('include_subexposures'),'no');
    app.select(3);
    assert.equal(app.assigned[0].pathname,'/evidence/prostate/summary');
    assert.equal(app.assigned[0].searchParams.get('scope'),'aggressive_prostate_cancer');
    assert.equal(app.assigned[0].searchParams.get('include_subexposures'),'no');
});
test('Settings changes update the URL, return link, and logo destination',()=>{
    const app=runtime({page:'settings',mode:'yes'});app.changeMode('no');
    assert.equal(app.url.searchParams.get('include_subexposures'),'no');
    for(const name of ['evidence','home']) {
        assert.equal(app.link(name).pathname,'/evidence/urinary/');
        assert.equal(app.link(name).searchParams.get('scope'),'bladder_cancer');
        assert.equal(app.link(name).searchParams.get('include_subexposures'),'no');
    }
    assert.equal(app.link('summary').searchParams.get('include_subexposures'),'no');
    app.select(3);assert.equal(app.assigned[0].pathname,'/settings');
    assert.equal(app.assigned[0].searchParams.get('scope'),'aggressive_prostate_cancer');
    assert.equal(app.assigned[0].searchParams.get('include_subexposures'),'no');
});
test('ChatGPT settings return to the same track and disease',()=>{
    const app=runtime({page:'settings',scope:3,track:'chatgpt'});
    for(const name of ['evidence','home']) {
        assert.equal(app.link(name).pathname,'/evidence/prostate/chatgpt');
        assert.equal(app.link(name).searchParams.get('track'),'chatgpt');
        assert.equal(app.link(name).searchParams.get('scope'),'aggressive_prostate_cancer');
        assert.equal(app.link(name).searchParams.get('include_subexposures'),'no');
    }
});
test('Study selection preserves disease, exposure, and analysis track',()=>{
    const app=runtime({scope:3,track:'chatgpt'});
    const target=app.link('selection');
    assert.equal(target.pathname,'/study-selection');
    assert.equal(target.searchParams.get('scope'),'aggressive_prostate_cancer');
    assert.equal(target.searchParams.get('track'),'chatgpt');
    assert.equal(target.searchParams.get('include_subexposures'),'no');
    assert.equal(target.searchParams.get('exposure'),'Vitamin D');
    const page=runtime({page:'selection',scope:3,track:'chatgpt'});
    assert.equal(page.link('evidence').pathname,'/evidence/prostate/chatgpt');
    assert.equal(page.link('evidence').searchParams.get('scope'),'aggressive_prostate_cancer');
    assert.equal(page.link('evidence').searchParams.get('include_subexposures'),'no');
    assert.equal(page.link('evidence').searchParams.get('exposure'),'Vitamin D');
});
test('Study-selection navigation preserves the requested related-exposure preference',()=>{
    for(const mode of ['yes','no']) {
        const page=runtime({page:'selection',scope:0,mode});
        for(const link of page.links)assert.equal(new URL(link.href,page.url).searchParams.get('include_subexposures'),mode);
    }
});
test('Reset restores related-exposure inclusion as well as analysis settings',()=>{
    const app=runtime({page:'settings'});app.reset();
    assert.equal(app.resets,1);assert.equal(app.nodes['include-subexposures'].value,'yes');
    assert.equal(app.url.searchParams.get('include_subexposures'),'yes');
    for(const link of app.links)assert.equal(new URL(link.href,app.url).searchParams.get('include_subexposures'),'yes');
});
test('legacy exclusion query values are normalized before navigation',()=>{
    for(const mode of ['false','0','NO']) {
        const app=runtime({mode});assert.equal(app.link('settings').searchParams.get('include_subexposures'),'no');
        app.select(2);assert.equal(app.assigned[0].searchParams.get('include_subexposures'),'no');
    }
});
for(const scope of [0,2])test(`${scopes[scope].group} fetches carry exclusion without a page selector`,async()=>{
    const app=runtime({scope,sourceScript:true});
    assert.equal(app.window.includeSubexposuresMode(),'no');
    await app.window.fetch(`${scopes[scope].prefix}/analyze`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({exposure:'Vitamin D',include_subexposures:'yes'})});
    const call=app.fetches[0];
    assert.equal(new URL(call.input).searchParams.get('include_subexposures'),'no');
    assert.equal(JSON.parse(call.options.body).include_subexposures,'no');
    assert.equal(JSON.parse(call.options.body).exposure,'Vitamin D');
    await app.window.fetch('https://example.org/reference');
    assert.equal(app.fetches[1].input,'https://example.org/reference');
});
(async()=>{
    let failed=0;
    for(const {name,fn} of cases) {
        try{await fn();console.log(`PASS ${name}`);}catch(error){failed++;console.error(`FAIL ${name}: ${error.stack}`);}
    }
    console.log(`${cases.length-failed}/${cases.length} navigation checks passed`);process.exitCode=failed?1:0;
})().catch(error=>{console.error(error);process.exitCode=1;});
