"""Replay scoped curator publication exclusions after model and human decisions."""
from __future__ import annotations
import copy
import json
from functools import lru_cache
from pathlib import Path
from scripts import astra_fulltext_common as common

VERSION = 'curator-publication-dedup-2026-09-14-v1'
CHECKPOINT = 'curator_dedup/current_decisions.json'


@lru_cache(maxsize=4)
def _load(path, mtime_ns, size):
    packet = json.loads(Path(path).read_text())
    if packet.get('version') != VERSION or packet.get('decisions_sha256') != common.digest(packet.get('decisions')):
        raise ValueError('Invalid curator publication-dedup checkpoint: ' + path)
    for decision in packet['decisions']:
        if (decision.get('action') != 'exclude_duplicate_publication'
                or any(not decision.get(key) for key in ('exposure', 'disease', 'endpoint', 'retained_pmid', 'rejected_pmid'))
                or decision['retained_pmid'] == decision['rejected_pmid']):
            raise ValueError('Invalid scoped curator publication decision: ' + path)
    return packet


def checkpoint(combined_root=None):
    root = Path(combined_root) if combined_root is not None else Path(__file__).resolve().parents[1] / 'combined'
    path = root / CHECKPOINT
    if not path.exists():
        return {}
    stat = path.stat()
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def replay_record(record, chatgpt, claude_gemini, combined_root):
    """Keep the curator exclusion authoritative even after rebuilding raw tracks."""
    saved = checkpoint(combined_root).get('record_snapshots', {}).get(record['context_id'])
    if not saved:
        return record
    if saved['source_fingerprint'] != common.source_digest(chatgpt, claude_gemini):
        raise ValueError('Curator-deduplicated source changed; explicit reconciliation required: ' + record['context_id'])
    restored = saved['record']
    if (saved['record_sha256'] != common.digest(restored) or not common.is_human(restored)
            or restored.get('final_status') != 'EXCLUDE'
            or restored.get('decision_authority') != 'human_curator_dedup'):
        raise ValueError('Invalid curator-dedup record snapshot: ' + record['context_id'])
    return copy.deepcopy(restored)


def filter_rows(rows, exposure, disease, endpoint='Incidence', combined_root=None):
    """Remove a rejected article only in its authorized displayed scope.

    Child-disease source records remain intact. Filtering the composed parent
    prevents those records from restoring the article in the rejected scope.
    """
    rejected = {decision['rejected_pmid'] for decision in checkpoint(combined_root).get('decisions', [])
                if (decision['exposure'], decision['disease'], decision['endpoint']) == (exposure, disease, endpoint)}
    if not rejected:
        return rows
    return [row for row in rows if str(row.get('PMID') or row.get('pmid') or '').strip() not in rejected]
