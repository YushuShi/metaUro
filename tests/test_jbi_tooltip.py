"""The served evidence table must display both saved JBI answer layouts."""
import shutil
import subprocess

import pytest

from app import app


@pytest.mark.parametrize('scope', ('urinary', 'prostate'))
def test_served_jbi_tooltip_reads_nested_and_flat_answers(scope):
    if not shutil.which('node'):
        pytest.skip('Node.js is needed to evaluate the served JavaScript')

    with app.test_client() as client:
        response = client.get(f'/evidence/{scope}/static/script.js')
        conditional = client.get(
            f'/evidence/{scope}/static/script.js',
            headers={'If-Modified-Since': response.headers['Last-Modified']},
        )
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert conditional.status_code == 200
    assert conditional.data == response.data
    script = response.get_data(as_text=True)
    start = script.index('    function itemizedJbiEntries(study) {')
    end = script.index('\n    }', start) + len('\n    }')
    helper = script[start:end]
    assert 'const answers = study.JBI.answers;' in helper

    check = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {};
vm.runInNewContext(fs.readFileSync(0, 'utf8'), context);
const entries = study => JSON.parse(JSON.stringify(context.itemizedJbiEntries(study)));

// PMID 14973107 is saved in this nested format.
const answers = Object.fromEntries(Array.from({length: 13}, (_, i) => [`q${i + 1}`, 'Yes']));
answers.q3 = 'Unclear';
answers.q11 = 'NA';
const nested = entries({JBI: {answers, checklist_type: 'case_control', grade: 'Fair'}});
assert.deepEqual(nested.map(item => item.question),
    Array.from({length: 13}, (_, i) => `Q${i + 1}`));
assert.equal(nested[2].answer, 'Unclear');
assert.equal(nested[10].answer, 'NA');

const flat = entries({JBI: {q2: 'No', q1: 'Yes', grade: 'Moderate'}});
assert.deepEqual(flat, [
    {question: 'Q1', answer: 'Yes'},
    {question: 'Q2', answer: 'No'},
]);
assert.deepEqual(entries({JBI: {answers: {}, q1: 'Yes'}}),
    [{question: 'Q1', answer: 'Yes'}]);
assert.deepEqual(entries({JBI: {grade: 'Fair'}}), []);
"""
    completed = subprocess.run(
        ['node', '-e', check], input=helper, text=True,
        capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr
