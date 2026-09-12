/* Inherited aggressive evidence must not inherit a general-prostate risk. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../static/script.js'), 'utf8');
const start = source.indexOf('function studyBaselineRisk(');
const end = source.indexOf('\nfunction uiText(', start);
const sandbox = {};vm.createContext(sandbox);vm.runInContext(source.slice(start,end),sandbox);
const baseline=sandbox.studyBaselineRisk;
const aggressive={'Disease Endpoint':'Aggressive prostate cancer'};
assert.ok(Number.isNaN(baseline(aggressive,'Prostate cancer',0.2)));
assert.ok(Number.isNaN(baseline({'Disease Endpoint':'aggressive'},'Prostate cancer',0.2)));
assert.ok(Number.isNaN(baseline({...aggressive,'Conversion Baseline Risk':0.2},'Prostate cancer',0.2)));
assert.equal(baseline(aggressive,'Aggressive prostate cancer',0.005),0.005);
assert.equal(baseline({...aggressive,'Conversion Baseline Risk':0.003},'Aggressive prostate cancer',0.005),0.003);
assert.ok(Number.isNaN(baseline({...aggressive,'Conversion Baseline Risk':null},'Aggressive prostate cancer',0.005)));
assert.equal(baseline({},'Prostate cancer',0.05),0.05);
console.log('Per-row aggressive baseline isolation passed');
