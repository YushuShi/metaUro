#!/usr/bin/env node

/**
 * Compare the canonical ChatGPT screening/extraction decisions with the
 * completed Claude-R2 + Gemini-R3 validation track.
 *
 * The comparison is deliberately restricted to the 6,314 contexts that
 * passed the shared R1 screen and therefore received an independent Claude
 * detailed review. Gemini full-text rows override Claude rows for their exact
 * context_id. ChatGPT rows are reconstructed from the selected R1 source so
 * superseded screening generations are never mixed into the comparison.
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
const MONTHLY_ROOT = path.join(REPO_ROOT, "data", "monthly_updates");
const PARALLEL_ROOT = path.join(
  REPO_ROOT,
  "data",
  "claude_gemini_screening",
  "all_history_through_2026-08-29",
);
const DEFAULT_OUTPUT_DIR = path.join(
  REPO_ROOT,
  "outputs",
  "chatgpt_claude_gemini_comparison_2026-09-03",
);

const SCREENING_COLUMNS = [
  "PMID",
  "first_author_last_name",
  "exposure",
  "cancer",
  "context_id",
  "chatgpt_source_generation",
  "claude_gemini_decision_basis",
  "missing_from_pipeline",
  "included_by_pipeline",
  "excluded_by_pipeline",
  "chatgpt_status",
  "claude_gemini_status",
  "exclusion_code",
  "exclusion_reason",
  "brief_explanation",
];

const EXTRACTION_COLUMNS = [
  "PMID",
  "first_author_last_name",
  "exposure",
  "cancer",
  "context_id",
  "chatgpt_source_generation",
  "claude_gemini_decision_basis",
  "evidence_type",
  "chatgpt_evidence_type",
  "claude_gemini_evidence_type",
  "verbal_evidence_note",
  "discrepant_fields",
  "chatgpt_sample_size",
  "claude_gemini_sample_size",
  "chatgpt_effect_type",
  "claude_gemini_effect_type",
  "chatgpt_effect_size",
  "claude_gemini_effect_size",
  "chatgpt_ci_lower",
  "chatgpt_ci_upper",
  "claude_gemini_ci_lower",
  "claude_gemini_ci_upper",
  "chatgpt_p_value",
  "claude_gemini_p_value",
  "chatgpt_comparison",
  "claude_gemini_comparison",
  "brief_explanation",
];

function readJson(relativeOrAbsolutePath) {
  const resolved = path.isAbsolute(relativeOrAbsolutePath)
    ? relativeOrAbsolutePath
    : path.join(REPO_ROOT, relativeOrAbsolutePath);
  return JSON.parse(fs.readFileSync(resolved, "utf8"));
}

function requireArray(value, label) {
  if (!Array.isArray(value)) {
    throw new Error(`${label} must be an array`);
  }
  return value;
}

function uniqueMap(rows, label) {
  const result = new Map();
  for (const row of rows) {
    const contextId = String(row?.context_id ?? "");
    if (!contextId) {
      throw new Error(`${label} contains a row without context_id`);
    }
    if (result.has(contextId)) {
      throw new Error(`${label} contains duplicate context_id ${contextId}`);
    }
    result.set(contextId, row);
  }
  return result;
}

function firstAuthorLastName(...authorValues) {
  let author = "";
  for (const value of authorValues) {
    if (Array.isArray(value) && value.length) {
      author = String(value[0] ?? "").trim();
    } else if (typeof value === "string" && value.trim()) {
      author = value.split(/\s*;\s*|\s*,\s*(?=[A-Z][a-z])/u, 1)[0].trim();
    }
    if (author) break;
  }
  if (!author) return "Not listed";
  if (/\b(?:collaboration|committee|consortium|group|investigators|network|study|team)\b/iu.test(author)) {
    return author;
  }
  if (author.includes(",")) return author.split(",", 1)[0].trim();
  const tokens = author.split(/\s+/u);
  if (tokens.length > 1 && /^[A-Z][A-Z.'’\-]*$/u.test(tokens.at(-1))) {
    tokens.pop();
  }
  return tokens.join(" ");
}

function combinedDecisionBasis(r2, r3) {
  if (r3) return "Gemini R3 full-text review";
  if (r2?.needs_full_text && !r2?.full_text_eligible) {
    return "Claude R2 retained; Gemini deferred (no PMCID)";
  }
  return "Claude R2 detailed screen; Gemini not required";
}

function normalizedStatus(row, pipeline) {
  if (pipeline === "chatgpt") {
    return String(row?.final_status ?? "");
  }
  const finalStatus = String(row?.final_status ?? row?.full_text_status ?? "");
  if (finalStatus) return finalStatus;
  if (row?.eligibility === "EXCLUDE") return "EXCLUDE";
  return row?.needs_full_text ? "ELIGIBLE_NOT_POOLABLE" : "INCLUDE_META";
}

function isIncluded(row, pipeline) {
  return normalizedStatus(row, pipeline) !== "EXCLUDE";
}

function parseNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const normalized = String(value)
    .trim()
    .replace(/[−–—]/gu, "-")
    .replace(/,/gu, "")
    .replace(/^[<>≤≥=~≈]\s*/u, "");
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function decimalPlaces(value) {
  const parsed = parseNumber(value);
  if (parsed === null) return null;
  const text = String(parsed).toLowerCase();
  const [coefficient, exponentText] = text.split("e");
  const exponent = exponentText ? Number(exponentText) : 0;
  const fractionLength = (coefficient.split(".", 2)[1] ?? "").length;
  return Math.max(0, fractionLength - exponent);
}

function obviousRoundingEquivalent(left, right, minimumPlaces = 2) {
  const a = parseNumber(left);
  const b = parseNumber(right);
  if (a === null || b === null) return false;
  const aPlaces = decimalPlaces(left);
  const bPlaces = decimalPlaces(right);
  if (aPlaces === bPlaces) return false;
  const fewerPlaces = Math.min(aPlaces, bPlaces);
  // Require at least hundredths precision. This catches harmless display-level
  // differences such as 2.812 versus 2.81 without treating materially distinct
  // one-decimal estimates as equivalent merely because both round to 3.2.
  if (fewerPlaces < minimumPlaces) return false;
  const lessPrecise = aPlaces < bPlaces ? a : b;
  const morePrecise = aPlaces < bPlaces ? b : a;
  const scale = 10 ** fewerPlaces;
  const rounded = Math.round((morePrecise + Number.EPSILON) * scale) / scale;
  return Math.abs(rounded - lessPrecise) <= 1e-12 * Math.max(1, Math.abs(lessPrecise));
}

function effectValueEquivalent(left, right) {
  if (numbersEquivalent(left?.effect_size, right?.effect_size)) return true;
  return (
    obviousRoundingEquivalent(left?.effect_size, right?.effect_size, 1)
    && uncertaintyEquivalent(left, right)
  );
}

function numbersEquivalent(left, right) {
  const a = parseNumber(left);
  const b = parseNumber(right);
  if (a === null || b === null) return a === b;
  if (Object.is(a, b) || a === b) return true;
  // This tolerance removes binary floating-point noise only. It does not hide
  // scientifically distinct rounded estimates such as 1.07 versus 1.08.
  return (
    Math.abs(a - b) <= 1e-12 * Math.max(1, Math.abs(a), Math.abs(b))
    || obviousRoundingEquivalent(left, right)
  );
}

function normalizeEffectType(value) {
  const text = String(value ?? "").trim().toUpperCase();
  if (!text) return "";
  if (/\bOR\b|ODDS RATIO/u.test(text)) return "OR";
  if (/\bRR\b|RISK RATIO|RATE RATIO|RELATIVE RISK/u.test(text)) return "RR";
  if (/\bHR\b|HAZARD RATIO/u.test(text)) return "HR";
  return text.replace(/\s+/gu, " ");
}

function completeCi(extraction) {
  const lower = parseNumber(extraction?.ci_lower);
  const upper = parseNumber(extraction?.ci_upper);
  return lower === null || upper === null ? null : [lower, upper];
}

function hasAnyUncertainty(extraction) {
  return [extraction?.ci_lower, extraction?.ci_upper, extraction?.p_value]
    .some((value) => parseNumber(value) !== null);
}

function uncertaintyEquivalent(left, right) {
  const leftCi = completeCi(left);
  const rightCi = completeCi(right);
  const ciMatches = Boolean(
    leftCi
      && rightCi
      && numbersEquivalent(leftCi[0], rightCi[0])
      && numbersEquivalent(leftCi[1], rightCi[1]),
  );
  const leftP = parseNumber(left?.p_value);
  const rightP = parseNumber(right?.p_value);
  const pMatches = leftP !== null && rightP !== null && numbersEquivalent(leftP, rightP);
  if (ciMatches || pMatches) return true;
  return !hasAnyUncertainty(left) && !hasAnyUncertainty(right);
}

function display(value) {
  return value === null || value === undefined || value === "" ? "missing" : String(value);
}

function evidenceType(extraction) {
  const raw = String(extraction?.exposure_measurement_type ?? "").trim();
  if (raw === "human_biospecimen" || raw === "biospecimen" || raw === "biomarker") {
    return "biospecimen";
  }
  if (raw === "dietary_intake") return "dietary intake";
  return raw || "unclear";
}

function hasVerbalExposureEvidence(extraction) {
  const entry = extraction?.evidence?.exposure_measurement;
  if (typeof entry === "string") return Boolean(entry.trim());
  return Boolean(entry && typeof entry === "object" && String(entry.excerpt ?? "").trim());
}

function verbalEvidenceNote(chatExtraction, combinedExtraction) {
  const chatMissing = !hasVerbalExposureEvidence(chatExtraction);
  const combinedMissing = !hasVerbalExposureEvidence(combinedExtraction);
  const notes = [];
  if (chatMissing) {
    notes.push(
      `ChatGPT lacks verbal exposure-measurement evidence (${evidenceType(chatExtraction)}); not counted as a discrepancy`,
    );
  }
  if (combinedMissing) {
    notes.push(
      `Claude+Gemini lacks verbal exposure-measurement evidence (${evidenceType(combinedExtraction)}); not counted as a discrepancy`,
    );
  }
  return notes.join("; ");
}

function screeningExplanation({ missing, code, reason, combinedBasis }) {
  const codeText = code ? ` (${code})` : "";
  const reasonText = String(reason || "no exclusion reason was recorded")
    .trim()
    .replace(/[.;:,\s]+$/u, "");
  if (missing === "Claude+Gemini") {
    const excludingStage = combinedBasis.startsWith("Gemini") ? "Gemini R3" : "Claude R2";
    return `Missing from Claude+Gemini: ${excludingStage} excluded this context${codeText} because ${reasonText}; ChatGPT retained it.`;
  }
  if (combinedBasis.includes("deferred")) {
    return `Missing from ChatGPT: ChatGPT excluded this context${codeText} because ${reasonText}; Claude R2 retained it, but Gemini made no decision because no PMCID was available.`;
  }
  if (combinedBasis.startsWith("Gemini")) {
    return `Missing from ChatGPT: ChatGPT excluded this context${codeText} because ${reasonText}; Gemini R3 retained it after full-text review.`;
  }
  return `Missing from ChatGPT: ChatGPT excluded this context${codeText} because ${reasonText}; Claude R2 retained it and no Gemini review was required.`;
}

function extractionExplanation(fields, chat, combined) {
  const parts = [];
  if (fields.includes("sample_size")) {
    parts.push(
      `sample size differs (ChatGPT ${display(chat?.total_n)} vs Claude+Gemini ${display(combined?.total_n)})`,
    );
  }
  if (fields.includes("effect_size")) {
    parts.push(
      `effect size differs (ChatGPT ${display(chat?.effect_type)} ${display(chat?.effect_size)} vs Claude+Gemini ${display(combined?.effect_type)} ${display(combined?.effect_size)})`,
    );
  }
  if (fields.includes("effect_measure_type")) {
    parts.push(
      `effect measure differs despite the same numeric estimate (ChatGPT ${display(chat?.effect_type)} vs Claude+Gemini ${display(combined?.effect_type)})`,
    );
  }
  if (fields.includes("confidence_interval_or_p_value")) {
    parts.push(
      `neither uncertainty measure matches (ChatGPT CI ${display(chat?.ci_lower)}–${display(chat?.ci_upper)}, p ${display(chat?.p_value)} vs Claude+Gemini CI ${display(combined?.ci_lower)}–${display(combined?.ci_upper)}, p ${display(combined?.p_value)})`,
    );
  }
  return `${parts.join("; ")}.`;
}

function csvCell(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\r\n]/u.test(text) ? `"${text.replace(/"/gu, '""')}"` : text;
}

function csvText(columns, rows) {
  const lines = [columns.map(csvCell).join(",")];
  for (const row of rows) {
    lines.push(columns.map((column) => csvCell(row[column])).join(","));
  }
  return `\uFEFF${lines.join("\n")}\n`;
}

function parseArguments(argv) {
  const parsed = { diagnostics: false, write: false, outputDir: DEFAULT_OUTPUT_DIR };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--write") parsed.write = true;
    else if (value === "--diagnostics") parsed.diagnostics = true;
    else if (value === "--output-dir") {
      index += 1;
      if (!argv[index]) throw new Error("--output-dir requires a value");
      parsed.outputDir = path.resolve(argv[index]);
    } else {
      throw new Error(`Unknown argument: ${value}`);
    }
  }
  return parsed;
}

function incrementCounter(counter, key) {
  counter[key] = (counter[key] ?? 0) + 1;
}

function quantitativeDiagnostics(rows) {
  const mismatchPatterns = {};
  const sampleSizeModes = {};
  const effectModes = {};
  const evidenceTypes = {};
  const effectNearPairs = [];
  const effectTypeOnlyPairs = [];
  const ciNearPairs = [];
  const pValueNearPairs = [];
  const effectOnlyRows = [];
  for (const row of rows) {
    incrementCounter(mismatchPatterns, row.discrepant_fields);
    incrementCounter(evidenceTypes, row.evidence_type);
    if (row.discrepant_fields === "effect_size") {
      effectOnlyRows.push({
        PMID: row.PMID,
        exposure: row.exposure,
        chatgpt_type: row.chatgpt_effect_type,
        chatgpt_effect: row.chatgpt_effect_size,
        claude_gemini_type: row.claude_gemini_effect_type,
        claude_gemini_effect: row.claude_gemini_effect_size,
        chatgpt_ci: [row.chatgpt_ci_lower, row.chatgpt_ci_upper],
        claude_gemini_ci: [row.claude_gemini_ci_lower, row.claude_gemini_ci_upper],
        chatgpt_p: row.chatgpt_p_value,
        claude_gemini_p: row.claude_gemini_p_value,
        chatgpt_comparison: row.chatgpt_comparison,
        claude_gemini_comparison: row.claude_gemini_comparison,
      });
    }

    if (row.discrepant_fields.includes("sample_size")) {
      const left = parseNumber(row.chatgpt_sample_size);
      const right = parseNumber(row.claude_gemini_sample_size);
      incrementCounter(
        sampleSizeModes,
        left === null || right === null ? "one_missing" : "both_present_different",
      );
    }

    if (
      row.discrepant_fields.includes("effect_size")
      || row.discrepant_fields.includes("effect_measure_type")
    ) {
      const left = parseNumber(row.chatgpt_effect_size);
      const right = parseNumber(row.claude_gemini_effect_size);
      const typeOnly =
        left !== null
        && right !== null
        && numbersEquivalent(left, right)
        && row.discrepant_fields.includes("effect_measure_type")
        && normalizeEffectType(row.chatgpt_effect_type)
          !== normalizeEffectType(row.claude_gemini_effect_type);
      incrementCounter(
        effectModes,
        typeOnly
          ? "equal_number_different_type"
          : left === null || right === null
            ? "one_missing"
            : "both_present_different",
      );
      if (typeOnly) {
        effectTypeOnlyPairs.push({
          PMID: row.PMID,
          exposure: row.exposure,
          effect_size: left,
          chatgpt_type: row.chatgpt_effect_type,
          claude_gemini_type: row.claude_gemini_effect_type,
          chatgpt_comparison: row.chatgpt_comparison,
          claude_gemini_comparison: row.claude_gemini_comparison,
        });
      }
      if (
        left !== null
        && right !== null
        && !numbersEquivalent(left, right)
        && Math.abs(left - right) <= 0.01
      ) {
        effectNearPairs.push({
          PMID: row.PMID,
          exposure: row.exposure,
          chatgpt: left,
          claude_gemini: right,
          chatgpt_comparison: row.chatgpt_comparison,
          claude_gemini_comparison: row.claude_gemini_comparison,
        });
      }
    }

    if (row.discrepant_fields.includes("confidence_interval_or_p_value")) {
      const chatLower = parseNumber(row.chatgpt_ci_lower);
      const chatUpper = parseNumber(row.chatgpt_ci_upper);
      const combinedLower = parseNumber(row.claude_gemini_ci_lower);
      const combinedUpper = parseNumber(row.claude_gemini_ci_upper);
      if (
        chatLower !== null
        && chatUpper !== null
        && combinedLower !== null
        && combinedUpper !== null
        && Math.max(
          Math.abs(chatLower - combinedLower),
          Math.abs(chatUpper - combinedUpper),
        ) <= 0.01
      ) {
        ciNearPairs.push({
          PMID: row.PMID,
          exposure: row.exposure,
          chatgpt: [chatLower, chatUpper],
          claude_gemini: [combinedLower, combinedUpper],
          chatgpt_comparison: row.chatgpt_comparison,
          claude_gemini_comparison: row.claude_gemini_comparison,
        });
      }
      const chatP = parseNumber(row.chatgpt_p_value);
      const combinedP = parseNumber(row.claude_gemini_p_value);
      if (
        chatP !== null
        && combinedP !== null
        && !numbersEquivalent(chatP, combinedP)
        && Math.abs(chatP - combinedP) <= 0.0001
      ) {
        pValueNearPairs.push({
          PMID: row.PMID,
          exposure: row.exposure,
          chatgpt: chatP,
          claude_gemini: combinedP,
          chatgpt_ci: [chatLower, chatUpper],
          claude_gemini_ci: [combinedLower, combinedUpper],
        });
      }
    }
  }
  return {
    mismatch_patterns: mismatchPatterns,
    sample_size_modes: sampleSizeModes,
    effect_modes: effectModes,
    evidence_types: evidenceTypes,
    near_effect_pairs_at_most_0_01: effectNearPairs,
    equal_effect_number_different_type_pairs: effectTypeOnlyPairs,
    near_ci_pairs_max_endpoint_difference_at_most_0_01: ciNearPairs,
    near_p_value_pairs_at_most_0_0001: pValueNearPairs,
    effect_only_rows: effectOnlyRows,
  };
}

function buildComparison() {
  const selectedPacket = readJson(path.join(PARALLEL_ROOT, "r1_selected_decisions.json"));
  const selectedRows = requireArray(selectedPacket.results, "r1_selected_decisions.results");
  const r1PassRows = selectedRows.filter((row) => row.decision === "PASS");
  const selectedByContext = uniqueMap(r1PassRows, "selected R1 PASS decisions");

  const finalMapsBySource = new Map();
  for (const row of r1PassRows) {
    const source = String(row.selected_source ?? "");
    if (!source) throw new Error(`R1 PASS ${row.context_id} lacks selected_source`);
    if (!finalMapsBySource.has(source)) {
      const packet = readJson(
        path.join(MONTHLY_ROOT, source, "chatgpt_final_screening.json"),
      );
      finalMapsBySource.set(
        source,
        uniqueMap(requireArray(packet.results, `${source} ChatGPT results`), `${source} ChatGPT results`),
      );
    }
  }

  const r2Packet = readJson(path.join(PARALLEL_ROOT, "claude_r2_results.json"));
  const r3Packet = readJson(path.join(PARALLEL_ROOT, "gemini_r3_results.json"));
  const r2Rows = requireArray(r2Packet.results, "claude_r2_results.results");
  const r3Rows = requireArray(r3Packet.results, "gemini_r3_results.results");
  const r2ByContext = uniqueMap(r2Rows, "Claude R2 results");
  const r3ByContext = uniqueMap(r3Rows, "Gemini R3 results");

  if (r2ByContext.size !== selectedByContext.size) {
    throw new Error(
      `Claude R2 coverage ${r2ByContext.size} differs from selected R1 PASS coverage ${selectedByContext.size}`,
    );
  }
  for (const contextId of r2ByContext.keys()) {
    if (!selectedByContext.has(contextId)) {
      throw new Error(`Claude R2 context was not a selected R1 PASS: ${contextId}`);
    }
  }
  for (const contextId of r3ByContext.keys()) {
    if (!r2ByContext.has(contextId)) {
      throw new Error(`Gemini R3 context has no Claude R2 source: ${contextId}`);
    }
  }

  const screeningRows = [];
  const extractionRows = [];
  const statusMatrix = new Map();
  let bothIncluded = 0;
  let bothExcluded = 0;

  const contextIds = [...r2ByContext.keys()].sort((left, right) =>
    left.localeCompare(right, "en", { numeric: true }),
  );
  for (const contextId of contextIds) {
    const selected = selectedByContext.get(contextId);
    const chatMap = finalMapsBySource.get(String(selected.selected_source));
    const chat = chatMap.get(contextId);
    if (!chat) {
      throw new Error(`No ChatGPT final row for selected source/context ${contextId}`);
    }
    const r2 = r2ByContext.get(contextId);
    const r3 = r3ByContext.get(contextId);
    const combined = r3 ?? r2;
    const combinedBasis = combinedDecisionBasis(r2, r3);
    if (String(chat.pmid ?? "") !== String(combined.pmid ?? "")) {
      throw new Error(`PMID identity differs for ${contextId}`);
    }

    const chatStatus = normalizedStatus(chat, "chatgpt");
    const combinedStatus = normalizedStatus(combined, "claude_gemini");
    const chatIncluded = isIncluded(chat, "chatgpt");
    const combinedIncluded = isIncluded(combined, "claude_gemini");
    const matrixKey = `${chatIncluded ? "include" : "exclude"}|${combinedIncluded ? "include" : "exclude"}`;
    statusMatrix.set(matrixKey, (statusMatrix.get(matrixKey) ?? 0) + 1);

    const base = {
      PMID: String(chat.pmid ?? combined.pmid ?? ""),
      first_author_last_name: firstAuthorLastName(chat.authors, combined.authors, r2.authors),
      exposure: String(chat.exposure ?? combined.exposure ?? ""),
      cancer: String(chat.cancer ?? combined.cancer ?? ""),
      context_id: contextId,
      chatgpt_source_generation: String(selected.selected_source ?? ""),
      claude_gemini_decision_basis: combinedBasis,
    };

    if (chatIncluded !== combinedIncluded) {
      const missing = chatIncluded ? "Claude+Gemini" : "ChatGPT";
      const included = chatIncluded ? "ChatGPT" : "Claude+Gemini";
      const excluded =
        missing === "Claude+Gemini"
          ? combinedBasis.startsWith("Gemini")
            ? "Claude+Gemini (Gemini R3)"
            : "Claude+Gemini (Claude R2)"
          : "ChatGPT";
      const exclusion = chatIncluded ? combined : chat;
      const exclusionCode = String(exclusion.exclusion_code ?? "");
      const exclusionReason = String(exclusion.reason ?? "");
      screeningRows.push({
        ...base,
        missing_from_pipeline: missing,
        included_by_pipeline: included,
        excluded_by_pipeline: excluded,
        chatgpt_status: chatStatus,
        claude_gemini_status: combinedStatus,
        exclusion_code: exclusionCode,
        exclusion_reason: exclusionReason,
        brief_explanation: screeningExplanation({
          missing,
          code: exclusionCode,
          reason: exclusionReason,
          combinedBasis,
        }),
      });
      continue;
    }

    if (!chatIncluded) {
      bothExcluded += 1;
      continue;
    }
    bothIncluded += 1;
    const chatExtraction = chat.extraction ?? {};
    const combinedExtraction = combined.extraction ?? {};
    const discrepantFields = [];
    if (!numbersEquivalent(chatExtraction.total_n, combinedExtraction.total_n)) {
      discrepantFields.push("sample_size");
    }
    const effectNumbersMatch = effectValueEquivalent(chatExtraction, combinedExtraction);
    if (!effectNumbersMatch) {
      discrepantFields.push("effect_size");
    }
    if (!uncertaintyEquivalent(chatExtraction, combinedExtraction)) {
      discrepantFields.push("confidence_interval_or_p_value");
    }
    if (!discrepantFields.length) continue;

    const chatEvidenceType = evidenceType(chatExtraction);
    const combinedEvidenceType = evidenceType(combinedExtraction);
    const preferredEvidenceType =
      chatEvidenceType === combinedEvidenceType
        ? chatEvidenceType
        : `${chatEvidenceType} / ${combinedEvidenceType}`;
    extractionRows.push({
      ...base,
      evidence_type: preferredEvidenceType,
      chatgpt_evidence_type: chatEvidenceType,
      claude_gemini_evidence_type: combinedEvidenceType,
      verbal_evidence_note: verbalEvidenceNote(chatExtraction, combinedExtraction),
      discrepant_fields: discrepantFields.join("; "),
      chatgpt_sample_size: chatExtraction.total_n,
      claude_gemini_sample_size: combinedExtraction.total_n,
      chatgpt_effect_type: chatExtraction.effect_type,
      claude_gemini_effect_type: combinedExtraction.effect_type,
      chatgpt_effect_size: chatExtraction.effect_size,
      claude_gemini_effect_size: combinedExtraction.effect_size,
      chatgpt_ci_lower: chatExtraction.ci_lower,
      chatgpt_ci_upper: chatExtraction.ci_upper,
      claude_gemini_ci_lower: combinedExtraction.ci_lower,
      claude_gemini_ci_upper: combinedExtraction.ci_upper,
      chatgpt_p_value: chatExtraction.p_value,
      claude_gemini_p_value: combinedExtraction.p_value,
      chatgpt_comparison: chatExtraction.comparison_type,
      claude_gemini_comparison: combinedExtraction.comparison_type,
      brief_explanation: extractionExplanation(
        discrepantFields,
        chatExtraction,
        combinedExtraction,
      ),
    });
  }

  return {
    screeningRows,
    extractionRows,
    summary: {
      scope: "shared_R1_PASS_contexts_only",
      compared_contexts: contextIds.length,
      distinct_pmids: new Set(r2Rows.map((row) => String(row.pmid ?? ""))).size,
      gemini_overrides: r3ByContext.size,
      status_matrix: Object.fromEntries([...statusMatrix.entries()].sort()),
      both_included_contexts: bothIncluded,
      both_excluded_contexts: bothExcluded,
      screening_discrepancies: screeningRows.length,
      extraction_discrepancies: extractionRows.length,
      numeric_extraction_discrepancies: extractionRows.filter((row) =>
        row.discrepant_fields
          .split("; ")
          .some((field) => [
            "sample_size",
            "effect_size",
            "confidence_interval_or_p_value",
          ].includes(field)),
      ).length,
      effect_measure_type_only_discrepancies: extractionRows.filter(
        (row) => row.discrepant_fields === "effect_measure_type",
      ).length,
      screening_missing_from_pipeline: Object.fromEntries(
        ["ChatGPT", "Claude+Gemini"].map((pipeline) => [
          pipeline,
          screeningRows.filter((row) => row.missing_from_pipeline === pipeline).length,
        ]),
      ),
      screening_rows_missing_first_author: screeningRows.filter(
        (row) => !row.first_author_last_name,
      ).length,
      extraction_rows_missing_first_author: extractionRows.filter(
        (row) => !row.first_author_last_name,
      ).length,
      extraction_discrepancy_field_counts: Object.fromEntries(
        [
          "sample_size",
          "effect_size",
          "effect_measure_type",
          "confidence_interval_or_p_value",
        ].map((field) => [
          field,
          extractionRows.filter((row) => row.discrepant_fields.split("; ").includes(field)).length,
        ]),
      ),
      ignored_shared_r1_fail_contexts: selectedRows.length - r1PassRows.length,
      numeric_equivalence_rule:
        "numeric equality after parsing, 1e-12 relative tolerance for binary floating-point noise, and obvious rounding equivalence when a more precise value rounds to an otherwise identical value reported to at least hundredths precision; one-decimal effect rounding is accepted only when CI or p-value agrees",
      uncertainty_rule:
        "agreement when the complete CI pair matches OR the p-value matches; both entirely absent also agrees",
      verbal_evidence_rule:
        "missing exposure-measurement excerpt is annotated only on otherwise quantitative-discrepancy rows and is never itself a discrepancy",
    },
  };
}

function main() {
  const args = parseArguments(process.argv.slice(2));
  const comparison = buildComparison();
  if (args.write) {
    fs.mkdirSync(args.outputDir, { recursive: true });
    fs.writeFileSync(
      path.join(args.outputDir, "screening_discrepancies.csv"),
      csvText(SCREENING_COLUMNS, comparison.screeningRows),
      "utf8",
    );
    fs.writeFileSync(
      path.join(args.outputDir, "extraction_discrepancies.csv"),
      csvText(EXTRACTION_COLUMNS, comparison.extractionRows),
      "utf8",
    );
  }
  const report = args.diagnostics
    ? { summary: comparison.summary, diagnostics: quantitativeDiagnostics(comparison.extractionRows) }
    : comparison.summary;
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

main();
