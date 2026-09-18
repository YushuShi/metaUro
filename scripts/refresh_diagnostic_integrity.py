"""Record derived diagnostic changes without rewriting scientific evidence."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,shutil
ROOT=Path(__file__).resolve().parents[1]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def record(path):return {'path':str(path.relative_to(ROOT)),'sha256':sha(path),'bytes':path.stat().st_size}
def refresh():
    manifest_path=ROOT/'evidence-manifest.json';manifest=json.loads(manifest_path.read_text())
    entries={entry['path']:entry for entry in manifest['files']}; allowed=set(); inputs={}
    for source in ('metaPee','MetaVir'):
        base=ROOT/'sources'/source
        audit=json.loads((base/'outputs/diagnostic_square_log10_2026-09-17/render_audit.json').read_text())
        assert audit['inputs_before']==audit['inputs_after']
        for relative,digest in audit['inputs_before'].items():
            assert sha(base/relative)==digest,'Scientific input changed after rendering'
            inputs[str((base/relative).relative_to(ROOT))]=digest
        for relative,entry in audit['outputs'].items():
            path=base/relative
            for item,key in [(path,'pdf_sha256'),(path.with_suffix('.png'),'png_sha256')]:
                assert sha(item)==entry[key]
                allowed.add(item)
        allowed.update(base/'scripts'/name for name in ['build_summary_plots.py','plot_localization.py','diagnostic_style.py','rebuild_summary_diagnostics.py'])
    changes=[]
    for path in sorted(allowed):
        entry=record(path);previous=entries.get(entry['path'])
        if previous!=entry:
            changes.append({'path':entry['path'],'before':previous,'after':entry})
            entries[entry['path']]=entry
    if not changes:
        print('metaUro: diagnostic integrity records already current.');return
    version='diagnostic-style-2026-09-17-'+sha(manifest_path)[:10]
    revision=ROOT/'evidence-revisions'/version;revision.mkdir(parents=True,exist_ok=True)
    prior=revision/'prior-release-manifest.json';shutil.copy2(manifest_path,prior)
    audit_path=revision/'revision.json'
    audit_path.write_text(json.dumps({'revision':version,'prior_active_revision':manifest.get('active_revision'),
        'scope':'Derived diagnostic plots and rendering code only',
        'style':{'reference':'MetaFeminaMultiLanguage/Plot/Plot.R','labels':'12-14 pt bold','shape':'square','egger_axis':'log10(p)'},
        'scientific_inputs_sha256':inputs,'changes':changes},indent=2)+'\n')
    readme=revision/'README.md';readme.write_text('Diagnostic presentation revision\n\nUses the requested Plot.R aesthetic, 12-14 pt bold exposure labels, square pages, and log10 Egger p values. Saved workbooks, pooled estimates, eligibility and screening records remain unchanged. This revision applies only to metaUro embedded sources. The previous release manifest and original baseline provenance are retained.\n')
    manifest['files']=sorted(entries.values(),key=lambda e:e['path'])
    manifest.setdefault('supporting_files',[]).extend(record(p) for p in [prior,audit_path,readme])
    manifest['active_revision']=version;manifest['updated_at']=datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    print(f'metaUro: recorded {len(changes)} diagnostic/code assets; prior evidence provenance retained.',flush=True)
if __name__=='__main__':refresh()
