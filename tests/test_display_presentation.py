import json
from pathlib import Path
from bs4 import BeautifulSoup
from display_localization import chinese_display,normalize_catalog
import app
ROOT=Path(__file__).resolve().parents[1]

def test_chinese_replacement_is_phrase_aware_and_display_only():
    for value in ('暴露','暴露因素','营养暴露因素','营养暴露'):
        assert chinese_display(value,'zh-CN')=='营养因子'
    for value in ('暴露','暴露因子','營養暴露因子','營養暴露'):
        assert chinese_display(value,'zh-TW')=='營養因子'
    source={'Exposure':{'zh-CN':'营养暴露因素','zh-TW':'營養暴露因子','en':'Exposure'}}
    result=normalize_catalog(source)
    assert result=={'Exposure':{'zh-CN':'营养因子','zh-TW':'營養因子','en':'Exposure'}}
    assert source['Exposure']['zh-CN']=='营养暴露因素'

def test_hero_uses_published_source_metadata():
    minimal='<html><head><title>Source</title></head><body><header></header><main><section class="hero"><h1>Source</h1><p>Old text</p></section></main></body></html>'
    for source in ('MetaVir','metaPee'):
        with app.app.test_request_context('/evidence/prostate/?include_subexposures=no'):
            soup=BeautifulSoup(app.decorate_html(minimal,source,''),'html.parser')
        assert soup.select_one('.app-identity h1').text=='MetaUro'
        time=soup.select_one('.app-identity #last-updated')
        manifest=json.loads((ROOT/'summary/no/summary_manifest.json').read_text())
        assert time['datetime']==manifest['sources'][source]['metadata']['generated_at']
        assert time.text!='—'
        assert len(soup.select('#last-updated'))==1
