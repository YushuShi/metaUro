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
    assert proof['registered_native_driver_r2'] is True
    assert proof['model_and_network_calls'] == 0


@pytest.mark.parametrize("scope", ["metaPee", "MetaVir"])
def test_isolated_monthly_model_requests(scope, tmp_path):
    from automation.autonomous_monthly import environment, monthly_profile

    worker = ROOT / "runtime_code/monthly" / scope
    expected = monthly_profile.specification()
    assert (worker / "automation/monthly_profile.py").read_bytes() == Path(monthly_profile.__file__).read_bytes()
    env = environment(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE="1", MPLCONFIGDIR=str(tmp_path / "mpl"))
    subprocess.run([sys.executable, str(ROOT / "tests/native_screening_profile_probe.py"), str(worker), str(tmp_path), json.dumps(expected)],
                   cwd=worker, env=env, check=True, timeout=60)

@pytest.mark.parametrize("scope", ["metaPee", "MetaVir"])
def test_worker_commands_from_outside_checkout(scope, tmp_path):
    worker = ROOT / "runtime_code/monthly" / scope / "automation"
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "CORNELL_API_KEY", "CORNELL_API_BASE_URL", "API_BASE_URI"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for script in ("monthly_pipeline", "monthly_adjudication", "preprint_recovery/preprint_screening", "gateway_preflight"):
        result = subprocess.run([sys.executable, str(worker / (script + ".py")), "--help"],
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        if script == "gateway_preflight":
            # This command has no argument parser. Missing credentials prevent network access.
            assert result.returncode == 1 and "credentials are missing" in result.stdout, result.stderr
        else:
            assert result.returncode == 0 and "usage:" in result.stdout, result.stderr
