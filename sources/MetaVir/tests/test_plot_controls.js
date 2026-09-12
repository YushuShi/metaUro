/* Plot controls follow the displayed table and PNG, including empty updates. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const observers = new Map(), listeners = {}, downloads = [], draws = [], sizes = [];
const attrs = {src:'data:image/png;base64,BAUJAT'};
const image = {complete:true,naturalWidth:1120,naturalHeight:770,classList:{contains(){return false;}},getAttribute:k=>attrs[k]};
const buttonAttrs = {}, button = {setAttribute(k,v){buttonAttrs[k]=v;},getAttribute:k=>buttonAttrs[k],removeAttribute(k){delete buttonAttrs[k];delete this[k];},addEventListener(t,fn){listeners[t]=fn;}};
let significant = false;
const table = {querySelector(){return significant ? {} : null;}};
const note = {hidden:false};
const nodes = {'baujat-plot':image,'download-baujat-png':button,'loo-tbody':table,disease:{value:'Breast cancer'},exposure:{value:'Vitamin A'}};
const document = {readyState:'complete',getElementById:id=>nodes[id]||null,querySelector:()=>note,
 createElement(tag){
  if(tag==='canvas')return {getContext(){return {fillRect(){},drawImage(img){draws.push(img);}};},toBlob(cb,type){sizes.push([this.width,this.height,type]);cb({type});}};
  if(tag==='a')return {click(){downloads.push(this.download);}};
 }};
const sandbox = {document,MutationObserver:class{constructor(cb){this.cb=cb;}observe(node){observers.set(node,this.cb);}},URL:{createObjectURL(){return 'blob:plot';},revokeObjectURL(){}},setTimeout(){}};
(async()=>{
 vm.createContext(sandbox);
 vm.runInContext(fs.readFileSync(path.join(root,'static/plot_controls.js'),'utf8'),sandbox);
 assert.equal(note.hidden,true);
 significant=true;observers.get(table)();assert.equal(note.hidden,false);
 significant=false;observers.get(table)();assert.equal(note.hidden,true);
 assert.equal(button.getAttribute('aria-disabled'),'false');
 await listeners.click({preventDefault(){}});
 assert.equal(draws[0],image);
 assert.deepEqual(sizes[0],[1120,770,'image/png']);
 assert.deepEqual(downloads,['breast_cancer-vitamin_a-baujat.png']);
 delete attrs.src;observers.get(image)();
 assert.equal(button.getAttribute('aria-disabled'),'true');assert.equal(button.href,undefined);
 await listeners.click({preventDefault(){}});assert.equal(downloads.length,1);
 console.log(path.basename(root)+': conditional red-estimate note and Baujat PNG download passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
