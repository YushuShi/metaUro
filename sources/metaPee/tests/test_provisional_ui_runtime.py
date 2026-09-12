import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from scripts import provisional_ui_runtime as runtime
from scripts import reconcile_screening_tracks as reconcile


def test_interactive_selection_uses_saved_quality_and_gates():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = {'canonical_track':'combined','disease':'Example','exposure':'Dairy','endpoint':'Incidence',
                  'studies':[{'Record ID':'x','Quality Score':'Fair','policy_poolable':False,'Effect Size':1.2}]}
        submitted = runtime.identify_rows(result, root, 'yes')['studies']
        submitted[0].update({'Quality Score':'Good','policy_poolable':True,'Effect Size':1.4})
        bound = runtime.bind_submitted_rows(submitted, result, root, 'yes')
        assert bound[0]['Quality Score'] == 'Fair'
        assert bound[0]['policy_poolable'] is False
        assert bound[0]['Effect Size'] == 1.4
        assert result['studies'][0]['Effect Size'] == 1.2


def test_saved_generation_and_mode_reject_stale_row_selection():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = {'canonical_track':'combined','studies':[{'Record ID':'x'}]}
        submitted = runtime.identify_rows(result, root, 'yes')['studies']
        import pytest
        with pytest.raises(ValueError, match='stale'):
            runtime.bind_submitted_rows(submitted, result, root, 'no')
        path = runtime.view_root(root) / 'manifest.json';path.parent.mkdir(parents=True);path.write_text('{}')
        with pytest.raises(ValueError, match='stale'):
            runtime.bind_submitted_rows(submitted, result, root, 'yes')


def test_external_staging_retains_before_copy_without_changing_canonical():
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        stage = directory / 'staged'; audit = directory / 'audit'
        path = stage / 'dairy/example.json';path.parent.mkdir(parents=True);path.write_text('{"old":true}')
        with patch.object(reconcile, 'COMBINED', stage), patch.object(reconcile, 'AUDIT', audit):
            reconcile.write(path, {'new':True})
        assert json.loads(path.read_text()) == {'new':True}
        assert json.loads((audit / 'before/external_saved_view/dairy/example.json').read_text()) == {'old':True}


def test_source_change_invalidates_saved_view():
    import hashlib
    from scripts import provisional_review_policy as policy
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); source=root/'source.json';source.write_text('{"n":100}')
        manifest=runtime.view_root(root)/'manifest.json';manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'complete':True,'version':policy.VERSION,'protected_source_hashes':{'source.json':hashlib.sha256(source.read_bytes()).hexdigest()}}))
        assert runtime.ready(root)
        source.write_text('{"n":101}')
        assert not runtime.ready(root)


def test_active_analysis_settings_render_once_from_saved_rows():
    from unittest.mock import Mock
    backend=Mock(); result={'canonical_track':'combined','studies':[{'Record ID':'one'}]}
    with patch('analysis_settings_backend.settings_active', return_value=True):
        assert runtime.ensure_plots(result, Path('/not-used'), 'yes', {}, backend) is result
    backend.perform_meta_analysis.assert_not_called()
