"""Retain native XLSX styles/objects while installing Artifact Tool output values."""
from __future__ import annotations
import copy,gzip,hashlib,io,json,re,subprocess,sys,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from openpyxl import load_workbook
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main';Q=lambda n:'{'+NS+'}'+n
ET.register_namespace('',NS)
def digest(raw):return hashlib.sha256(raw).hexdigest()
def values(raw):
 b=load_workbook(io.BytesIO(raw),read_only=True,data_only=False)
 try:return {s.title:[list(r) for r in s.values] for s in b.worksheets}
 finally:b.close()
def col(i):
 out=''
 while i:i,n=divmod(i-1,26);out=chr(65+n)+out
 return out

def preserve(original,authored,item):
 oldv,newv=values(original),values(authored);first=next(iter(oldv));expected=[item['columns'],*[[r.get(k) for k in item['columns']] for r in item['rows']]]
 # Imported used ranges may retain blank trailing rows; compare only populated output.
 assert newv[first][:len(expected)]==expected
 oldby={r[0]:r for r in oldv[first][1:] if r[0] is not None};newby={r[0]:r for r in expected[1:]}
 for name,row in oldby.items():
  if name not in item['affectedExposures']:assert newby.get(name)==row,('unrelated result changed',name)
 with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(authored)) as new:
  original_xml=old.read('xl/worksheets/sheet1.xml').decode();sheet=ET.fromstring(original_xml);oldrows=list(sheet.find(Q('sheetData')));templates={oldv[first][i][0]:r for i,r in enumerate(oldrows) if i>0 and i<len(oldv[first])}
  fresh=ET.fromstring(new.read('xl/worksheets/sheet1.xml'));freshrows={int(r.get('r')):r for r in fresh.find(Q('sheetData'))}
  strings=[''.join(r.itertext()) for r in ET.fromstring(new.read('xl/sharedStrings.xml'))] if 'xl/sharedStrings.xml' in new.namelist() else []
  data=ET.Element(Q('sheetData'));data.append(copy.deepcopy(oldrows[0]))
  for index,wanted in enumerate(expected[1:],2):
   template=templates.get(wanted[0],oldrows[-1]);row=copy.deepcopy(template);row.set('r',str(index));freshcells={re.sub(r'\d+$','',c.get('r')):c for c in freshrows[index]};oldcells={re.sub(r'\d+$','',c.get('r')):c for c in row}
   for cell in list(row):row.remove(cell)
   for cindex in range(1,len(item['columns'])+1):
    letter=col(cindex);cell=copy.deepcopy(oldcells.get(letter,ET.Element(Q('c'))));cell.set('r',letter+str(index))
    if cell.find(Q('f')) is not None:raise ValueError('Unexpected formula in computed summary table')
    for child in list(cell):
     if child.tag in {Q('v'),Q('is'),Q('f')}:cell.remove(child)
    cell.attrib.pop('t',None);source=freshcells.get(letter)
    if source is not None:
     typ=source.get('t')
     if typ=='s':cell.set('t','inlineStr');ET.SubElement(ET.SubElement(cell,Q('is')),Q('t')).text=strings[int(source.find(Q('v')).text)]
     else:
      if typ:cell.set('t',typ)
      for child in source:
       if child.tag in {Q('v'),Q('is'),Q('f')}:cell.append(copy.deepcopy(child))
    row.append(cell)
   data.append(row)
  replacement=ET.tostring(data,encoding='unicode');xml=re.sub(r'<(?:[A-Za-z0-9_]+:)?sheetData\b[^>]*>.*?</(?:[A-Za-z0-9_]+:)?sheetData>',lambda _:replacement,original_xml,count=1,flags=re.S)
  extent='A1:'+col(len(item['columns']))+str(len(expected))
  xml=re.sub(r'(<dimension\b[^>]*\bref=")[^"]+("[^>]*/>)',lambda m:m[1]+extent+m[2],xml,count=1)
  xml=re.sub(r'(<autoFilter\b[^>]*\bref=")[^"]+("[^>]*>)',lambda m:m[1]+extent+m[2],xml,count=1)
  book=old.read('xl/workbook.xml').decode();filter_range="'"+first.replace("'","''")+"'!$A$1:$"+col(len(item['columns']))+'$'+str(len(expected))
  book,n=re.subn(r'(<definedName\b(?=[^>]*\bname="_xlnm\._FilterDatabase")(?=[^>]*\blocalSheetId="0")[^>]*>)[^<]+(</definedName>)',lambda m:m[1]+filter_range+m[2],book)
  parts={'xl/worksheets/sheet1.xml':xml.encode()}
  if n:parts['xl/workbook.xml']=book.encode()
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w') as z:
   for info in old.infolist():z.writestr(copy.copy(info),parts.get(info.filename,old.read(info.filename)))
  final=buf.getvalue()
  with zipfile.ZipFile(io.BytesIO(final)) as z:assert all(old.read(name)==z.read(name) for name in old.namelist() if name not in parts)
 finalv=values(final);assert finalv[first]==expected;assert {k:v for k,v in finalv.items() if k!=first}=={k:v for k,v in oldv.items() if k!=first}
 return final,{'unchanged_native_parts':len(old.namelist())-len(parts),'changed_parts':list(parts),'filter_range':filter_range if n else None,'dimension':extent,'other_sheets_unchanged':True,'unrelated_result_rows_unchanged':True}

def main(root):
 out=root/'data/multicancer_review_2026-09-29';receipts=[]
 for item in json.loads((out/'workbook_payload.json').read_text()):
  target=Path(item['outputPath']);rel=target.relative_to(root);original=subprocess.check_output(['git','show','HEAD:'+rel.as_posix()],cwd=root);authored=target.read_bytes();final,receipt=preserve(original,authored,item)
  before=out/'workbook_originals'/rel.with_suffix('.xlsx.gz');before.parent.mkdir(parents=True,exist_ok=True);before.write_bytes(gzip.compress(original,mtime=0))
  target.write_bytes(final);receipts.append({'path':rel.as_posix(),'before_sha256':digest(original),'authored_sha256':digest(authored),'after_sha256':digest(final),**receipt})
 (out/'workbook_preservation.json').write_text(json.dumps(receipts,indent=2)+'\n');print('Native XLSX preservation verified',len(receipts))
if __name__=='__main__':main(Path(__file__).resolve().parents[1])
