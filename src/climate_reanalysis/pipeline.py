"""Bounded, restartable ERA5 monthly sync. No remote polling loop or destructive refresh."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import shutil
import uuid

from climate_reanalysis import jobs, publication, store
from climate_reanalysis.manifest import manifest_path, manifest_sha256
from climate_reanalysis.period import ready_months
from climate_reanalysis.refresh import _month_end
from climate_reanalysis.restore import restore_one
from climate_reanalysis.state import Journal, atomic_json, producer_lock
from climate_reanalysis.variables import VAR_SPECS

FINISHED = {'complete', 'waiting_final'}


def _iso(now):
    return now.astimezone(timezone.utc).isoformat()


def task_settings(settings, task):
    base = settings.store_dir / '.pipeline' / 'work' / task['id']
    return settings.model_copy(update={'raw_dir': settings.raw_dir / '.pipeline' / task['id'],
                                      'manifest_dir': settings.manifest_dir / '.pipeline' / task['id'],
                                      'store_dir': base / 'candidate', 'cold_dir': None})


def _create_task(month, variables, kind, now, base_path):
    return {'id': uuid.uuid4().hex, 'month': month, 'kind': kind,
            'variables': variables, 'stage': 'pending_download', 'jobs': {},
            'created_at': _iso(now), 'updated_at': _iso(now), 'attempts': 0,
            'next_retry_at': None, 'last_error': None,
            'base_path': str(base_path.resolve()) if base_path.exists() else None}


def _plan(settings, journal, start, refresh, now):
    index = publication.read_index(settings.store_dir)
    tasks = journal.data['tasks']
    active = sum(t['stage'] not in FINISHED for t in tasks.values())
    months = ready_months(start, now.date(), settings.publish_lag_days)
    if refresh:
        # Refresh candidates may predate --start. Never lose them from the work set.
        months = sorted(set(months) | set(index['months']) | {
            p.name[:6] for p in settings.store_dir.glob('*/*_era5.zarr')})
    for month in months:
        if active >= settings.max_active_months:
            break
        old = tasks.get(month)
        if old and old['stage'] not in FINISHED:
            continue
        if old and old.get('next_retry_at') and _iso(now) < old['next_retry_at']:
            continue
        path = publication.resolve_month(settings.store_dir, month, index)
        prov = publication.provenance(path)
        missing = [v for v in VAR_SPECS if not prov.get(v, {}).get('manifest_sha256')]
        stale = []
        if refresh and (now.date() - _month_end(month)).days > settings.refresh_after_days:
            stale = [v for v in VAR_SPECS if '0005' in prov.get(v, {}).get('expver', [])]
        if missing and month >= start and month in ready_months(start, now.date(), settings.publish_lag_days):
            variables, kind = missing, 'update'
        elif stale:
            variables, kind = stale, 'refresh'
        else:
            continue
        if old:
            journal.archive(old)
        tasks[month] = _create_task(month, variables, kind, now, path)
        active += 1
        journal.save()


def _prepare_candidate(settings, task, work, journal):
    if task.get('candidate_ready'):
        return
    # This is exclusively our unpublished work directory. Interrupted copies are
    # rebuilt; no source/published file is ever modified or hardlinked.
    if work.store_dir.exists():
        shutil.rmtree(work.store_dir)
    if task['base_path']:
        shutil.copytree(task['base_path'], store.store_path(work.store_dir, task['month']))
    task['candidate_ready'] = True
    journal.save()


def _advance(settings, task, journal, accounts, active, now):
    work = task_settings(settings, task)
    entry = publication.read_index(settings.store_dir)['months'].get(task['month'])
    if entry and entry['task_id'] == task['id']:
        # Index switch survived a crash even if the final journal update did not.
        publication.resolve_month(settings.store_dir, task['month'])
        task.update(stage='complete', last_error=None, next_retry_at=None)
        journal.save()
        return
    if task['stage'] == 'pending_download':
        if not jobs.advance_downloads(work, task, journal, accounts, active):
            return
        task['stage'] = 'pending_restore'
        journal.save()
    if task['stage'] == 'pending_restore':
        _prepare_candidate(settings, task, work, journal)
        for var in task['variables']:
            restore_one(work, manifest_path(work.manifest_dir, task['month'], var))
        task['stage'] = 'pending_validation'
        journal.save()
    if task['stage'] == 'pending_validation':
        expected = {v: manifest_sha256(manifest_path(work.manifest_dir, task['month'], v))
                    for v in task['variables']}
        # Crash after candidate rename but before index switch: validate the moved
        # generation again, then publish the same generation without redownloading.
        candidate_root = work.store_dir
        moved = settings.store_dir / '.versions' / task['month'] / task['id']
        if moved.exists():
            candidate_root = moved
        validation = publication.validate_month(
            store.store_path(candidate_root, task['month']), task['month'], expected)
        task['validation'] = validation
        if task['kind'] == 'refresh' and any(
            validation['expver'][v] != ['0001'] for v in task['variables']
        ):
            task.update(stage='waiting_final', last_error=None,
                        next_retry_at=_iso(now + timedelta(days=settings.final_retry_days)))
            journal.save()
            return
        publication.publish(settings.store_dir, task, candidate_root, validation, _iso(now))
        task.update(stage='complete', last_error=None, next_retry_at=None)
        journal.save()


def sync(settings, start: str, *, refresh: bool = False, now=None, accounts_factory=None):
    """One tick; returns machine-readable status. Pending CDS jobs are normal success."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # Validate start before creating files; use a canonical UTC date in all decisions.
    datetime.strptime(start, '%Y%m')
    if len(start) != 6:
        raise ValueError('start must be YYYYMM')
    if settings.concurrency < 1:
        raise ValueError('sync concurrency must be positive')
    store.check_area_matches_grid(settings.area)
    with producer_lock(settings.store_dir):
        journal = Journal(settings.store_dir)
        layout = {key: str(getattr(settings, key).resolve())
                  for key in ('raw_dir', 'manifest_dir', 'store_dir')}
        if journal.data.get('layout', layout) != layout:
            raise ValueError('pipeline paths changed; restore previous config before resuming')
        journal.data['layout'] = layout
        _plan(settings, journal, start, refresh, now)
        clients = None

        def accounts():
            nonlocal clients
            if clients is None:
                clients = (accounts_factory or jobs.make_accounts)(settings)
            return clients

        active = jobs.active_jobs(journal.data['tasks'].values())
        for task in journal.data['tasks'].values():
            if task['stage'] in FINISHED:
                continue
            if task.get('next_retry_at') and _iso(now) < task['next_retry_at']:
                continue
            task['updated_at'] = _iso(now)
            try:
                _advance(settings, task, journal, accounts, active, now)
                # A successful intermediate poll clears the previous transient error.
                if task['stage'] not in FINISHED:
                    task.update(last_error=None, next_retry_at=None)
            except Exception as exc:
                task['attempts'] += 1
                # Remote exceptions can contain signed URLs or credentials. Persist
                # only type; deterministic local ValueErrors carry our own messages.
                message = str(exc) if type(exc) is ValueError else type(exc).__name__
                task['last_error'] = message if 'http' not in message.lower() else type(exc).__name__
                hours = min(24, settings.retry_hours * 2 ** min(task['attempts'] - 1, 8))
                task['next_retry_at'] = _iso(now + timedelta(hours=hours))
            journal.save()
        journal.data['last_run_at'] = _iso(now)
        journal.save()
        result = status(settings)
        atomic_json(settings.store_dir / '.pipeline' / '_runs' / f'{uuid.uuid4().hex}.json',
                    {k: result[k] for k in ('last_run_at', 'counts', 'failed', 'publication_revision')})
        return result


def status(settings):
    data = Journal.read(settings.store_dir)
    index = publication.read_index(settings.store_dir)
    return {'schema_version': 1, 'last_run_at': data.get('last_run_at'),
            'publication_revision': index['revision'],
            'published_months': index['months'],
            'counts': dict(Counter(t['stage'] for t in data['tasks'].values())),
            'failed': sum(bool(t.get('last_error')) for t in data['tasks'].values()),
            'tasks': data['tasks']}


def attach_job(settings, month, variable, request_id):
    """Operator reconciles an uncertain submit after checking the CDS account."""
    with producer_lock(settings.store_dir):
        journal = Journal(settings.store_dir)
        task = journal.data['tasks'][month]
        job = task['jobs'][variable]
        if job['status'] != 'submitting':
            raise ValueError('only uncertain submissions can be attached')
        accounts = jobs.make_accounts(settings)
        remote = accounts[job['account']].get_remote(request_id)
        # Check receipt before attaching: never silently consume an unrelated job.
        collection = remote.collection_id
        request = remote.request
        from climate_reanalysis.download import build_request, DATASET
        expected = build_request(variable, month, settings.area)
        if collection != DATASET or request != expected:
            raise ValueError('CDS receipt does not match the pending request')
        job.update(request_id=request_id, status='submitted')
        task.update(last_error=None, next_retry_at=None)
        journal.save()


def retry_submission(settings, month, variable):
    """Only after operator confirms no remote job exists for the uncertain intent."""
    with producer_lock(settings.store_dir):
        journal = Journal(settings.store_dir)
        task = journal.data['tasks'][month]
        job = task['jobs'][variable]
        if job['status'] != 'submitting':
            raise ValueError('only uncertain submissions can be reset')
        job['status'] = 'failed'
        job['operator_confirmed_absent'] = True
        task.update(last_error=None, next_retry_at=None)
        journal.save()


def retry_task(settings, month, *, redownload=False):
    """Recover failed processing; optional fresh download retains the previous attempt."""
    with producer_lock(settings.store_dir):
        journal = Journal(settings.store_dir)
        task = journal.data['tasks'][month]
        if not task.get('last_error') or task['stage'] in FINISHED:
            raise ValueError('retry-task requires a failed unfinished task')
        if any(j['status'] not in jobs.TERMINAL | {'downloaded'} for j in task['jobs'].values()):
            raise ValueError('active or uncertain CDS jobs remain; resume or reconcile them first')
        if redownload:
            journal.archive(task)
            base = publication.resolve_month(settings.store_dir, month)
            journal.data['tasks'][month] = _create_task(
                month, task['variables'], task['kind'], datetime.now(timezone.utc), base)
        else:
            if (settings.store_dir / '.versions' / month / task['id']).exists():
                raise ValueError('version is already staged for publication; resume with sync')
            if task['stage'] not in {'pending_restore', 'pending_validation'}:
                raise ValueError('download failure requires --redownload or normal timed retry')
            task.update(stage='pending_restore', candidate_ready=False,
                        last_error=None, next_retry_at=None)
        journal.save()
