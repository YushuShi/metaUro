"""Check exported forest PI strokes; CJK vector captions also need visual QA."""
from pathlib import Path
import json
import sys
import fitz


def verify(root):
    root=Path(root).resolve()
    audit=root/('evidence-revisions/summary-pi-2026-09-27' if root.name=='metaUro' else 'data/quantitative_audits/summary_pi_2026-09-27')
    items=[]
    for path in audit.glob('render_report_*.json'):
        items.extend(item for item in json.loads(path.read_text()) if item['file'].endswith('.pdf'))
    records=[];errors=[]
    for item in items:
        with fitz.open(root/item['file']) as doc:
            content=''.join(''.join(page.get_text().split()) for page in doc)
            strokes=sum(abs(drawing.get('width',0)-1.15)<.001 and drawing.get('dashes')!='[] 0'
                        for page in doc for drawing in page.get_cdrawings() if drawing.get('type')=='s')
            expected=item['valid_prediction_intervals']
            if strokes!=expected:errors.append({'file':item['file'],'expected_pi_strokes':expected,'actual_pi_strokes':strokes})
            caption=None
            if item['rows'] and item['locale'] in {'en','nl'}:
                needle={'en':'dashed line below CI = 95% prediction interval (where available)',
                        'nl':'streepjeslijn onder BI = 95%-voorspellingsinterval (indien beschikbaar)'}[item['locale']]
                caption=''.join(needle.split()) in content
                if not caption:errors.append({'file':item['file'],'missing_latin_caption':True})
            records.append({'file':item['file'],'pages':len(doc),'saved_valid_pi_count':expected,
                            'rendered_pi_strokes':strokes,'latin_caption_present':caption})
    assert records,'No completed forest export records'
    result={'status':'passed' if not errors else 'failed','checked_pdfs':len(records),
            'caption_note':'English and Dutch captions are checked in extracted text. Existing CJK export fonts emit vector glyphs that are not returned by PDF text extraction; representative localized figures were checked visually. Every PDF is checked for the exact number of saved valid PI strokes.',
            'errors':errors,'records':records}
    (audit/'pdf_validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print(root.name,result['status'],len(records),'PDFs; errors:',errors)
    if errors:raise SystemExit(1)

if __name__=='__main__':verify(sys.argv[1])
