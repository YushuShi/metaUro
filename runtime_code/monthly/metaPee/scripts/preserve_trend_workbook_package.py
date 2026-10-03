"""Transfer verified Artifact Tool values into the original native OOXML package."""
from pathlib import Path
from xml.etree import ElementTree as ET
import copy,gzip,hashlib,io,json,subprocess,sys,zipfile
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main';TAG='{'+NS+'}';ET.register_namespace('',NS)
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
def digest(value):return hashlib.sha256(value).hexdigest()
def column(index):
 out='';index+=1
 while index:index,r=divmod(index-1,26);out=chr(65+r)+out
 return out
def sheets(archive):
 book=ET.fromstring(archive.read('xl/workbook.xml'));rels=ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
 paths={r.get('Id'):r.get('Target') for r in rels}
 return {s.get('name'):(paths[s.get('{'+REL+'}id')].lstrip('/') if paths[s.get('{'+REL+'}id')].startswith('/') else 'xl/'+paths[s.get('{'+REL+'}id')]) for s in book.find(TAG+'sheets')}
def replace_values(original,fresh,strings,addresses):
 rows={int(row.get('r')):row for row in original.find(TAG+'sheetData')};newcells={c.get('r'):c for row in fresh.find(TAG+'sheetData') for c in row};count=0
 for address in addresses:
  number=int(''.join(x for x in address if x.isdigit()));row=rows.get(number)
  if row is None:raise ValueError('Unexpected new worksheet row '+address)
  cell=next((c for c in row if c.get('r')==address),None);new=newcells.get(address)
  if cell is None:
   if new is None:continue
   cell=ET.SubElement(row,TAG+'c',{'r':address})
  if cell.find(TAG+'f') is not None:raise ValueError('Unexpected formula in computed output '+address)
  previous=ET.tostring(cell)
  for child in list(cell):
   if child.tag in {TAG+'v',TAG+'is',TAG+'f'}:cell.remove(child)
  cell.attrib.pop('t',None)
  if new is not None:
   typ=new.get('t')
   if typ=='s':
    value=new.find(TAG+'v');text=strings[int(value.text)] if value is not None else ''
    cell.set('t','inlineStr');inline=ET.SubElement(cell,TAG+'is');ET.SubElement(inline,TAG+'t').text=text
   else:
    if typ:cell.set('t',typ)
    for child in new:
     if child.tag in {TAG+'v',TAG+'is',TAG+'f'}:cell.append(copy.deepcopy(child))
  if ET.tostring(cell)!=previous:count+=1
 return count

def preserve(root,payload_path):
 data=json.loads(payload_path.read_text());audit=[];out=payload_path.parent
 for item in data:
  target=Path(item['outputPath']);relative=target.relative_to(root);authored=target.read_bytes()
  original=subprocess.check_output(['git','show','HEAD:'+relative.as_posix()],cwd=root)
  preimage=out/'workbook_originals'/relative.with_suffix(relative.suffix+'.gz');preimage.parent.mkdir(parents=True,exist_ok=True)
  if not preimage.exists():preimage.write_bytes(gzip.compress(original,mtime=0))
  with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(authored)) as generated:
   old_sheets=sheets(old);new_sheets=sheets(generated);strings=[]
   if 'xl/sharedStrings.xml' in generated.namelist():strings=[''.join(x.itertext()) for x in ET.fromstring(generated.read('xl/sharedStrings.xml')).findall(TAG+'si')]
   first=next(iter(old_sheets));parts={};source=ET.fromstring(old.read(old_sheets[first]));fresh=ET.fromstring(generated.read(new_sheets[first]))
   addresses=[column(col)+str(row) for row in range(1,max(item['previousRows']+1,len(item['rows'])+1)+1) for col in range(len(item['columns']))]
   count=replace_values(source,fresh,strings,addresses);parts[old_sheets[first]]=ET.tostring(source,encoding='utf-8',xml_declaration=True)
   name=item.get('analysisMetadataSheet')
   if name and item.get('analysisInputSha256'):
    source=ET.fromstring(old.read(old_sheets[name]));fresh=ET.fromstring(generated.read(new_sheets[name]))
    # All metadata values are Artifact Tool-authored copies of the original,
    # with only intended input SHA cells updated. Native layout remains exact.
    addresses=[c.get('r') for row in source.find(TAG+'sheetData') for c in row if c.get('r').startswith('B')]
    count+=replace_values(source,fresh,strings,addresses);parts[old_sheets[name]]=ET.tostring(source,encoding='utf-8',xml_declaration=True)
   buf=io.BytesIO()
   with zipfile.ZipFile(buf,'w') as archive:
    for info in old.infolist():archive.writestr(copy.copy(info),parts.get(info.filename,old.read(info.filename)))
   target.write_bytes(buf.getvalue())
   with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as verified:
    assert all(old.read(name)==verified.read(name) for name in old.namelist() if name not in parts)
   audit.append({'file':relative.as_posix(),'original_sha256':digest(original),'artifact_authored_sha256':digest(authored),'preserved_export_sha256':digest(buf.getvalue()),'source_before':str(preimage.relative_to(root)),'changed_cell_xml':count,'all_unrelated_package_parts_byte_identical':True})
 # Validate the actual final numeric cells independently after package transfer.
 from openpyxl import load_workbook
 for item in data:
  book=load_workbook(item['outputPath'],read_only=True,data_only=True);sheet=book.worksheets[0]
  expected=[item['columns'],*[ [r.get(k) for k in item['columns']] for r in item['rows'] ]]
  values=list(sheet.iter_rows(min_row=1,max_row=len(expected),max_col=len(item['columns']),values_only=True))
  for r,(actual,expectedrow) in enumerate(zip(values,expected),1):
   for c,(actual,wanted) in enumerate(zip(actual,expectedrow),1):
    assert actual==wanted,(item['filename'],r,c,actual,wanted)
  book.close()
 (out/'workbook_package_preservation.json').write_text(json.dumps(audit,indent=2)+'\n')
 print('Native workbook packages and authored values verified:',len(audit),flush=True)
if __name__=='__main__':preserve(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
