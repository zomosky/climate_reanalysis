"""Verify the actual installed ECMWF client's list/receipt response boundary."""
from datetime import datetime, timezone
import json

import ecmwf.datastores
import requests

from climate_reanalysis.download import DATASET, build_request
from climate_reanalysis.submission import _matching_jobs


def test_real_sdk_follows_pages_and_reads_receipt_without_waiting(monkeypatch):
    base = 'https://cds.example.invalid/api'
    request = build_request('2m_temperature', '202608', (55, 70, 15, 140))
    calls = []
    session = requests.Session()
    def respond(method, url, **kwargs):
        assert method.lower() == 'get'  # reconciliation must never submit/delete
        calls.append(url)
        if '/catalogue/' in url:
            payload = {'messages': []}
        elif url.endswith('/jobs'):
            assert kwargs['params'] == {'limit': 100, 'sortby': '-created'}
            payload = {'jobs': [], 'links': [{'rel': 'next',
                        'href': base+'/retrieve/v1/jobs?cursor=2'}]}
        elif url.endswith('/jobs?cursor=2'):
            payload = {'jobs': [{'jobID': 'recovered', 'processID': DATASET,
                                 'created': '2026-09-09T00:00:00'}], 'links': []}
        else:
            assert url.endswith('/jobs/recovered')
            payload = {'processID': DATASET, 'status': 'accepted',
                       'created': '2026-09-09T00:00:00',
                       'links': [{'rel': 'self', 'href': url}],
                       'metadata': {'request': {'ids': request}}}
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        response.url = url
        response.request = requests.Request(method, url).prepare()
        return response
    monkeypatch.setattr(session, 'request', respond)
    client = ecmwf.datastores.Client(url=base, key='test-only', session=session,
                                     maximum_tries=1)
    matches = _matching_jobs(client, request,
                             datetime(2026, 9, 8, tzinfo=timezone.utc), set(), 20)
    assert matches == ['recovered']
    assert sum('/retrieve/' in url for url in calls) == 4
