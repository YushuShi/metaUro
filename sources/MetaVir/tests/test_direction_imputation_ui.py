import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


ROOT = Path(__file__).resolve().parents[1]


class DirectionImputationUiTests(unittest.TestCase):
    def test_poolability_help_is_preserved_on_main_and_legacy_redirect(self):
        for route in ('/', '/chatgpt', '/about'):
            html = app_module.app.test_client().get(route, follow_redirects=True).get_data(as_text=True)
            self.assertIn('Pooling requires total sample size', html)
            self.assertIn('either a confidence interval or a usable p-value', html)
            self.assertIn('Exact case counts are optional', html)
            self.assertIn('Direction imputed', html)

    def test_both_evidence_apis_preserve_direction_imputation(self):
        for track in ('combined', 'chatgpt'):
            with self.subTest(track=track), tempfile.TemporaryDirectory() as tempdir:
                row = {
                    'PMID': '123', 'Sample Size': 1000, 'Cases': None,
                    'Quality Score': 'Good', 'JBI': {'q1': 'Yes'},
                    'comparison_type': 'HIGH_VS_LOW',
                    'comparison': 'high vs low',
                    'direction_imputed': True,
                    'direction_imputation_reason': 'Article does not state the reference direction.',
                }
                cache = {'canonical_track': track, 'studies': [row]}
                if track == 'combined':
                    (Path(tempdir) / '_merge_manifest.json').write_text(json.dumps({
                        'schema_version': 1, 'canonical_track': 'combined', 'build_id': 'test-build',
                    }))
                    cache['combined_projection'] = {'build_id': 'test-build'}
                cache_path = Path(app_module.get_cache_path(
                    'Prostate cancer', 'Tomato', 'Incidence', True, False,
                    cache_dir=tempdir,
                ))
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache))
                cache_setting = 'CACHE_DIR' if track == 'combined' else 'CHATGPT_CACHE_DIR'
                with patch.object(app_module, cache_setting, tempdir), patch.object(
                    app_module, '_load_verifications_for_update', return_value={}
                ):
                    response = app_module.app.test_client().post('/analyze', json={
                        'disease': 'Prostate cancer', 'exposure': 'Tomato',
                        'outcome': 'Incidence', 'exclude_meta': True, 'evidence_track': track,
                    })
                self.assertEqual(response.status_code, 200)
                actual = response.get_json()['studies'][0]
                for field in ('comparison', 'comparison_type', 'direction_imputed',
                              'direction_imputation_reason'):
                    self.assertEqual(actual[field], row[field])
                self.assertIsNone(actual['Cases'])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for JavaScript UI checks')
    def test_marker_requires_an_explicit_imputation_flag_and_renders_reason_as_text(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function element() {
    return {children: [], attributes: {}, appendChild(child) {this.children.push(child);},
        setAttribute(name, value) {this.attributes[name] = value;}};
}
const context = {window: {}, document: {body: {dataset: {}},
    addEventListener() {}, createElement: element}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/script.js', 'utf8'), context);
for (const flag of [undefined, false, 'false', '-']) {
    const target = element();
    context.appendDirectionImputationNotice(target, {direction_imputed: flag});
    assert.equal(target.children.length, 0);
}
const target = element();
const reason = 'Default yes vs no; <img src=x onerror=alert(1)> is literal evidence text.';
context.appendDirectionImputationNotice(target,
    {direction_imputed: true, direction_imputation_reason: reason}, true);
assert.equal(target.children.length, 2);
assert.equal(target.children[0].textContent, 'Direction imputed');
assert.equal(target.children[0].title, reason);
assert.equal(target.children[0].tabIndex, 0);
assert.equal(target.children[1].textContent, reason);
assert.equal(target.children[1].innerHTML, undefined);
assert.equal(context.directionImputationReason({direction_imputed: true,
    comparison: 'yes vs no'}), 'Assumed yes vs no; direction was not explicit in the article.');
assert.equal(context.directionImputationReason({direction_imputed: false}), '');
"""
        completed = subprocess.run(
            ['node', '-e', script], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_csv_keeps_imputation_status_and_reason(self):
        script = (ROOT / 'static' / 'script.js').read_text(encoding='utf-8')
        export_script = script.split('function exportStudiesToCsv()', 1)[1].split(
            '// Select All / Export / Deselect All Buttons', 1
        )[0]
        self.assertIn("'Direction Imputed', 'Direction Imputation Reason'", export_script)
        self.assertIn("uiText(study.direction_imputed === true ? 'Yes' : 'No')", export_script)
        self.assertIn('directionImputationReason(study)', export_script)
        self.assertIn("study['P Value']", export_script)


if __name__ == '__main__':
    unittest.main()
