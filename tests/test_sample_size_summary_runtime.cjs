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
 append(...nodes){for(const node of nodes)node.parent=this;this.children.push(...nodes);}appendChild(node){this.append(node);}
 cloneNode(deep){const node=new Node(this.tagName,this.value);node.attributes={...this.attributes};node._text=this._text;if(deep)node.append(...this.children.map(child=>child.cloneNode(true)));return node;}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);}
 click(){return this.dispatch('click');}showModal(){this.open=true;}close(){this.open=false;this.dispatch('close');}
 replaceChildren(...nodes){this.children=[...nodes];this._text='';if(this.tagName==='select'&&!nodes.some(x=>x.value===this.value))this.value=nodes[0]?.value||'';}
 setAttribute(name,value){this.attributes[name]=String(value);}getAttribute(name){return this.attributes[name];}
 addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}async dispatch(name){for(const fn of this.listeners[name]||[])await fn({target:this,preventDefault(){}});await flush();}
 setCustomValidity(message){this.validationMessage=String(message);}
 reportValidity(){return this.valid!==false&&!this.validationMessage;}
}
function descendants(node,predicate){return [node,...node.children.flatMap(x=>descendants(x,predicate))].filter(predicate);}
async function runtime({search='',state={keep_latest_cohort:true},repository='MetaGut',source='metaPee',storage={},fetcher}={}){
 const nodes={};const named={'sample-summary-disease':'disease','sample-baseline':'baseline_percent','sample-control-exposure':'control_exposure_percent','sample-allocation':'allocation_ratio','sample-alpha':'alpha','sample-power':'power','sample-sidedness':'sidedness','sample-maximum-size':'max_sample_size'};
 for(const [id,name] of Object.entries(named)){nodes[id]=new Node(id==='sample-summary-disease'?'select':'input');nodes[id].name=name;}
 for(const id of ['sample-summary-form','sample-summary-status','sample-summary-plots','sample-baseline-note','sample-summary-submit','sample-summary-context'])nodes[id]=new Node();
 Object.assign(nodes['sample-control-exposure'],{value:'50'});nodes['sample-allocation'].value='1';nodes['sample-alpha'].value='.05';nodes['sample-power'].value='.8';nodes['sample-sidedness'].value='two';nodes['sample-maximum-size'].value='20000';
 const body=new Node('body');body.dataset={sampleSizeRepository:repository,sampleSizeSource:source};
 const downloads=[],blobs=[];
 const document={listeners:{},body,getElementById:id=>nodes[id]||null,createElement(tag){const node=new Node(tag);if(tag==='a')node.addEventListener('click',()=>{if(node.download)downloads.push({filename:node.download,href:node.href});});return node;},createElementNS:(_ns,tag)=>new Node(tag),addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}};
 const window={listeners:{},AnalysisSettings:{ready:Promise.resolve(),get:()=>state},addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}};
 if(state===null)delete window.AnalysisSettings;
 const location={href:'https://example.test/sample-size-summary'+search,search};const history={replaceState(_state,_title,next){location.href=String(next);location.search=new URL(next).search;}};
 const requests=[];let nextResponse=null;
 const catalog={diseases:[{value:'A',label:'Disease A',source:'metaPee',baseline_percent:1},{value:'B',label:'Disease B',source:'MetaVir',baseline_percent:2}],defaults:{alpha:.05,power:.8,sidedness:'two',allocation_ratio:1,control_exposure_percent:50}};
 const defaultData={baseline_percent:1,panels:[{measurement:'dietary',design:'retrospective',points:[{exposure:'Soy',effect_size:.8,total_n:2000,study_count:2}]},{measurement:'all',design:'prospective',points:[{exposure:'Coffee',effect_size:.7,total_n:1000,study_count:2},{exposure:'One study',effect_size:.9,total_n:200,study_count:1},{exposure:'Invalid N',effect_size:.9,total_n:Infinity,study_count:3},{exposure:'Missing ES',effect_size:null,total_n:20,study_count:3}]}]};
 const escape=value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
 const serialize=node=>`<${node.tagName}${Object.entries(node.attributes).map(([key,value])=>` ${key}="${escape(value)}"`).join('')}>${escape(node._text)}${node.children.map(serialize).join('')}</${node.tagName}>`;
 const context={document,window,Blob,XMLSerializer:class{serializeToString(node){return serialize(node);}},URL:class extends URL{static createObjectURL(blob){blobs.push(blob);return 'blob:sample-plot';}static revokeObjectURL(){}},URLSearchParams,location,history,AbortController,console,setTimeout,clearTimeout,localStorage:{getItem(key){return storage[key] ? JSON.stringify(storage[key]) : null;}},FormData:class{constructor(form){}*[Symbol.iterator](){for(const [id,name] of Object.entries(named))yield[name,nodes[id].value];}},fetch:async(url,options)=>{const query=new URL(url,location.href);requests.push(query);if(fetcher)return fetcher(query,options,{catalog,defaultData});if(nextResponse){const result=nextResponse;nextResponse=null;return result;}return{ok:true,json:async()=>query.pathname.endsWith('/catalog')?catalog:defaultData};}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(sourcePath,'utf8'),context);
 for(const fn of document.listeners.DOMContentLoaded||[])fn();await flush();
 return{nodes,requests,window,state,location,catalog,defaultData,document,downloads,blobs,setNext(value){nextResponse=value;},async submit(){await nodes['sample-summary-form'].dispatch('submit');},async changeDisease(value){nodes['sample-summary-disease'].value=value;await nodes['sample-summary-disease'].dispatch('change');},async editBaseline(value){nodes['sample-baseline'].value=value;await nodes['sample-baseline'].dispatch('input');}};
}
(async()=>{
 const ui=await runtime();assert.equal(ui.nodes['sample-baseline'].value,'1');assert.equal(ui.nodes['sample-summary-plots'].children.length,4);assert(!ui.requests.at(-1).searchParams.has('baseline_percent'));
 const titles=ui.nodes['sample-summary-plots'].children.map(card=>card.children[0].textContent);assert.deepEqual(titles,['All exposure measurements · Prospective','All exposure measurements · Retrospective','Dietary only · Prospective','Dietary only · Retrospective']);
 const text=ui.nodes['sample-summary-plots'].textContent;assert(text.includes('Coffee'));for(const excluded of ['One study','Invalid N','Missing ES'])assert(!text.includes(excluded));
 await ui.editBaseline('7');await ui.submit();assert.equal(ui.requests.at(-1).searchParams.get('baseline_percent'),'7');
 await ui.changeDisease('B');assert.equal(ui.nodes['sample-baseline'].value,'2');assert(!ui.requests.at(-1).searchParams.has('baseline_percent'));assert.equal(ui.requests.at(-1).searchParams.get('disease'),'B');
 for(const [id,value] of Object.entries({'sample-control-exposure':'35','sample-allocation':'2','sample-alpha':'.01','sample-power':'.9','sample-sidedness':'one'}))ui.nodes[id].value=value;
 await ui.submit();for(const [key,value] of Object.entries({control_exposure_percent:'35',allocation_ratio:'2',alpha:'.01',power:'.9',sidedness:'one'}))assert.equal(ui.requests.at(-1).searchParams.get(key),value);
 ui.setNext({ok:false,status:400,json:async()=>({error:'Validation rejected'})});await ui.submit();assert.equal(ui.nodes['sample-summary-status'].textContent,'Validation rejected');assert.equal(ui.nodes['sample-summary-plots'].children.length,0);assert.equal(ui.nodes['sample-summary-submit'].disabled,false);assert.equal(ui.nodes['sample-summary-plots'].attributes['aria-busy'],'false');
 console.log('PASS baseline editing/reset, 4-panel order, >=2-study/finite filtering, controls, API errors and busy reset.');
 const explicit=await runtime({search:'?keep_latest_cohort=false&age=70',state:{keep_latest_cohort:true,age:60}});
 for(const url of explicit.requests){assert.equal(url.searchParams.get('keep_latest_cohort'),'false');assert.equal(url.searchParams.get('age'),'70');}
 let pending=[];const racing=await runtime({fetcher:async(url,options,{catalog,defaultData})=>url.pathname.endsWith('/catalog')?{ok:true,json:async()=>catalog}:new Promise(resolve=>pending.push({resolve,defaultData,options}))});
 // Make a second update while the initial one is still pending.
 await racing.changeDisease('B');assert.equal(pending.length,2);racing.nodes['sample-alpha'].value='.01';pending[1].resolve({ok:true,json:async()=>pending[1].defaultData});await flush();
 assert(racing.nodes['sample-summary-context'].textContent.includes('α = 0.05'),'Context must describe sent settings, not edits made while the request was pending');
 const before=racing.nodes['sample-summary-status'].textContent;pending[0].resolve({ok:false,status:503,json:async()=>({error:'Stale failure'})});await flush();
 assert.equal(racing.nodes['sample-summary-status'].textContent,before,'A stale failure must not replace newer success');assert.equal(racing.nodes['sample-summary-plots'].children.length,4);
 const dense=await runtime({fetcher:async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:{panels:[{measurement:'all',design:'prospective',points:Array.from({length:24},(_,i)=>({exposure:'Long exposure name '+i,effect_size:.8,total_n:1000,study_count:3}))}]}})});
 const labels=descendants(dense.nodes['sample-summary-plots'],node=>node.attributes.class==='sample-point-label').map(node=>({x:Number(node.attributes.x),y:Number(node.attributes.y)-12,w:Math.max(...node.children.map(line=>line.textContent.length))*7+10,h:node.children.length*15+4}));
 assert.equal(labels.length,24);
 const denseGroups=descendants(dense.nodes['sample-summary-plots'],node=>node.tagName==='g'&&node.attributes.tabindex==='0');
 for(const group of denseGroups){
  assert(!group.children.some(node=>node.tagName==='line'),'Exposure labels must not have connector lines');
  const dot=group.children.find(node=>node.tagName==='circle'),label=group.children.find(node=>node.tagName==='text');
  assert.equal(Number(label.attributes.x)-Number(dot.attributes.cx),13,'Every label must stay directly beside its own dot');
  const height=label.children.length*15+4;
  assert.equal(Number(label.attributes.y)-12+height/2,Number(dot.attributes.cy),'Label block must stay vertically centered on its dot');
 }
 assert.equal(new Set(labels.map(label=>`${label.x},${label.y}`)).size,1,'Overlapping labels are allowed instead of moving names away from coincident dots');
 const continuous=await runtime({fetcher:async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:{panels:[{measurement:'all',design:'prospective',effect_label:'Mean difference',points:[{exposure:'Negative mean difference',effect_size:-2,total_n:100,study_count:2},{exposure:'Another negative mean difference',effect_size:-1,total_n:200,study_count:3}]}]}})});
 const circles=descendants(continuous.nodes['sample-summary-plots'],node=>node.tagName==='circle');assert.equal(circles.length,2);for(const dot of circles){assert(Number.isFinite(Number(dot.attributes.cx)));assert(Number(dot.attributes.cx)>=82&&Number(dot.attributes.cx)<=650,'Negative mean differences must stay inside the plot bounds');}
 assert(continuous.nodes['sample-summary-plots'].textContent.includes('Mean difference'));
 // Ratio effects use natural-log coordinates, while sample size stays log10.
 const logarithmic=await runtime({fetcher:async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:{panels:[{measurement:'all',design:'prospective',points:[
  {exposure:'Protective',effect_size:.5,total_n:1000,study_count:2,dot_color:'#2475C0',label_color:'#8DC9E8'},
  {exposure:'Reference',effect_size:1,total_n:1000,study_count:2,color:'#123456'},
  {exposure:'Harmful',effect_size:4,total_n:1000,study_count:2,dot_color:'#C9354C',label_color:'#EEA4BC'},
  {exposure:'Invalid zero ratio',effect_size:0,total_n:1000,study_count:2},
  {exposure:'Invalid negative ratio',effect_size:-1,total_n:1000,study_count:2}
 ]},{measurement:'all',design:'retrospective',points:[{exposure:'Odds',effect_size:.5,total_n:1000,study_count:2,color:'#000080'}]}]}})});
 const ratioCard=logarithmic.nodes['sample-summary-plots'].children[0];
 const ratioDots=descendants(ratioCard,node=>node.tagName==='circle');assert.equal(ratioDots.length,3);
 const positions=ratioDots.map(dot=>Number(dot.attributes.cx));
 assert(Math.abs((positions[2]-positions[1])/(positions[1]-positions[0])-2)<1e-10,'Doubling log-effect distance must double horizontal distance');
 const nullLine=descendants(ratioCard,node=>node.attributes.class==='sample-null-line')[0];
 assert(Math.abs(Number(nullLine.attributes.x1)-positions[1])<1e-10,'RR=1 must lie on the log-effect zero line');
 assert(ratioCard.textContent.includes('ln(RR)'));
 assert(ratioCard.textContent.includes('log₁₀(required total sample size)'));
 assert(logarithmic.nodes['sample-summary-plots'].children[1].textContent.includes('ln(OR)'));
 const tableEffects=card=>Object.fromEntries(descendants(card,node=>node.tagName==='tbody')[0].children.map(row=>[row.children[1].textContent,row.children[2].textContent]));
 const effectHeader=card=>descendants(card,node=>node.attributes['data-sort-field']==='effect_size')[0];
 assert(effectHeader(ratioCard).textContent.startsWith('Effect size ln(RR)'));
 assert.deepEqual(tableEffects(ratioCard),{Harmful:'1.386',Protective:'-0.693',Reference:'0.000'});
 const oddsCard=logarithmic.nodes['sample-summary-plots'].children[1];
 assert(effectHeader(oddsCard).textContent.startsWith('Effect size ln(OR)'));
 assert.deepEqual(tableEffects(oddsCard),{Odds:'-0.693'});
 const continuousCard=continuous.nodes['sample-summary-plots'].children[0];
 assert(!effectHeader(continuousCard).textContent.includes('ln('));
 assert.deepEqual(tableEffects(continuousCard),{'Negative mean difference':'-2.000','Another negative mean difference':'-1.000'});
 const colored=descendants(ratioCard,node=>node.tagName==='g'&&node.attributes.tabindex==='0');
 assert(colored[0].attributes.style.includes('--sample-dot-color:#2475C0'));
 assert(colored[0].attributes.style.includes('--sample-label-color:#8DC9E8'));
 assert(colored[1].attributes.style.includes('--sample-dot-color:#123456'));
 assert(colored[2].attributes.style.includes('--sample-label-color:#EEA4BC'));
 assert(colored[0].textContent.includes('1000')||colored[0].textContent.includes('1,000'),'Sample size labels must keep the untransformed total');
 assert(!continuous.nodes['sample-summary-plots'].textContent.includes('log(Mean difference)'),'Continuous effects must not be logarithmically transformed');
 console.log('PASS natural-log RR/OR coordinates, no-effect zero, unchanged log10 sample sizes, and Summary dot/label colors.');
 const trials=await runtime({fetcher:async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:{panels:[{measurement:'all',design:'prospective',points:[
  {exposure:'A long randomized exposure name with wrapped lines',effect_size:.8,total_n:1000,study_count:3,rct_marker:'*',rct_count:2},
  {exposure:'Discordant',effect_size:.7,total_n:2000,study_count:3,rct_marker:'#',rct_count:1},
  {exposure:'No trial',effect_size:.6,total_n:3000,study_count:3,rct_marker:''},
  {exposure:'Invalid marker',effect_size:.9,total_n:4000,study_count:3,rct_marker:'<script>'}
 ]}]}})});
 const trialLabels=descendants(trials.nodes['sample-summary-plots'],node=>node.attributes.class==='sample-point-label');
 const marked=trialLabels.filter(label=>label.children.some(line=>line.children.length));assert.equal(marked.length,2);
 for(const label of marked){
  const marker=label.children.at(-2).children[0];assert(['*','#'].includes(marker.textContent));
  assert.equal(marker.attributes['baseline-shift'],'super');
  assert.equal(label.children.at(-1).children.length,0,'The marker belongs to the exposure, not its sample size');
 }
 const tableMarkers=descendants(trials.nodes['sample-summary-plots'],node=>node.tagName==='sup');assert.equal(tableMarkers.length,2);
 assert(tableMarkers.some(marker=>marker.textContent==='#'&&marker.attributes.title.includes('does not share')));
 console.log('PASS superscript trial markers on exposure names and tables, wrapped labels, and unmarked exposures.');
 const ranked=await runtime({fetcher:async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:{panels:[{measurement:'all',design:'prospective',points:[
  {exposure:'Z lower effect',effect_size:.80041,total_n:200,study_count:4,rct_marker:'*',rct_count:1,dot_color:'#123456',label_color:'#654321'},
  {exposure:'A higher effect',effect_size:.80049,total_n:30,study_count:2},
  {exposure:'Middle',effect_size:1.256789,total_n:100,study_count:3}
 ]}]}})});
 let rankedCard=ranked.nodes['sample-summary-plots'].children[0];
 const rows=()=>descendants(rankedCard,node=>node.tagName==='tbody')[0].children;
 const names=()=>rows().map(row=>row.children[1].textContent.replace('*',''));
 const hasClass=(node,name)=>String(node.className||node.attributes.class||'').split(/\s+/).includes(name);
 const sortButtons=()=>descendants(rankedCard,node=>node.tagName==='button'&&hasClass(node,'sample-sort-button'));
 const sortButton=field=>sortButtons().find(node=>(node.dataset.sortField||node.getAttribute('data-sort-field'))===field);
 const indicator=button=>descendants(button,node=>hasClass(node,'sample-sort-indicator'))[0];
 const header=button=>descendants(rankedCard,node=>node.tagName==='th').find(th=>th.children.includes(button));
 const assertSort=(field,direction)=>{
  for(const candidate of ['exposure','effect_size','total_n','study_count']){
   const button=sortButton(candidate);assert(button,`Sortable ${candidate} header must be present`);
   if(candidate===field){
    assert.equal(header(button).getAttribute('aria-sort'),direction==='asc'?'ascending':'descending');
    assert.equal(indicator(button).textContent,direction==='asc'?'▲':'▼');
   }else{
    assert(['none',undefined].includes(header(button).getAttribute('aria-sort')));
    assert.equal(indicator(button).textContent,'▲▼');
   }
  }
 };
 assert.equal(sortButtons().length,4);
 assert.equal(descendants(rankedCard,node=>node.tagName==='select').length,0,'Panel ranking must use clickable headers without dropdowns');
 const rankHeader=descendants(rankedCard,node=>node.tagName==='th')[0];
 assert.equal(rankHeader.textContent,'Rank');assert.equal(descendants(rankHeader,node=>node.tagName==='button').length,0);
 assert.deepEqual(names(),['A higher effect','Middle','Z lower effect'],'Default N sort must be numeric');assertSort('total_n','asc');
 const rankedDetails=descendants(rankedCard,node=>node.tagName==='details')[0];rankedDetails.open=true;
 const requestCount=ranked.requests.length;
 const rankSnapshot=JSON.stringify(ranked.defaultData);
 await sortButton('effect_size').click();assertSort('effect_size','asc');
 assert.deepEqual(names(),['Z lower effect','A higher effect','Middle'],'Effect ranking must use raw unrounded values');
 assert.deepEqual(rows().map(row=>row.children[2].textContent),['-0.223','-0.223','0.229']);
 assert(descendants(rankedCard,node=>node.tagName==='details')[0].open,'Sorting preserves the user’s open table');
 await sortButton('effect_size').click();assertSort('effect_size','desc');assert.deepEqual(names(),['Middle','A higher effect','Z lower effect']);
 await sortButton('study_count').click();assertSort('study_count','asc');assert.deepEqual(names(),['A higher effect','Middle','Z lower effect']);
 await sortButton('study_count').click();assertSort('study_count','desc');assert.deepEqual(names(),['Z lower effect','Middle','A higher effect']);
 await sortButton('exposure').click();assertSort('exposure','asc');assert.deepEqual(names(),['A higher effect','Middle','Z lower effect']);
 await sortButton('exposure').click();assertSort('exposure','desc');assert.deepEqual(names(),['Z lower effect','Middle','A higher effect']);
 await sortButton('total_n').click();assertSort('total_n','asc');assert.deepEqual(names(),['A higher effect','Middle','Z lower effect']);
 await sortButton('total_n').click();assertSort('total_n','desc');assert.deepEqual(names(),['Z lower effect','Middle','A higher effect']);
 assert.equal(ranked.requests.length,requestCount,'Sorting must not recalculate or fetch evidence');
 assert.equal(descendants(rankedCard,node=>node.tagName==='circle').length,3,'Sorting keeps every eligible dot');
 assert.equal(JSON.stringify(ranked.defaultData),rankSnapshot,'Ranking must not mutate API evidence');
 await ranked.submit();rankedCard=ranked.nodes['sample-summary-plots'].children[0];assert.deepEqual(names(),['Z lower effect','Middle','A higher effect'],'Ranking survives recalculation');assertSort('total_n','desc');
 const mainChart=descendants(rankedCard,node=>node.tagName==='svg')[0];
 assert(!mainChart.children.some(node=>node.tagName==='title'),'Blank plot space must not trigger a generic SVG tooltip');
 assert.equal(descendants(mainChart,node=>node.tagName==='title').length,3,'Per-exposure tooltips remain available');
 const chartIndex=rankedCard.children.findIndex(node=>hasClass(node,'sample-chart-scroll'));
 const linksIndex=rankedCard.children.findIndex(node=>descendants(node,child=>child.tagName==='a'&&hasClass(child,'sample-plot-link')).length);
 const tableIndex=rankedCard.children.findIndex(node=>node.tagName==='details');
 assert(chartIndex>=0&&linksIndex>chartIndex&&tableIndex>linksIndex,'Ranked tables belong below the chart and plot links');
 const plotLink=(scope,label)=>descendants(scope,node=>node.tagName==='a'&&hasClass(node,'sample-plot-link')&&node.textContent===label)[0];
 const view=plotLink(rankedCard,'View full-size plot');assert(view);await view.click();assert.equal(ranked.downloads.length,0,'Opening a full-size link is not a file download');
 const dialog=ranked.document.body.children.find(node=>node.tagName==='dialog');assert(dialog.open);
 const exported=descendants(dialog,node=>node.tagName==='svg')[0];assert.equal(exported.attributes.width,'960');assert(Number(exported.attributes.height)>540);
 assert.equal(descendants(exported,node=>node.tagName==='circle').length,3);
 assert(!descendants(exported,node=>node.tagName==='title').some(node=>node.textContent.includes('Each labeled dot is one exposure')),'Full-size exports must not restore the removed generic SVG tooltip');
 assert(exported.textContent.includes('ln(RR)'));assert(exported.textContent.includes('All eligible randomized-trial estimates'));
 await plotLink(dialog,'Download full-size SVG').click();
 assert.equal(ranked.downloads[0].filename,'metagut-a-all-prospective-sample-size.svg');
 const xml=await ranked.blobs[0].text();assert(xml.includes('xmlns="http://www.w3.org/2000/svg"'));assert(xml.includes('<style>'));assert(xml.includes('--sample-dot-color:#123456'));assert(xml.includes('baseline-shift="super"'));assert(xml.includes('Disease A'));assert(xml.includes('Baseline incidence'));assert(xml.includes('Exposures without trials remain included'));
 await descendants(dialog,node=>node.tagName==='button'&&node.textContent==='Close')[0].click();assert(!ranked.document.body.children.includes(dialog));
 ranked.nodes['sample-summary-disease'].value='B';ranked.nodes['sample-summary-context'].textContent='Unsent new settings';ranked.nodes['sample-maximum-size'].value='50';
 await plotLink(rankedCard,'Download full-size SVG').click();assert.equal(ranked.downloads.length,2);
 assert.equal(ranked.downloads[1].filename,'metagut-a-all-prospective-sample-size.svg');const unchangedExport=await ranked.blobs[1].text();assert(unchangedExport.includes('Disease A'));assert(!unchangedExport.includes('Disease B'));assert(!unchangedExport.includes('Unsent new settings'));
 assert.equal((unchangedExport.match(/<circle\b/g)||[]).length,3,'Unsubmitted maximum-size edits must not change a rendered export');
 console.log('PASS sortable numeric/alphabetic headers, arrows and aria-sort, fixed3decimals, persistent sorting, point-only tooltips, links, table placement and snapshot-safe SVG downloads.');
 // The maximum applies to every panel and every representation, including exact boundary values.
 const limitPoints=[
  {exposure:'Below limit',effect_size:.8,total_n:100,study_count:2},
  {exposure:'Inside limit',effect_size:.9,total_n:19999,study_count:3},
  {exposure:'At default limit',effect_size:1.2,total_n:20000,study_count:4},
  {exposure:'Above default limit',effect_size:1.3,total_n:20001,study_count:2},
  {exposure:'At raised limit',effect_size:1.4,total_n:50000,study_count:3},
  {exposure:'Zero N',effect_size:.8,total_n:0,study_count:2},
  {exposure:'Negative N',effect_size:.8,total_n:-10,study_count:2},
  {exposure:'Infinite N',effect_size:.8,total_n:Infinity,study_count:2},
  {exposure:'Single study',effect_size:.8,total_n:10,study_count:1},
  {exposure:'Missing effect',effect_size:null,total_n:10,study_count:3}
 ];
 const limitData={panels:['all','dietary'].flatMap(measurement=>['prospective','retrospective'].map(design=>({measurement,design,points:limitPoints})))};
 const limitOriginal=JSON.stringify(limitData);
 const limitFetcher=async(url,options,{catalog})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?catalog:limitData});
 const capped=await runtime({fetcher:limitFetcher});
 const cappedCards=()=>capped.nodes['sample-summary-plots'].children;
 const tableNames=card=>descendants(card,node=>node.tagName==='tbody')[0]?.children.map(row=>row.children[1].textContent)||[];
 const topTick=chart=>descendants(chart,node=>node.tagName==='text'&&node.attributes.class==='sample-tick'&&node.attributes['text-anchor']==='end').sort((a,b)=>Number(a.attributes.y)-Number(b.attributes.y))[0];
 const assertCap=(limit,expected)=>{
  for(const card of cappedCards()){
   assert.deepEqual(tableNames(card),expected);
   const chart=descendants(card,node=>node.tagName==='svg')[0];
   if(!expected.length){assert(!chart);assert.equal(descendants(card,node=>node.tagName==='a'&&hasClass(node,'sample-plot-link')).length,0);continue;}
   const dots=descendants(chart,node=>node.tagName==='circle');assert.equal(dots.length,expected.length);
   assert.equal(topTick(chart).textContent,Math.log10(limit).toFixed(2),'The top vertical tick must match the selected maximum');
   const boundary=descendants(chart,node=>node.tagName==='g'&&node.attributes.tabindex==='0').find(group=>group.children.find(child=>child.tagName==='title')?.textContent.startsWith(limit===20000?'At default limit:':limit===50000?'At raised limit:':'Below limit:'));
   if(boundary){assert.equal(Number(boundary.children.find(node=>node.tagName==='circle').attributes.cy),40,'An exact-limit point must lie at the unpadded vertical ceiling');}
  }
 };
 assert.equal(capped.nodes['sample-maximum-size'].value,'20000');assert.equal(capped.requests.at(-1).searchParams.get('max_sample_size'),'20000');
 assertCap(20000,['Below limit','Inside limit','At default limit']);
 await plotLink(cappedCards()[0],'View full-size plot').click();
 const capDialog=capped.document.body.children.find(node=>node.tagName==='dialog');const capSvg=descendants(capDialog,node=>node.tagName==='svg')[0];
 assert.equal(descendants(capSvg,node=>node.tagName==='circle').length,3);assert(!capSvg.textContent.includes('Above default limit'));assert.equal(topTick(capSvg).textContent,Math.log10(20000).toFixed(2));
 await plotLink(capDialog,'Download full-size SVG').click();const cappedXml=await capped.blobs[0].text();assert.equal((cappedXml.match(/<circle\b/g)||[]).length,3);assert(!cappedXml.includes('Above default limit'));assert(!cappedXml.includes('At raised limit'));
 await descendants(capDialog,node=>node.tagName==='button'&&node.textContent==='Close')[0].click();
 capped.nodes['sample-maximum-size'].value='50000';await capped.submit();assertCap(50000,['Below limit','Inside limit','At default limit','Above default limit','At raised limit']);
 capped.nodes['sample-maximum-size'].value='100';await capped.submit();assertCap(100,['Below limit']);
 const validCards=[...cappedCards()];const validRequests=capped.requests.length;
 for(const invalid of ['', '0', '-1', '1.5', 'NaN', 'Infinity', '9007199254740992']){
  capped.nodes['sample-maximum-size'].value=invalid;await capped.submit();
  assert.equal(capped.requests.length,validRequests,`Invalid maximum ${JSON.stringify(invalid)} must be rejected before fetching`);
  assert.deepEqual(cappedCards(),validCards,'Invalid input preserves the last successful plots');
 }
 capped.nodes['sample-maximum-size'].value='50';await capped.submit();assertCap(50,[]);
 assert.equal(JSON.stringify(limitData),limitOriginal,'Filtering, sorting and exports must preserve the returned numerical evidence');
 const urlCap=await runtime({search:'?max_sample_size=25000',fetcher:limitFetcher});
 assert.equal(urlCap.nodes['sample-maximum-size'].value,'25000');assert.equal(urlCap.requests.at(-1).searchParams.get('max_sample_size'),'25000');
 assert.deepEqual(tableNames(urlCap.nodes['sample-summary-plots'].children[0]),['Below limit','Inside limit','At default limit','Above default limit']);
 let responsePending;
 const capRace=await runtime({fetcher:async(url,options,{catalog})=>url.pathname.endsWith('/catalog')?{ok:true,json:async()=>catalog}:new Promise(resolve=>{responsePending=resolve;})});
 capRace.nodes['sample-maximum-size'].value='100';responsePending({ok:true,json:async()=>limitData});await flush();
 const raceChart=descendants(capRace.nodes['sample-summary-plots'].children[0],node=>node.tagName==='svg')[0];
 assert.equal(descendants(raceChart,node=>node.tagName==='circle').length,3,'A pending request uses the submitted maximum, not a later unsent edit');
 assert.equal(topTick(raceChart).textContent,Math.log10(20000).toFixed(2));
 await plotLink(capRace.nodes['sample-summary-plots'].children[0],'Download full-size SVG').click();
 const raceXml=await capRace.blobs[0].text();assert.equal((raceXml.match(/<circle\b/g)||[]).length,3);
 console.log('PASS default/custom maximum, inclusive boundary and exact axis ceiling, all-panel/table/export filtering, validation before fetch and submitted-setting snapshots.');
 const uro=await runtime({repository:'MetaUro',state:null,storage:{'metaUro:metaPee':{age:60,sex:'both'},'metaUro:MetaVir':{age:70,sex:'male'}},fetcher:async(url,options,{catalog,defaultData})=>({ok:true,json:async()=>url.pathname.endsWith('/catalog')?{...catalog,diseases:catalog.diseases.map(item=>({...item,baseline_percent:item.value==='B'&&url.searchParams.get('age')==='70'?9:item.baseline_percent}))}:defaultData})});
 await uro.changeDisease('B');assert.equal(uro.nodes['sample-baseline'].value,'9','Changing evidence source must refresh its baseline under that source’s saved settings');assert.equal(uro.requests.at(-1).searchParams.get('age'),'70');assert(!uro.requests.at(-1).searchParams.has('baseline_percent'));
 for(const url of [...ui.requests,...uro.requests].filter(url=>url.pathname.endsWith('/catalog')))assert(!url.searchParams.has('baseline_percent'),'Per-disease overrides must not leak into other catalog baselines');
 console.log('PASS explicit settings, stale requests, continuous effects, adjacent labels without connectors, and source-specific baseline refresh.');
})().catch(error=>{console.error(error);process.exitCode=1;});
