/* Execute the shipped browser module against synthetic DOM and API fixtures. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path');
const sourcePath=process.argv[2] || path.resolve(__dirname,'../static/sample-size-summary.js');
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));};
class Node {
 constructor(tag='div',value=''){this.tagName=tag;this._value=value;this.children=[];this.listeners={};this.attributes={};this._text='';this.dataset={};this.disabled=false;this.classList={toggle:(key,value)=>{this.classes||={};this.classes[key]=value;}};}
 get value(){return String(this._value);}set value(value){this._value=String(value);}
 get options(){return this.children;}get selectedOptions(){return this.children.filter(x=>x.value===this.value);}
 get textContent(){return this._text+this.children.map(x=>x.textContent||'').join('');}set textContent(text){this._text=String(text);this.children=[];}
 append(...nodes){this.children.push(...nodes);}appendChild(node){this.append(node);}
 replaceChildren(...nodes){this.children=[...nodes];this._text='';if(this.tagName==='select'&&!nodes.some(x=>x.value===this.value))this.value=nodes[0]?.value||'';}
 setAttribute(name,value){this.attributes[name]=String(value);}getAttribute(name){return this.attributes[name];}
 addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}async dispatch(name){for(const fn of this.listeners[name]||[])await fn({target:this,preventDefault(){}});await flush();}
 reportValidity(){return this.valid!==false;}
}
function descendants(node,predicate){return [node,...node.children.flatMap(x=>descendants(x,predicate))].filter(predicate);}
async function runtime({search='',state={keep_latest_cohort:true},repository='MetaGut',source='metaPee',storage={},fetcher}={}){
 const nodes={};const named={'sample-summary-disease':'disease','sample-baseline':'baseline_percent','sample-control-exposure':'control_exposure_percent','sample-allocation':'allocation_ratio','sample-alpha':'alpha','sample-power':'power','sample-sidedness':'sidedness'};
 for(const [id,name] of Object.entries(named)){nodes[id]=new Node(id==='sample-summary-disease'?'select':'input');nodes[id].name=name;}
 for(const id of ['sample-summary-form','sample-summary-status','sample-summary-plots','sample-baseline-note','sample-summary-submit','sample-summary-context'])nodes[id]=new Node();
 Object.assign(nodes['sample-control-exposure'],{value:'50'});nodes['sample-allocation'].value='1';nodes['sample-alpha'].value='.05';nodes['sample-power'].value='.8';nodes['sample-sidedness'].value='two';
 const document={listeners:{},body:{dataset:{sampleSizeRepository:repository,sampleSizeSource:source}},getElementById:id=>nodes[id]||null,createElement:tag=>new Node(tag),createElementNS:(_ns,tag)=>new Node(tag),addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}};
 const window={listeners:{},AnalysisSettings:{ready:Promise.resolve(),get:()=>state},addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}};
 if(state===null)delete window.AnalysisSettings;
 const location={href:'https://example.test/sample-size-summary'+search,search};const history={replaceState(_state,_title,next){location.href=String(next);location.search=new URL(next).search;}};
 const requests=[];let nextResponse=null;
 const catalog={diseases:[{value:'A',label:'Disease A',source:'metaPee',baseline_percent:1},{value:'B',label:'Disease B',source:'MetaVir',baseline_percent:2}],defaults:{alpha:.05,power:.8,sidedness:'two',allocation_ratio:1,control_exposure_percent:50}};
 const defaultData={baseline_percent:1,panels:[{measurement:'dietary',design:'retrospective',points:[{exposure:'Soy',effect_size:.8,total_n:2000,study_count:2}]},{measurement:'all',design:'prospective',points:[{exposure:'Coffee',effect_size:.7,total_n:1000,study_count:2},{exposure:'One study',effect_size:.9,total_n:200,study_count:1},{exposure:'Invalid N',effect_size:.9,total_n:Infinity,study_count:3},{exposure:'Missing ES',effect_size:null,total_n:20,study_count:3}]}]};
 const context={document,window,URL,URLSearchParams,location,history,AbortController,console,setTimeout,clearTimeout,localStorage:{getItem(key){return storage[key] ? JSON.stringify(storage[key]) : null;}},FormData:class{constructor(form){}*[Symbol.iterator](){for(const [id,name] of Object.entries(named))yield[name,nodes[id].value];}},fetch:async(url,options)=>{const query=new URL(url,location.href);requests.push(query);if(fetcher)return fetcher(query,options,{catalog,defaultData});if(nextResponse){const result=nextResponse;nextResponse=null;return result;}return{ok:true,json:async()=>query.pathname.endsWith('/catalog')?catalog:defaultData};}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(sourcePath,'utf8'),context);
 for(const fn of document.listeners.DOMContentLoaded||[])fn();await flush();
 return{nodes,requests,window,state,location,catalog,defaultData,setNext(value){nextResponse=value;},async submit(){await nodes['sample-summary-form'].dispatch('submit');},async changeDisease(value){nodes['sample-summary-disease'].value=value;await nodes['sample-summary-disease'].dispatch('change');},async editBaseline(value){nodes['sample-baseline'].value=value;await nodes['sample-baseline'].dispatch('input');}};
}
(async()=>{
 const ui=await runtime();assert.equal(ui.nodes['sample-baseline'].value,'1');assert.equal(ui.nodes['sample-summary-plots'].children.length,4);
 const titles=ui.nodes['sample-summary-plots'].children.map(card=>card.children[0].textContent);assert.deepEqual(titles,['All exposure measurements · Prospective','All exposure measurements · Retrospective','Dietary only · Prospective','Dietary only · Retrospective']);
 const text=ui.nodes['sample-summary-plots'].textContent;assert(text.includes('Coffee'));for(const excluded of ['One study','Invalid N','Missing ES'])assert(!text.includes(excluded));
 await ui.editBaseline('7');await ui.submit();assert.equal(ui.requests.at(-1).searchParams.get('baseline_percent'),'7');
 await ui.changeDisease('B');assert.equal(ui.nodes['sample-baseline'].value,'2');assert.equal(ui.requests.at(-1).searchParams.get('baseline_percent'),'2');assert.equal(ui.requests.at(-1).searchParams.get('disease'),'B');
 for(const [id,value] of Object.entries({'sample-control-exposure':'35','sample-allocation':'2','sample-alpha':'.01','sample-power':'.9','sample-sidedness':'one'}))ui.nodes[id].value=value;
 await ui.submit();for(const [key,value] of Object.entries({control_exposure_percent:'35',allocation_ratio:'2',alpha:'.01',power:'.9',sidedness:'one'}))assert.equal(ui.requests.at(-1).searchParams.get(key),value);
 ui.setNext({ok:false,status:400,json:async()=>({error:'Validation rejected'})});await ui.submit();assert.equal(ui.nodes['sample-summary-status'].textContent,'Validation rejected');assert.equal(ui.nodes['sample-summary-plots'].children.length,0);assert.equal(ui.nodes['sample-summary-submit'].disabled,false);assert.equal(ui.nodes['sample-summary-plots'].attributes['aria-busy'],'false');
 console.log('PASS baseline editing/reset, 4-panel order, >=2-study/finite filtering, controls, API errors and busy reset.');
 const explicit=await runtime({search:'?keep_latest_cohort=false&age=70',state:{keep_latest_cohort:true,age:60}});
 for(const url of explicit.requests){assert.equal(url.searchParams.get('keep_latest_cohort'),'false');assert.equal(url.searchParams.get('age'),'70');}
 let pending=[];const racing=await runtime({fetcher:async(url,options,{catalog,defaultData})=>url.pathname.endsWith('/catalog')?{ok:true,json:async()=>catalog}:new Promise(resolve=>pending.push({resolve,defaultData,options}))});
 // Make a second update while the initial one is still pending.
 await racing.changeDisease('B');assert.equal(pending.length,2);pending[1].resolve({ok:true,json:async()=>pending[1].defaultData});await flush();
 const before=racing.nodes['sample-summary-status'].textContent;pending[0].resolve({ok:false,status:503,json:async()=>({error:'Stale failure'})});await flush();
 assert.equal(racing.nodes['sample-summary-status'].textContent,before,'A stale failure must not replace newer success');assert.equal(racing.nodes['sample-summary-plots'].children.length,4);
 const dense=await runtime({fetcher:async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:{panels:[{measurement:'all',design:'prospective',points:Array.from({length:24},(_,i)=>({exposure:'Long exposure name '+i,effect_size:.8,total_n:1000,study_count:3}))}]}})});
 const labels=descendants(dense.nodes['sample-summary-plots'],node=>node.attributes.class==='sample-point-label').map(node=>({x:Number(node.attributes.x),y:Number(node.attributes.y)-12,w:Math.max(...node.children.map(line=>line.textContent.length))*7+10,h:node.children.length*15+4}));
 let collisions=0;for(let i=0;i<labels.length;i++)for(let j=i+1;j<labels.length;j++){const a=labels[i],b=labels[j];if(a.x<b.x+b.w&&a.x+a.w>b.x&&a.y<b.y+b.h&&a.y+a.h>b.y)collisions++;}
 assert.equal(labels.length,24);assert.equal(collisions,0,'Clustered exposure callouts must remain readable');
 const continuous=await runtime({fetcher:async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:{panels:[{measurement:'all',design:'prospective',effect_label:'Mean difference',points:[{exposure:'Negative mean difference',effect_size:-2,total_n:100,study_count:2},{exposure:'Another negative mean difference',effect_size:-1,total_n:200,study_count:3}]}]}})});
 const circles=descendants(continuous.nodes['sample-summary-plots'],node=>node.tagName==='circle');assert.equal(circles.length,2);for(const dot of circles){assert(Number.isFinite(Number(dot.attributes.cx)));assert(Number(dot.attributes.cx)>=82&&Number(dot.attributes.cx)<=650,'Negative mean differences must stay inside the plot bounds');}
 assert(continuous.nodes['sample-summary-plots'].textContent.includes('Mean difference'));
 const uro=await runtime({repository:'MetaUro',state:null,storage:{'metaUro:metaPee':{age:60,sex:'both'},'metaUro:MetaVir':{age:70,sex:'male'}},fetcher:async(url,options,{catalog,defaultData})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?{...catalog,diseases:catalog.diseases.map(item=>({...item,baseline_percent:item.value==='B'&&url.searchParams.get('age')==='70'?9:item.baseline_percent}))}:defaultData})});
 await uro.changeDisease('B');assert.equal(uro.nodes['sample-baseline'].value,'9','Changing evidence source must refresh its baseline under that source’s saved settings');assert.equal(uro.requests.at(-1).searchParams.get('age'),'70');assert.equal(uro.requests.at(-1).searchParams.get('baseline_percent'),'9');
 for(const url of [...ui.requests,...uro.requests].filter(url=>url.pathname.endsWith('/catalog')))assert(!url.searchParams.has('baseline_percent'),'Per-disease overrides must not leak into other catalog baselines');
 console.log('PASS explicit settings, stale-request protection, negative mean differences, dense labels and source-specific baseline refresh.');
})().catch(error=>{console.error(error);process.exitCode=1;});
