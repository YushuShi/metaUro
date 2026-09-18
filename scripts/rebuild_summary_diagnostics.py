"""Restyle metaUro diagnostics and refresh the release integrity inventory."""
from pathlib import Path
import os,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    for source in ('metaPee','MetaVir'):
        root=ROOT/'sources'/source;out=root/'outputs/diagnostic_square_log10_2026-09-17'
        env=dict(os.environ,MPLCONFIGDIR=str(out/'matplotlib-cache'),TMPDIR=str(out))
        subprocess.run([sys.executable,'-B','scripts/rebuild_summary_diagnostics.py'],cwd=root,env=env,check=True)
    from scripts.refresh_diagnostic_integrity import refresh
    refresh()
    subprocess.run([sys.executable,'-B','scripts/verify_evidence.py'],cwd=ROOT,check=True)

if __name__=='__main__':main()
