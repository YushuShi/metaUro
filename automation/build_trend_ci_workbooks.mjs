import fs from 'node:fs/promises';
import path from 'node:path';
import { FileBlob, Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const [input, destination] = process.argv.slice(2);
const previewOnly = process.argv.includes('--preview-only');
const payload = JSON.parse(await fs.readFile(input, 'utf8'));
const checks = [];
const errorPattern = /^(?:#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A|#NUM!|#NULL!|#SPILL!|#CALC!)$/;
function columnName(index) {
  let name = '';
  for (let value = index + 1; value > 0; value = Math.floor((value - 1) / 26)) name = String.fromCharCode(65 + (value - 1) % 26) + name;
  return name;
}
for (const item of payload) {
  const directory = item.outputPath ? path.dirname(item.outputPath) : path.join(destination, item.mode, 'workbooks');
  const outputPath = item.outputPath || path.join(directory, item.filename);
  const existing = await fs.access(outputPath).then(() => true, () => false);
  const workbook = existing ? await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath)) : Workbook.create();
  const sheet = existing ? workbook.worksheets.getItemAt(0) : workbook.worksheets.add(`Estimates ${item.mode === 'yes' ? 'Yes' : 'No'}`);
  const sheetName = sheet.name;
  const previewDirectory = path.join(path.dirname(input), previewOnly ? 'previews_before' : 'previews');
  await fs.mkdir(previewDirectory, { recursive: true });
  if (previewOnly) {
    for (const range of ['A1:H10']) {
      const preview = await workbook.render({ sheetName, range, scale: 1.5, format: 'png' });
      await fs.writeFile(path.join(previewDirectory, `${item.mode}_${item.filename.replace('.xlsx', '')}_${range.replace(':', '-')}.png`), new Uint8Array(await preview.arrayBuffer()));
    }
    continue;
  }
  const values = [item.columns, ...item.rows.map(row => item.columns.map(col => row[col] ?? null))];
  const last = Math.max(2, values.length);
  const previousRows = item.previousRows ?? (existing ? Math.max(0, sheet.getUsedRange(true).values.length - 1) : 0);
  if (existing && previousRows > item.rows.length) sheet.getRangeByIndexes(values.length, 0, previousRows - item.rows.length, item.columns.length).clear({ applyTo: 'contents' });
  sheet.getRangeByIndexes(0, 0, values.length, item.columns.length).values = values;
  if (!existing) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(1);
  const table = sheet.getRangeByIndexes(0, 0, last, item.columns.length);
  table.format.font = { name: 'Arial', size: 10 };
  table.format.columnWidth = 16;
  table.format.rowHeight = 34;
  table.format.verticalAlignment = 'center';
  sheet.getRangeByIndexes(0, 0, 1, item.columns.length).format = {
    fill: '#334155', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' },
    wrapText: true, rowHeight: 40,
  };
  sheet.getRange(`A1:A${last}`).format.columnWidth = 34;
  sheet.getRange(`C2:C${last}`).format.wrapText = true;
  for (let col = 0; col < item.columns.length; col++) {
    const name = item.columns[col];
    const cells = sheet.getRangeByIndexes(1, col, last - 1, 1);
    if (['number studies', 'total N', 'total Cases'].includes(name)) cells.setNumberFormat('#,##0');
    else if (['Pooled', 'lower', 'upper', 'I^2', 'eggers'].some(prefix => name.startsWith(prefix))) cells.setNumberFormat('0.000');
    if (['Exposure', 'Effect measure', 'Outcome Label', 'Baseline incidence description', 'Category', 'Evidence sources'].includes(name)) {
      sheet.getRangeByIndexes(0, col, last, 1).format.columnWidth = name === 'Evidence sources' ? 70 : 34;
    }
  }
  }
  // Longer CSV provenance needs room while preserving all other existing styles.
  const descriptionColumn = item.columns.indexOf('Baseline incidence description');
  if (descriptionColumn >= 0 && item.outcome && ['alzheimer_disease_incidence', 'parkinson_disease_incidence', 'combined_cognitive_alzheimer'].includes(item.outcome)) {
    sheet.getRangeByIndexes(0, descriptionColumn, last, 1).format.columnWidth = 80;
    sheet.getRangeByIndexes(0, descriptionColumn, last, 1).format.wrapText = true;
    const maxDescriptionLength = Math.max(...item.rows.map(row => String(row['Baseline incidence description'] ?? '').length), 0);
    sheet.getRangeByIndexes(1, descriptionColumn, last - 1, 1).format.rowHeight = Math.max(90, Math.ceil(maxDescriptionLength / 75) * 14 + 14);
    const effectColumn = item.columns.indexOf('Effect measure');
    if (effectColumn >= 0) sheet.getRangeByIndexes(0, effectColumn, last, 1).format = { columnWidth: 34, wrapText: true };
    const baselineColumn = item.columns.indexOf('Conversion baseline incidence');
    if (baselineColumn >= 0) sheet.getRangeByIndexes(0, baselineColumn, last, 1).format = { columnWidth: 38, wrapText: true };
  }
  if (item.analysisInputSha256 && item.analysisMetadataSheet) {
      const metadata = workbook.worksheets.getItem(item.analysisMetadataSheet);
      const existingValues = metadata.getUsedRange(true).values;
      for (let r = 0; r < existingValues.length; r++) {
        const label = String(existingValues[r]?.[0] ?? '');
        const exposure = label.startsWith('Input SHA-256 / ') ? label.slice(16) : null;
        if (exposure && item.analysisInputSha256[exposure]) metadata.getCell(r, 1).values = [[item.analysisInputSha256[exposure]]];
      }
  }
  workbook.recalculate();
  const inspect = await workbook.inspect({ kind: 'table', range: `'${sheetName}'!A1:H5`, include: 'values,formulas', tableMaxRows: 5, tableMaxCols: 8, maxChars: 1600 });
  const errors = await workbook.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!', options: { useRegex: true, maxResults: 300 }, maxChars: 2000 });
  const actual = sheet.getRangeByIndexes(0, 0, values.length, item.columns.length).values;
  const errorCells = [];
  for (let row = 0; row < values.length; row++) {
    for (let col = 0; col < item.columns.length; col++) {
      const value = actual[row]?.[col] ?? null;
      if (typeof value === 'string' && errorPattern.test(value)) errorCells.push(`${columnName(col)}${row + 1}`);
      if (value !== (values[row][col] ?? null)) throw new Error(`Workbook value differs from its authoritative payload: ${item.mode}/${item.filename} ${columnName(col)}${row + 1}`);
    }
  }
  if (errorCells.length) throw new Error(`Spreadsheet error cells in ${item.mode}/${item.filename}: ${errorCells.join(', ')}`);
  await fs.mkdir(directory, { recursive: true });
  await fs.mkdir(previewDirectory, { recursive: true });
  const preview = await workbook.render({ sheetName, range: 'A1:H10', scale: 1.5, format: 'png' });
  const previewPath = path.join(previewDirectory, `${item.mode}_${item.filename.replace('.xlsx', '.png')}`);
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
  const previews = [{ range: 'A1:H10', path: previewPath }];
  // Keep the existing opening preview and also cover counts, outcomes and sources.
  for (const [first, width] of [[8, 9], [17, 6], [23, 6]]) {
    if (first >= item.columns.length) continue;
    const start = columnName(first);
    const end = columnName(Math.min(item.columns.length - 1, first + width - 1));
    const range = `${start}1:${end}10`;
    const panel = await workbook.render({ sheetName, range, scale: 1.5, format: 'png' });
    const panelPath = path.join(previewDirectory, `${item.mode}_${item.filename.replace('.xlsx', '')}_${start}-${end}.png`);
    await fs.writeFile(panelPath, new Uint8Array(await panel.arrayBuffer()));
    previews.push({ range, path: panelPath });
  }
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);
  checks.push({ mode: item.mode, filename: item.filename, rows: item.rows.length, recalculated: true, exact_payload_cells_verified: values.length * item.columns.length, error_count: errorCells.length, inspect: inspect.ndjson, errors: errors.ndjson, preview: previewPath, previews });
  console.log(`Exported ${item.mode}/${item.filename}: ${item.rows.length} rows`);
}
if (!previewOnly) await fs.writeFile(path.join(path.dirname(input), 'workbook_verification.json'), JSON.stringify(checks, null, 2));
