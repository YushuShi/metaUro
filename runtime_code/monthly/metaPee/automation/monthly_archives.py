"""Lossless Git-sized storage for new Neuro monthly audit packets only.

Canonical packets retain their original bytes and filenames at runtime. Git
stores deterministic gzip chunks bound to those bytes by an append-only index.
Historical evidence and UI assets are never archive candidates.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

INDEX = "data/monthly_automation/archives.json"
ARCHIVE_ROOT = "data/monthly_automation/archives"
THRESHOLD = 90 * 1024 * 1024
CHUNK_SIZE = 50 * 1024 * 1024


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def confined(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
        raise RuntimeError("Archive paths must remain inside the repository")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise RuntimeError("Archive paths must remain inside the repository")
    return result


def packet_path(relative):
    return bool(re.fullmatch(r"data/monthly_(?:updates|reports)/\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}/.+", relative))


def load_index(root):
    path = confined(root, INDEX)
    index = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "entries": {}}
    if index.get("schema_version") != 1 or not isinstance(index.get("entries"), dict):
        raise RuntimeError("Invalid monthly archive index")
    for relative, entry in index["entries"].items():
        confined(root, relative)
        if not packet_path(relative) or not isinstance(entry, dict):
            raise RuntimeError("Only date-scoped monthly packets may be archived")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))) or not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise RuntimeError("Invalid canonical archive binding")
        chunks = entry.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise RuntimeError("Monthly archive chunks are missing")
        for number, chunk in enumerate(chunks):
            expected = f"{ARCHIVE_ROOT}/{entry['sha256']}.gz.part{number:05d}"
            if chunk.get("path") != expected or not re.fullmatch(r"[0-9a-f]{64}", str(chunk.get("sha256", ""))):
                raise RuntimeError("Invalid monthly archive chunk binding")
            confined(root, expected)
    return index


def enabled(config):
    return config.get("adapter") == "neuro"


def pack_run(root, config, run_id, *, threshold=THRESHOLD, chunk_size=CHUNK_SIZE):
    """Prepare archives, keeping canonical originals on disk for validation."""
    if not enabled(config):
        return {}
    root = Path(root).resolve()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}", run_id):
        raise RuntimeError("Invalid archive run identifier")
    if threshold < 1 or chunk_size < 1:
        raise ValueError("Archive size limits must be positive")
    index = load_index(root)
    for base in ("data/monthly_updates", "data/monthly_reports"):
        directory = confined(root, f"{base}/{run_id}")
        if not directory.is_dir():
            continue
        for packet in sorted(directory.rglob("*")):
            if packet.is_symlink() or not packet.is_file() or packet.stat().st_size <= threshold:
                continue
            relative = str(packet.relative_to(root))
            confined(root, relative)
            original_sha = checksum(packet)
            previous = index["entries"].get(relative)
            if previous is not None:
                if original_sha != previous["sha256"] or packet.stat().st_size != previous["size"]:
                    raise RuntimeError("An indexed canonical monthly packet changed")
                verify_entry(root, previous)
                continue
            tracked = subprocess.run(["git", "cat-file", "-e", f"HEAD:{relative}"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if tracked.returncode == 0:
                raise RuntimeError("Refusing to convert an already committed historical packet")
            with tempfile.TemporaryDirectory() as temporary:
                compressed = Path(temporary) / "packet.gz"
                with packet.open("rb") as source, compressed.open("wb") as destination:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                chunks = []
                with compressed.open("rb") as source:
                    number = 0
                    while block := source.read(chunk_size):
                        relative_chunk = f"{ARCHIVE_ROOT}/{original_sha}.gz.part{number:05d}"
                        path = confined(root, relative_chunk)
                        block_sha = hashlib.sha256(block).hexdigest()
                        if path.exists() and checksum(path) != block_sha:
                            raise RuntimeError("An immutable monthly archive chunk changed")
                        if not path.exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(block)
                        chunks.append({"path": relative_chunk, "sha256": block_sha, "size": len(block)})
                        number += 1
                if checksum(packet) != original_sha:
                    raise RuntimeError("Canonical packet changed while its archive was prepared")
                index["entries"][relative] = {"sha256": original_sha, "size": packet.stat().st_size, "compression": "gzip", "chunks": chunks}
    if index["entries"]:
        path = confined(root, INDEX)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
        temporary.replace(path)
    return {relative: entry for relative, entry in index["entries"].items() if f"/{run_id}/" in relative}


def verify_entry(root, entry):
    for chunk in entry["chunks"]:
        path = confined(root, chunk["path"])
        if not path.is_file() or path.stat().st_size != chunk["size"] or checksum(path) != chunk["sha256"]:
            raise RuntimeError("A monthly archive chunk is missing or changed")


def hydrate_run(root, config, run_id):
    """Restore exact canonical bytes before native receipt/hash validation."""
    if not enabled(config):
        return 0
    root = Path(root).resolve()
    index = load_index(root)
    restored = 0
    for relative, entry in index["entries"].items():
        if f"/{run_id}/" not in relative:
            continue
        destination = confined(root, relative)
        verify_entry(root, entry)
        if destination.exists():
            if destination.stat().st_size != entry["size"] or checksum(destination) != entry["sha256"]:
                raise RuntimeError("Existing canonical monthly packet differs from its archive")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            compressed = Path(temporary) / "archive.gz"
            with compressed.open("wb") as output:
                for chunk in entry["chunks"]:
                    with confined(root, chunk["path"]).open("rb") as source:
                        shutil.copyfileobj(source, output, 1024 * 1024)
            unpacked = Path(temporary) / "packet"
            with gzip.open(compressed, "rb") as source, unpacked.open("wb") as output:
                remaining = entry["size"]
                while block := source.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(block)
                    if remaining < 0:
                        raise RuntimeError("Monthly archive expands beyond its recorded canonical size")
                    output.write(block)
            if unpacked.stat().st_size != entry["size"] or checksum(unpacked) != entry["sha256"]:
                raise RuntimeError("Hydrated monthly packet does not match its original hash")
            unpacked.replace(destination)
        restored += 1
    return restored
