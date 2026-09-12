"""The production translation functions preserve native-option indentation."""
import json
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path
import app

class HierarchyOptions(HTMLParser):
    def __init__(self):super().__init__();self.options=[];self.current=None
    def handle_starttag(self,tag,attrs):
        if tag=='option':
            attributes=dict(attrs)
            if 'data-hierarchy-depth' in attributes:
                self.current={'attributes':attributes,'text':''};self.options.append(self.current)
    def handle_data(self,data):
        if self.current is not None:self.current['text']+=data
    def handle_endtag(self,tag):
        if tag=='option':self.current=None

class LanguageIndentTests(unittest.TestCase):
    def test_all_scope_options_keep_indent_after_language_changes(self):
        client=app.app.test_client();options=[]
        for path in ('/','/summary'):
            response=client.get(path);self.assertEqual(response.status_code,200)
            parser=HierarchyOptions();parser.feed(response.get_data(as_text=True));self.assertTrue(parser.options)
            options.extend(parser.options)
        script=r"""
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync('static/i18n.js','utf8'),catalog=JSON.parse(fs.readFileSync('static/i18n-translations.json','utf8'));
const entries=JSON.parse(fs.readFileSync(0,'utf8'));
const names=['normalize','interpolate','escapeRegExp','prepareTranslationTemplates','findTranslation','translate','textSource','translateTextNode'];
function definition(name){const start=source.indexOf('    function '+name+'(');assert(start>=0,name);const end=source.indexOf('\n    function ',start+1);return source.slice(start,end);}
const engine=new Function('translations',`
const DEFAULT_LANGUAGE='en';let currentLanguage='en';let translationTemplates=[];const textSources=new WeakMap();const Node={TEXT_NODE:3};const isProtected=()=>false;
${names.map(definition).join('\n')}
prepareTranslationTemplates();return (node,language)=>{currentLanguage=language;translateTextNode(node);};
`)(catalog);
const nodes=entries.map(entry=>({nodeType:3,nodeValue:entry.text,original:entry.text,depth:Number(entry.attributes['data-hierarchy-depth'])}));
for(const locale of ['zh-CN','nl','ko','zh-TW','en'])for(const node of nodes){
 engine(node,locale);const indent='\u00a0\u00a0'.repeat(node.depth);assert(node.nodeValue.startsWith(indent));
 assert.equal((node.nodeValue.match(/^\s*/)||[''])[0],indent);
 const label=node.original.trim();const expected=locale==='en'?label:((catalog[label]||{})[locale]||label);
 assert.equal(node.nodeValue,indent+expected);
}
console.log(JSON.stringify({options:nodes.length,language_changes:5,indent_preserved:true}));
"""
        result=subprocess.run(['node','-e',script],input=json.dumps(options),cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
