import fs from 'node:fs/promises';
import path from 'node:path';
const [input,output]=process.argv.slice(2);
if(!input||!output)throw Error('Usage: verify_multicancer_workbooks.mjs INPUT_JSON OUTPUT_JSON');
const previewDirectory=path.join(path.dirname(input),'previews');
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';
const payload=JSON.parse(await fs.readFile(input,'utf8'));
const checks=[];
for(const item of payload){
 const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(item.outputPath));
 const sheet=wb.worksheets.getItemAt(0);
 const expected=[item.columns,...item.rows.map(row=>item.columns.map(k=>row[k]??null))];
 const actual=sheet.getRangeByIndexes(0,0,expected.length,item.columns.length).values;
 for(let r=0;r<expected.length;r++)for(let c=0;c<item.columns.length;c++){
  if((actual[r]?.[c]??null)!==expected[r][c])throw Error(`${item.filename} ${r},${c}`);
  if(typeof actual[r]?.[c]==='string' && /^#(REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!|SPILL!|CALC!)$/.test(actual[r][c]))throw Error('Spreadsheet error');
 }
 const affected=item.rows.map((r,i)=>item.affectedExposures.includes(r.Exposure)?i+2:null).filter(Boolean);
 const first=Math.max(1,Math.min(...affected)-1),last=Math.min(expected.length,first+12);
 const png=await wb.render({sheetName:sheet.name,range:`A${first}:K${last}`,scale:1.3,format:'png'});
 const name=`${item.mode}_${item.filename.replace('.xlsx','.png')}`;
 await fs.mkdir(previewDirectory,{recursive:true});
 await fs.writeFile(path.join(previewDirectory,name),new Uint8Array(await png.arrayBuffer()));
 checks.push({source:item.source,mode:item.mode,filename:item.filename,exact_payload_cells:expected.length*item.columns.length,rows:item.rows.length,final_import_verified:true,error_count:0,preview:name});
 console.log(name,'verified');
}
await fs.writeFile(output,JSON.stringify(checks,null,2)+'\n');
