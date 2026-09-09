"""Immutable month generations and an atomic, versioned publication index.

Readers snapshot publication.json once, then open the immutable relative paths.
The journal is never a readiness signal. Legacy stores remain untouched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import numpy as np
import zarr

from climate_reanalysis import store
from climate_reanalysis.period import month_hours
from climate_reanalysis.state import atomic_json
from climate_reanalysis.variables import VAR_SPECS

INDEX_NAME = 'publication.json'


def read_index(root: Path) -> dict:
    path = Path(root) / INDEX_NAME
    if not path.exists():
        return {'schema_version': 1, 'source': 'era5', 'revision': None, 'months': {}}
    data = json.loads(path.read_text())
    if (data.get('schema_version') != 1 or data.get('source') != 'era5'
            or not isinstance(data.get('months'), dict)):
        raise ValueError('unsupported or corrupt ERA5 publication index')
    return data


def resolve_month(root: Path, month: str, index: dict | None = None) -> Path:
    root = Path(root)
    index = read_index(root) if index is None else index
    entry = index['months'].get(month)
    if entry is None:
        return store.store_path(root, month)
    relative = Path(entry['path'])
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root.resolve() / '.versions'):
        raise ValueError('publication path must stay inside .versions')
    if not path.is_dir():
        raise FileNotFoundError(f'published ERA5 version missing: {path}')
    return path


def provenance(path: Path) -> dict:
    if not path.exists():
        return {}
    g = zarr.open_group(str(path), mode='r')
    return store._normalize_provenance(g.attrs.get('zrread_provenance', {}))


def validate_month(path: Path, month: str, expected: dict[str, str]) -> dict:
    """Read every value in bounded spatial blocks; metadata presence is insufficient."""
    g = zarr.open_group(str(path), mode='r')
    if g.metadata.consolidated_metadata is None:
        raise ValueError('candidate is not consolidated')
    hours = month_hours(month)
    np.testing.assert_array_equal(g['time'][:], hours.astype('int64'))
    np.testing.assert_array_equal(g['latitude'][:], store.TARGET_LAT)
    np.testing.assert_array_equal(g['longitude'][:], store.TARGET_LON)
    prov = provenance(path)
    shape = (len(hours), len(store.TARGET_LAT), len(store.TARGET_LON))
    versions = {}
    for var, spec in VAR_SPECS.items():
        if var not in prov or not prov[var].get('manifest_sha256'):
            raise ValueError(f'{month}/{var}: missing provenance')
        if var in expected and prov[var]['manifest_sha256'] != expected[var]:
            raise ValueError(f'{month}/{var}: manifest changed before publication')
        a = g[var]
        if a.shape != shape or a.attrs.get('units') != spec.units:
            raise ValueError(f'{month}/{var}: invalid shape or units')
        for y in range(0, shape[1], 32):
            for x in range(0, shape[2], 32):
                if not np.isfinite(a[:, y:y+32, x:x+32]).all():
                    raise ValueError(f'{month}/{var}: non-finite or missing data')
        versions[var] = prov[var].get('expver', [])
    quality = ('unknown' if any(not e for e in versions.values()) else
               'final' if all(e == ['0001'] for e in versions.values()) else 'preliminary')
    return {'hours': len(hours), 'variables': list(VAR_SPECS), 'expver': versions,
            'quality': quality}


def publish(root: Path, task: dict, candidate_root: Path, validation: dict, now: str):
    """Caller holds producer_lock. Index rename is the only visibility switch."""
    root = Path(root)
    index = read_index(root)
    previous = index['months'].get(task['month'])
    if previous and previous.get('task_id') == task['id']:
        return previous  # crash after index switch, before journal save
    generation = root / '.versions' / task['month'] / task['id']
    generation.parent.mkdir(parents=True, exist_ok=True)
    if not generation.exists():
        # Candidate and versions live under the same store root/filesystem.
        os.replace(candidate_root, generation)
    path = store.store_path(generation, task['month'])
    if not path.is_dir():
        raise ValueError('candidate generation missing')
    # Flush payload before publishing the pointer (also covers power loss).
    for parent, _, files in os.walk(generation):
        for name in files:
            with (Path(parent) / name).open('rb') as f:
                os.fsync(f.fileno())
        fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    for directory in (generation.parent, generation.parent.parent, root):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    entry = {'path': str(path.relative_to(root)), 'task_id': task['id'],
             'published_at': now, **validation}
    index['months'][task['month']] = entry
    index['revision'] = uuid.uuid4().hex
    atomic_json(root / INDEX_NAME, index)
    return entry


def refuse_legacy_write(root: Path):
    from climate_reanalysis.state import Journal
    if (Path(root) / INDEX_NAME).exists() or Journal.read(root)['tasks']:
        raise ValueError('managed archive: use sync; legacy in-place writers are disabled')
