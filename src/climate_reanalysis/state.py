"""Durable execution journal. Manifests and published provenance remain data truth."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import uuid


class PipelineBusy(RuntimeError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def producer_lock(root: Path):
    """Managed sync/recovery commands take this same non-blocking lock."""
    directory = Path(root) / '.pipeline'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'producer.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineBusy('ERA5 producer already running') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Journal:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / '.pipeline' / 'state.json'
        self.data = self.read(self.root)

    @staticmethod
    def read(root: Path) -> dict:
        path = Path(root) / '.pipeline' / 'state.json'
        if not path.exists():
            return {'schema_version': 1, 'tasks': {}}
        data = json.loads(path.read_text())
        if data.get('schema_version') != 1 or not isinstance(data.get('tasks'), dict):
            raise ValueError('unsupported or corrupt pipeline state')
        for month, task in data['tasks'].items():
            if (not isinstance(task, dict) or task.get('month') != month
                    or re.fullmatch(r'\d{6}', month) is None
                    or re.fullmatch(r'[0-9a-f]{32}', str(task.get('id', ''))) is None):
                raise ValueError('invalid pipeline task identity')
        return data

    def save(self):
        atomic_json(self.path, self.data)

    def archive(self, task):
        atomic_json(self.path.parent / 'history' / f"{task['id']}.json", task)
