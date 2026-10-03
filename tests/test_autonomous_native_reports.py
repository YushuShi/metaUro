import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scope', ['metaPee', 'MetaVir'])
def test_native_report_finalization_keeps_canonical_ledger(scope, tmp_path):
    runtime = ROOT/'runtime_code/monthly'/scope
    inventory = lambda: {str(p.relative_to(runtime)):hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in runtime.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    before = inventory()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', MPLBACKEND='Agg',
               MPLCONFIGDIR=str(tmp_path/'matplotlib'))
    result = subprocess.run([sys.executable, str(ROOT/'tests/native_ledger_probe.py'),
                             str(ROOT), scope, str(tmp_path)],
                            cwd=runtime, env=env,
                            text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout+'\n'+result.stderr
    assert inventory() == before, 'Native proof wrote outside its temporary fixture'
    proof = json.loads(result.stdout)
    assert proof['native_report_finalizer_validator'] is True
    assert proof['old_order_rejected'] is True
    assert proof['model_and_network_calls'] == 0
