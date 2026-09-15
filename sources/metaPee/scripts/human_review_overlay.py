"""Restore source-verified human decisions after rebuilding model/Astra evidence."""
from __future__ import annotations
import copy
import json
from functools import lru_cache
from pathlib import Path
from scripts import astra_fulltext_common as common

CHECKPOINT = 'human_review/current_decisions.json'
VERSION = 'human-manual-review-2026-09-14-v1'


@lru_cache(maxsize=4)
def _checkpoint(path, mtime_ns, size):
    return json.loads(Path(path).read_text())


def replay_record(record, chatgpt, claude_gemini, combined_root):
    path = Path(combined_root) / CHECKPOINT
    if not path.exists():
        return record
    stat = path.stat()
    packet = _checkpoint(str(path), stat.st_mtime_ns, stat.st_size)
    saved = packet.get('record_snapshots', {}).get(record['context_id'])
    if not saved:
        return record
    if saved['source_fingerprint'] != common.source_digest(chatgpt, claude_gemini):
        raise ValueError('Human-reviewed source changed; explicit reconciliation required: ' + record['context_id'])
    restored = saved['record']
    if saved['record_sha256'] != common.digest(restored) or not common.is_human(restored):
        raise ValueError('Invalid human-review snapshot: ' + record['context_id'])
    return copy.deepcopy(restored)
