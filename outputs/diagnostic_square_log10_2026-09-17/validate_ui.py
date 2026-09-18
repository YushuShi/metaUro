from pathlib import Path
import json,sys
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
def main():
    from app import app
    from source_worker import close_workers
    checks=0
    try:
        client=app.test_client()
        for source,public in [('metaPee','urinary'),('MetaVir','prostate')]:
            for mode in ('yes','no'):
                root=ROOT/'sources'/source
                plot=root/('combined/provisional_ui/subexposure_views/'+mode+'/Plot' if source=='metaPee' else 'Plot/subexposures/'+mode)
                manifest=json.loads((plot/'summary_manifest.json').read_text())
                response=client.get(f'/evidence/{public}/summary',query_string={'include_subexposures':mode})
                assert response.status_code==200,(public,response.status_code,response.data[:200]);response.close()
                for scope_key,scope in manifest['scopes'].items():
                    for name in ('effect-heterogeneity','egger-heterogeneity'):
                        entry=scope['plots'][name]
                        if not entry.get('available'):continue
                        for locale,filename in entry['locales'].items():
                            url=f'/evidence/{public}/summary/plots/{scope_key}/{name}'
                            response=client.get(url,query_string={'include_subexposures':mode,'lang':locale})
                            assert response.status_code==200,(url,response.status_code,response.data[:200])
                            assert response.data==(plot/filename).read_bytes(),(url,locale)
                            checks+=1;response.close()
    finally:close_workers()
    result={'metaUro_ui_plot_checks':checks,'summary_pages':4}
    (ROOT/'outputs/diagnostic_square_log10_2026-09-17/ui_validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
if __name__=='__main__':main()
