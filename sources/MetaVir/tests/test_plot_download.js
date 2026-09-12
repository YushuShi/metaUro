/* PNG downloads preserve the active image and disable stale diagnostics. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const repository = path.basename(root);
const catalog = JSON.parse(fs.readFileSync(path.join(root, 'static/seer-baseline.json')));
const disease = catalog.diseases.find(item => catalog.records.some(row => row.disease === item.id));
const listeners = {}, downloads = [], draws = [], blobs = [];
function element(id, extra = {}) {
    const attrs = {};
    return {dataset: {}, ...extra,
        addEventListener(type, fn) {listeners[id + ':' + type] = fn;},
        setAttribute(key, value) {attrs[key] = value;},
        getAttribute(key) {return attrs[key];},
        removeAttribute(key) {delete attrs[key]; if(key === 'href') delete this.href;}};
}
const option = {value:disease.label,textContent:disease.label,dataset:{disease:disease.label}};
const nodes = {
    disease:element('disease',{value:disease.label,selectedOptions:[option]}),
    exposure:element('exposure',{value:'Vitamin A'}),
    'analysis-settings-dialog':element('dialog',{dataset:{repository}}),
    'download-forest-png':element('download-forest-png'),
    'download-funnel-png':element('download-funnel-png'),
    'forest-plot':element('forest-plot',{complete:true,naturalWidth:1890,naturalHeight:700}),
    'funnel-plot':element('funnel-plot',{complete:true,naturalWidth:1120,naturalHeight:770})
};
const document = {readyState:'complete',getElementById:id=>nodes[id] || null,addEventListener(){},
    createElement(tag) {
        if(tag === 'canvas') return {getContext(){return {fillRect(){},drawImage(image){draws.push(image);}};},
            toBlob(callback,type){blobs.push({width:this.width,height:this.height,type});callback({type});}};
        if(tag === 'a') return {click(){downloads.push({href:this.href,download:this.download});}};
        return {};
    }};
const window = {addEventListener(){},dispatchEvent(){}};
const sandbox = {window,document,location:{pathname:'/'},localStorage:{getItem(){return null;},setItem(){}},
    fetch:async url=>({ok:true,json:async()=>url.includes('seer-baseline')?catalog:{}}),
    setTimeout(){},clearTimeout(){},CustomEvent:class{},Event:class{},
    URL:{createObjectURL(){return 'blob:download';},revokeObjectURL(){}}};
(async()=>{
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(root,'static/settings.js'),'utf8'),sandbox);
    await window.AnalysisSettings.ready;
    const response = {headline:{pooled_es:1.2},canonical_rr:{pooled_es:1.2},
        forest_plots:{log:'data:image/png;base64,FOREST'},funnel_plot_url:'data:image/png;base64,FUNNEL'};
    window.AnalysisSettings.receive(response);
    assert.equal(nodes['download-funnel-png'].getAttribute('aria-disabled'),'false');
    assert.equal(nodes['funnel-plot'].src,response.funnel_plot_url);
    await listeners['download-funnel-png:click']({preventDefault(){}});
    assert.equal(downloads.length,1);
    assert.equal(downloads[0].download,`${disease.id}-vitamin_a-funnel.png`);
    assert.equal(draws[0],nodes['funnel-plot']);
    assert.deepEqual(blobs[0],{width:1120,height:770,type:'image/png'});
    await listeners['download-forest-png:click']({preventDefault(){}});
    assert.equal(draws[1],nodes['forest-plot']);
    assert.deepEqual(blobs[1],{width:1890,height:700,type:'image/png'});
    window.AnalysisSettings.receive({headline:null,funnel_plot_url:'data:image/png;base64,OLD'});
    assert.equal(nodes['download-funnel-png'].getAttribute('aria-disabled'),'true');
    assert.equal(nodes['download-funnel-png'].href,undefined);
    await listeners['download-funnel-png:click']({preventDefault(){}});
    assert.equal(downloads.length,2);
    console.log(repository + ': forest and funnel PNG downloads passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
