"""Reconcile uncertain CDS submissions before allowing any new POST."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from climate_reanalysis.download import DATASET, build_request


class SubmissionRecoveryError(RuntimeError):
    """Only locally constructed, credential-free recovery messages."""


def iso(now):
    return now.astimezone(timezone.utc).isoformat()


def utc(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    # CDS receipts also use timezone-free timestamps, representing UTC.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def fingerprint(request):
    payload = json.dumps([DATASET, request], sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def _matching_jobs(client, request, earliest, known_ids, max_pages):
    """Read every page, including terminal jobs; incomplete scans prove nothing."""
    page = client.get_jobs(limit=100, sortby='-created')
    if page is None:
        raise ValueError('missing CDS job list')
    pages = 0
    seen = set()
    matches = []
    while page is not None:
        pages += 1
        if pages > max_pages:
            raise ValueError('job list page limit reached')
        entries = page.json['jobs']
        if not isinstance(entries, list):
            raise ValueError('invalid CDS job list')
        for entry in entries:
            request_id = entry['jobID']
            if not isinstance(request_id, str) or not request_id:
                raise ValueError('invalid CDS request id')
            if request_id in seen or request_id in known_ids:
                continue
            seen.add(request_id)
            if entry['processID'] != DATASET:
                continue
            if utc(entry['created']) < earliest:
                continue
            # Read receipt metadata without waiting for execution/results.
            receipt = client.get_remote(request_id).json
            if receipt['processID'] != DATASET:
                raise ValueError('CDS list and receipt disagree')
            params = receipt['metadata']['request']['ids']
            if not isinstance(params, dict):
                raise ValueError('invalid CDS request parameters')
            if params != request:
                continue
            if receipt['status'] not in {'accepted', 'running', 'successful',
                                          'failed', 'rejected', 'dismissed', 'deleted'}:
                raise ValueError('unknown CDS job status')
            if utc(receipt['created']) < earliest:
                raise ValueError('CDS list and receipt timestamps disagree')
            matches.append(request_id)
        page = page.next
    return matches


def reconcile(settings, task, name, journal, accounts_factory, now):
    """True when attached or confirmed absent; False during the visibility grace."""
    job = task['jobs'][name]
    check = job.setdefault('reconciliation', {
        'started_at': job.get('submission_started_at', iso(now)),
        'absent_checks': 0, 'outcome': 'waiting',
    })
    if check.get('next_check_at') and now < utc(check['next_check_at']):
        if check.get('last_error'):
            raise SubmissionRecoveryError(check['last_error'])
        return False
    check['next_check_at'] = iso(now + timedelta(minutes=settings.submission_reconcile_interval_minutes))
    check['last_checked_at'] = iso(now)
    journal.save()
    request = build_request(name, task['month'], settings.area)
    try:
        if job.get('request_sha256', fingerprint(request)) != fingerprint(request):
            raise ValueError('pending submission request changed')
        accounts = accounts_factory()
        if job['account'] not in accounts:
            raise ValueError('original CDS account is not configured')
        # Do not attach earlier attempts or jobs already owned by another task.
        known_ids = {id for t in journal.data['tasks'].values()
                     for j in t.get('jobs', {}).values()
                     for id in [j.get('request_id'), *j.get('previous_ids', [])] if id}
        # Legacy intents have no submission timestamp; retain their conservative
        # task creation bound, and start their absence grace at first reconciliation.
        earliest = utc(job.get('submission_started_at', task['created_at'])) - timedelta(minutes=5)
        matches = _matching_jobs(accounts[job['account']], request, earliest,
                                 known_ids, settings.submission_reconcile_max_pages)
    except Exception as exc:
        check.update(absent_checks=0, outcome='lookup_failed',
                     last_error=f'{name}: CDS submission lookup failed ({type(exc).__name__}); '
                                'automatic reconciliation will retry; attach-job remains available')
        check.pop('first_absent_at', None)
        journal.save()
        raise SubmissionRecoveryError(check['last_error']) from None
    if len(matches) > 1:
        check.update(absent_checks=0, outcome='ambiguous', matching_ids=matches,
                     last_error=f'{name}: multiple matching CDS submissions; '
                                'not resubmitting; verify and use attach-job')
        check.pop('first_absent_at', None)
        journal.save()
        raise SubmissionRecoveryError(check['last_error'])
    check.pop('last_error', None)
    check.pop('matching_ids', None)
    if matches:
        job.update(request_id=matches[0], status='submitted')
        check.update(outcome='attached', request_id=matches[0])
        journal.save()  # receipt durable before any poll/download
        return True
    check.setdefault('first_absent_at', iso(now))
    check['absent_checks'] += 1
    check['outcome'] = 'waiting'
    old_enough = now >= utc(check['started_at']) + timedelta(minutes=settings.submission_reconcile_grace_minutes)
    if old_enough and check['absent_checks'] >= settings.submission_reconcile_absence_checks:
        check['outcome'] = 'confirmed_absent'
        job['status'] = 'failed'  # existing admission path performs the new submit
        journal.save()
        return True
    journal.save()
    return False
