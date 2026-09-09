"""One scheduler tick of durable CDS jobs: submit once, poll later, resume by id."""
from __future__ import annotations

from collections import Counter
import hashlib

from climate_reanalysis import cds
from climate_reanalysis.download import DATASET, build_request, raw_path
from climate_reanalysis.manifest import _sha256_of, write_manifest
from climate_reanalysis.variables import cds_variables

TERMINAL = {'failed', 'rejected', 'dismissed', 'deleted'}


def make_accounts(settings):
    """No credentials/URLs in the journal. Stable account identity survives reordering."""
    import ecmwf.datastores
    accounts = {}
    for rc in list(settings.cdsapircs) or [settings.cdsapirc]:
        url, key = cds.read_cdsapirc(rc)
        identity = hashlib.sha256((url + '\0' + key).encode()).hexdigest()
        accounts[identity] = ecmwf.datastores.Client(
            url=url, key=key, session=cds._direct_session(),
            maximum_tries=1, retry_after=60,
        )
    return accounts


def advance_downloads(settings, task, journal, accounts_factory, active):
    """Returns True only when all task manifests exist and match downloaded bytes.

    A non-terminal remote job consumes an account slot across ticks/months.
    Transfer failure retains the same job id, never requeues its CDS request.
    """
    complete = True
    for var in task['variables']:
        files = []
        variable_complete = True
        for name in cds_variables(var):
            target = raw_path(settings.raw_dir, task['month'], name)
            job = task['jobs'].get(name)
            if job and job.get('status') == 'downloaded':
                if (not target.exists() or target.stat().st_size != job['size_bytes']
                        or _sha256_of(target) != job['sha256']):
                    raise ValueError(f'{task["month"]}/{name}: downloaded file changed')
                files.append(target)
                continue
            if job and job.get('status') == 'submitting':
                raise ValueError(f'{name}: submission outcome unknown; use attach-job')
            accounts = accounts_factory()
            if job is None or job.get('status') in TERMINAL:
                if job and job.get('request_id'):
                    # Only our own already-terminal job, never account-wide cleanup.
                    if job['account'] not in accounts:
                        raise ValueError('original CDS account is not configured')
                    try:
                        accounts[job['account']].delete(job['request_id'])
                    except Exception as exc:
                        if getattr(getattr(exc, 'response', None), 'status_code', None) not in (404, 410):
                            raise
                target.with_name(target.name + '.part').unlink(missing_ok=True)
                candidates = [a for a in accounts if active[a] < settings.concurrency]
                if not candidates:
                    variable_complete = False
                    continue
                account = min(candidates, key=lambda a: (active[a], a))
                attempts = (job or {}).get('attempts', 0) + 1
                history = list((job or {}).get('previous_ids', []))
                if job and job.get('request_id'):
                    history.append(job['request_id'])
                job = {'account': account, 'status': 'submitting', 'attempts': attempts,
                       'previous_ids': history}
                task['jobs'][name] = job
                journal.save()  # intent BEFORE the network call
                active[account] += 1  # uncertain submits also consume queue capacity
                remote = accounts[account].submit(
                    DATASET, build_request(name, task['month'], settings.area))
                job.update(request_id=remote.request_id, status='submitted')
                journal.save()  # receipt immediately; no poll before this save
            else:
                account = job['account']
                if account not in accounts:
                    raise ValueError(f'{name}: original CDS account is not configured')
                try:
                    remote = accounts[account].get_remote(job['request_id'])
                except Exception as exc:
                    if getattr(getattr(exc, 'response', None), 'status_code', None) in (404, 410):
                        job['status'] = 'deleted'
                        active[account] -= 1
                        journal.save()
                    raise
            try:
                status = remote.status
            except Exception as exc:
                # get_remote is lazy in the CDS client: absence usually surfaces
                # on status, not when the Remote object is constructed.
                if getattr(getattr(exc, 'response', None), 'status_code', None) in (404, 410):
                    job['status'] = 'deleted'
                    active[account] -= 1
                    journal.save()
                raise
            job['status'] = status
            journal.save()
            if status in TERMINAL:
                active[account] -= 1
                raise ValueError(f'{name}: CDS job {status}')
            if status != 'successful':
                variable_complete = False
                continue
            cds.curl_download(cds.results_url(remote.get_results()), target,
                              settings.dl_proxy, strict_size=True)
            job.update(status='downloaded', size_bytes=target.stat().st_size,
                       sha256=_sha256_of(target))
            active[account] -= 1
            journal.save()
            files.append(target)
        if variable_complete:
            # Do not rewrite completed_at/sha on every tick.
            from climate_reanalysis.manifest import manifest_path
            if not manifest_path(settings.manifest_dir, task['month'], var).exists():
                write_manifest(settings.manifest_dir, task['month'], var, files, settings.area)
        else:
            complete = False
    return complete


def active_jobs(tasks):
    return Counter(j['account'] for t in tasks for j in t.get('jobs', {}).values()
                   if j.get('status') not in TERMINAL | {'downloaded'})
