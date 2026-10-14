"""Real tiny Zarr/NetCDF + fake CDS. No network and no user datasets."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import numpy as np
import pytest
import zarr

from climate_reanalysis import jobs, pipeline, publication, store
from climate_reanalysis.settings import Settings
from climate_reanalysis.state import Journal, PipelineBusy, producer_lock
from climate_reanalysis.variables import VAR_SPECS
from tests.conftest import make_raw_nc

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    lat = np.array([15., 15.25]); lon = np.array([70., 70.25])
    monkeypatch.setattr(store, 'TARGET_LAT', lat)
    monkeypatch.setattr(store, 'TARGET_LON', lon)
    monkeypatch.setattr('climate_reanalysis.restore.TARGET_LAT', lat)
    monkeypatch.setattr('climate_reanalysis.restore.TARGET_LON', lon)
    settings = Settings(store_dir=tmp_path/'store', raw_dir=tmp_path/'raw',
                        manifest_dir=tmp_path/'manifest', area=(15.25,70,15,70.25))
    return settings


class Remote:
    def __init__(self, id, request, status='successful'):
        self.request_id = id
        self.request = request
        self.status = status
        from climate_reanalysis.download import DATASET
        self.collection_id = DATASET
        self.created_at = NOW

    @property
    def json(self):
        return {'processID': self.collection_id, 'status': self.status,
                'created': self.created_at.isoformat(),
                'metadata': {'request': {'ids': self.request}}}

    def get_results(self):
        return SimpleNamespace(asset={'href': self.request_id})


class Account:
    def __init__(self, initial='successful'):
        self.remotes = {}
        self.submits = 0
        self.resumes = []
        self.initial = initial
        self.list_calls = 0

    def get_jobs(self, **kwargs):
        self.list_calls += 1
        return SimpleNamespace(json={'jobs': [
            {'jobID': r.request_id, 'processID': r.collection_id,
             'created': r.created_at.isoformat()}
            for r in self.remotes.values()]}, next=None)

    def submit(self, dataset, request):
        self.submits += 1
        remote = Remote(str(self.submits), request, self.initial)
        self.remotes[remote.request_id] = remote
        return remote

    def get_remote(self, id):
        self.resumes.append(id)
        return self.remotes[id]


@pytest.fixture
def fake(setup, monkeypatch):
    account = Account()
    control = {'expver': '0005', 'value': 288., 'fail': False}

    def transfer(url, target, proxy, **kw):
        assert kw['strict_size']
        if control['fail']:
            raise OSError('transfer interrupted')
        request = account.remotes[url].request
        month = request['year'][0]+request['month'][0]
        from climate_reanalysis.period import month_hours
        make_raw_nc(target, month, request['variable'][0], control['value'],
                    lat=store.TARGET_LAT, lon=store.TARGET_LON,
                    expver=[control['expver']]*len(month_hours(month)))
    monkeypatch.setattr(jobs.cds, 'curl_download', transfer)
    return account, control


def tick(s, fake, now=NOW, **kw):
    return pipeline.sync(s, '202608', now=now, accounts_factory=lambda _: {'account':fake[0]}, **kw)


def test_publish_full_month_and_idempotent_tick(setup, fake):
    result = tick(setup, fake)
    assert result['counts'] == {'complete': 1}
    assert fake[0].submits == 10
    ref = publication.resolve_month(setup.store_dir, '202608')
    assert '.versions' in str(ref)
    assert not store.store_path(setup.store_dir, '202608').exists()
    assert set(publication.provenance(ref)) == set(VAR_SPECS)
    revision = result['publication_revision']
    result2 = tick(setup, fake)
    assert result2['publication_revision'] == revision
    assert fake[0].submits == 10


def test_cds_queue_resume_and_account_budget(setup, fake):
    fake[0].initial = 'accepted'
    first = tick(setup, fake)
    assert first['counts'] == {'pending_download': 1}
    assert fake[0].submits == setup.concurrency
    second = tick(setup, fake)
    assert fake[0].submits == setup.concurrency
    assert second['publication_revision'] is None
    fake[0].initial = 'successful'
    for r in fake[0].remotes.values():
        r.status = 'successful'
    assert tick(setup, fake)['counts'] == {'complete': 1}
    assert fake[0].submits == 10
    assert fake[0].resumes


def test_transfer_failure_retains_id_and_retry_time(setup, fake):
    fake[1]['fail'] = True
    first = tick(setup, fake)
    task = first['tasks']['202608']
    assert task['stage'] == 'pending_download' and task['last_error']
    assert task['next_retry_at'] == (NOW+timedelta(hours=1)).isoformat()
    assert fake[0].submits == 1
    tick(setup, fake)  # not due: no requests
    assert fake[0].submits == 1
    fake[1]['fail'] = False
    assert tick(setup, fake, now=NOW+timedelta(hours=1))['counts'] == {'complete': 1}
    assert fake[0].submits == 10  # no second submit for interrupted transfer


def test_transfer_backoff_is_independent_and_resets_on_progress(setup, fake, monkeypatch):
    # Seed the same state as an old submission failure followed by a transfer.
    fake[1]['fail'] = True
    first = tick(setup, fake)
    job = next(iter(first['tasks']['202608']['jobs'].values()))
    original_id = job['request_id']
    journal = Journal(setup.store_dir)
    journal.data['tasks']['202608'].update(attempts=9, next_retry_at=None)
    journal.save()
    original_transfer = jobs.cds.curl_download
    progressing = False

    def interrupted(*_args, **_kw):
        raise jobs.cds.TransferError(18, 40, 100, grew=progressing)

    monkeypatch.setattr(jobs.cds, 'curl_download', interrupted)
    current = NOW
    for wait in (10, 20, 40, 60):
        result = tick(setup, fake, now=current)
        task = result['tasks']['202608']
        assert task['attempts'] == 9
        assert task['next_retry_at'] == (current + timedelta(minutes=wait)).isoformat()
        assert 'rc=18' in task['last_error'] and '40/100' in task['last_error']
        assert next(iter(task['jobs'].values()))['request_id'] == original_id
        assert fake[0].submits == 1
        tick(setup, fake, now=current + timedelta(minutes=wait-1))
        assert fake[0].submits == 1
        current += timedelta(minutes=wait)
    progressing = True
    task = tick(setup, fake, now=current)['tasks']['202608']
    assert task['next_retry_at'] == (current + timedelta(minutes=10)).isoformat()
    assert task['transfer_attempts'] == 0
    fake[1]['fail'] = False
    monkeypatch.setattr(jobs.cds, 'curl_download', original_transfer)
    result = tick(setup, fake, now=current + timedelta(minutes=10))
    assert result['counts'] == {'complete': 1}
    assert result['tasks']['202608']['transfer_attempts'] == 0
    assert fake[0].submits == 10


def test_uncertain_submit_not_resubmitted_after_one_absence_check(setup, fake, monkeypatch):
    def lost_reply(*args):
        fake[0].submits += 1
        raise OSError('reply lost')
    monkeypatch.setattr(fake[0], 'submit', lost_reply)
    assert tick(setup, fake)['failed'] == 1
    later = tick(setup, fake, now=NOW+timedelta(hours=2))
    assert fake[0].submits == 1
    job = next(iter(later['tasks']['202608']['jobs'].values()))
    assert job['status'] == 'submitting' and job['reconciliation']['absent_checks'] == 1


def test_refresh_keeps_old_version_until_final_validated(setup, fake):
    old = tick(setup, fake)
    oldpath = publication.resolve_month(setup.store_dir, '202608')
    oldbytes = (oldpath/'zarr.json').read_bytes()
    later = datetime(2026,12,11,tzinfo=timezone.utc)
    # Scope only existing old month by raising lag (no Sep/Oct/Nov additions).
    s = setup.model_copy(update={'publish_lag_days': 100})
    wait = tick(s, fake, now=later, refresh=True)
    assert wait['counts'] == {'waiting_final': 1}
    assert wait['publication_revision'] == old['publication_revision']
    assert (oldpath/'zarr.json').read_bytes() == oldbytes
    submitted = fake[0].submits
    tick(s, fake, now=later+timedelta(days=1), refresh=True)
    assert fake[0].submits == submitted
    fake[1].update(expver='0001', value=299.)
    result = tick(s, fake, now=later+timedelta(days=7), refresh=True)
    newpath = publication.resolve_month(setup.store_dir, '202608')
    assert result['counts'] == {'complete': 1}
    assert newpath != oldpath and oldpath.exists()
    assert float(zarr.open_group(str(oldpath),mode='r')['t2m'][0,0,0]) == 288.
    assert float(zarr.open_group(str(newpath),mode='r')['t2m'][0,0,0]) == 299.


def test_nan_candidate_never_published(setup, fake):
    fake[1]['value'] = np.nan
    result = tick(setup, fake)
    assert result['tasks']['202608']['stage'] == 'pending_validation'
    assert result['failed'] == 1
    assert result['publication_revision'] is None


def test_restore_failure_retries_without_redownload(setup, fake, monkeypatch):
    original = pipeline.restore_one
    monkeypatch.setattr(pipeline, 'restore_one', lambda *_: (_ for _ in ()).throw(OSError()))
    result = tick(setup, fake)
    assert result['tasks']['202608']['stage'] == 'pending_restore'
    monkeypatch.setattr(pipeline, 'restore_one', original)
    result = tick(setup, fake, now=NOW+timedelta(hours=1))
    assert result['counts'] == {'complete': 1}
    assert fake[0].submits == 10


def partial_results(fake, monkeypatch, names):
    """A successful CDS response can be a readable but incomplete month."""
    import xarray as xr
    original = jobs.cds.curl_download
    control = {'names': set(names)}

    def transfer(url, target, proxy, **kw):
        original(url, target, proxy, **kw)
        name = fake[0].remotes[url].request['variable'][0]
        if name in control['names']:
            with xr.open_dataset(target) as ds:
                part = ds.isel(valid_time=slice(None, -23)).load()
            part.to_netcdf(target)

    monkeypatch.setattr(jobs.cds, 'curl_download', transfer)
    return control


def test_incomplete_cds_result_retries_only_bad_files_and_preserves_evidence(setup, fake, monkeypatch):
    from climate_reanalysis.download import raw_path
    from climate_reanalysis.manifest import manifest_path
    names = {name for spec in VAR_SPECS.values() for name in spec.cds}
    good = '10m_v_component_of_wind'
    control = partial_results(fake, monkeypatch, names - {good})
    first = tick(setup, fake)
    task = first['tasks']['202608']
    old_id = task['id']
    assert task['stage'] == 'pending_download'
    assert len(task['incomplete_files']) == 9
    assert task['next_retry_at'] == (NOW + timedelta(hours=6)).isoformat()
    assert first['publication_revision'] is None
    work = pipeline.task_settings(setup, task)
    old_good_bytes = raw_path(work.raw_dir, '202608', good).read_bytes()
    old_bad_bytes = raw_path(work.raw_dir, '202608', '2m_temperature').read_bytes()
    assert not manifest_path(work.manifest_dir, '202608', 'ssrd').exists()
    assert not manifest_path(work.manifest_dir, '202608', 'si10').exists()
    tick(setup, fake, now=NOW + timedelta(hours=5))
    assert fake[0].submits == 10
    control['names'].clear()
    result = tick(setup, fake, now=NOW + timedelta(hours=6))
    new = result['tasks']['202608']
    assert new['stage'] == 'complete' and new['id'] != old_id
    assert fake[0].submits == 19
    assert raw_path(work.raw_dir, '202608', good).read_bytes() == old_good_bytes
    assert raw_path(work.raw_dir, '202608', '2m_temperature').read_bytes() == old_bad_bytes
    assert (setup.store_dir / '.pipeline/history' / f'{old_id}.json').exists()
    assert new['jobs'][good]['request_id'] == task['jobs'][good]['request_id']
    assert set(new['incomplete_recoveries'][-1]['reused_files']) == {good}
    assert result['published_months']['202608']['hours'] == 744


def test_old_pending_restore_incomplete_results_enter_content_recovery(setup, fake, monkeypatch):
    from climate_reanalysis.manifest import write_manifest
    from climate_reanalysis.download import raw_path
    partial_results(fake, monkeypatch, {'2m_temperature'})
    result = tick(setup, fake)
    journal = Journal(setup.store_dir)
    task = journal.data['tasks']['202608']
    work = pipeline.task_settings(setup, task)
    # Reproduce the deployed pre-fix state, including an invalid ready manifest.
    write_manifest(work.manifest_dir, '202608', 't2m',
                   [raw_path(work.raw_dir, '202608', '2m_temperature')], setup.area)
    task.update(stage='pending_restore', attempts=15, last_error='old time-axis error',
                next_retry_at=None)
    task.pop('incomplete_files', None)
    journal.save()
    result = tick(setup, fake)
    recovered = result['tasks']['202608']
    assert recovered['stage'] == 'pending_download'
    assert list(recovered['incomplete_files']) == ['2m_temperature']
    assert recovered['next_retry_at'] == (NOW + timedelta(hours=6)).isoformat()
    assert fake[0].submits == 10
    assert result['publication_revision'] is None


def test_content_recovery_does_not_abandon_accepted_jobs(setup, fake, monkeypatch):
    from climate_reanalysis.manifest import manifest_path
    partial_results(fake, monkeypatch, {'surface_solar_radiation_downwards'})
    original = fake[0].submit

    def submit(dataset, request):
        remote = original(dataset, request)
        if request['variable'] == ['10m_v_component_of_wind']:
            remote.status = 'accepted'
        return remote

    monkeypatch.setattr(fake[0], 'submit', submit)
    first = tick(setup, fake)
    task = first['tasks']['202608']
    accepted = task['jobs']['10m_v_component_of_wind']['request_id']
    assert task['stage'] == 'pending_download' and not task['last_error']
    work = pipeline.task_settings(setup, task)
    assert not manifest_path(work.manifest_dir, '202608', 'ssrd').exists()
    fake[0].remotes[accepted].status = 'successful'
    result = tick(setup, fake)
    assert fake[0].submits == 10
    assert result['tasks']['202608']['jobs']['10m_v_component_of_wind']['request_id'] == accepted
    assert result['tasks']['202608']['incomplete_files']


def test_operator_can_advance_targeted_content_recovery_after_inspection(setup, fake, monkeypatch):
    control = partial_results(fake, monkeypatch, {'2m_temperature'})
    first = tick(setup, fake)
    old = first['tasks']['202608']['id']
    pipeline.retry_task(setup, '202608')
    fresh = Journal.read(setup.store_dir)['tasks']['202608']
    assert fresh['id'] != old and fresh['stage'] == 'pending_download'
    assert fresh['next_retry_at'] is None
    assert fake[0].submits == 10  # the recovery command itself performs no POST
    control['names'].clear()
    result = tick(setup, fake)
    assert result['counts'] == {'complete': 1}
    assert fake[0].submits == 11


def test_reused_file_integrity_failure_keeps_old_attempt_and_issues_no_requests(setup, fake, monkeypatch):
    from climate_reanalysis.download import raw_path
    partial_results(fake, monkeypatch, {'2m_temperature'})
    first = tick(setup, fake)
    task = first['tasks']['202608']
    work = pipeline.task_settings(setup, task)
    raw_path(work.raw_dir, '202608', 'surface_pressure').write_bytes(b'corrupt test file')
    result = tick(setup, fake, now=NOW + timedelta(hours=6))
    assert result['tasks']['202608']['id'] == task['id']
    assert 'downloaded file changed' in result['tasks']['202608']['last_error']
    assert fake[0].submits == 10
    assert result['publication_revision'] is None


def test_failed_month_does_not_block_older_refresh_with_one_slot(setup, fake):
    # August becomes due for refresh in December; September holds the sole slot
    # in a processing backoff, with no CDS requests still running.
    tick(setup, fake)
    journal = Journal(setup.store_dir)
    base = store.store_path(setup.store_dir, '202609')
    task = pipeline._create_task('202609', ['t2m'], 'update', NOW, base)
    later = datetime(2026, 12, 11, tzinfo=timezone.utc)
    task.update(stage='pending_restore', last_error='incomplete month',
                next_retry_at=(later + timedelta(hours=24)).isoformat())
    journal.data['tasks']['202609'] = task
    journal.save()
    fake[0].initial = 'accepted'
    settings = setup.model_copy(update={'max_active_months': 1, 'publish_lag_days': 100})
    result = tick(settings, fake, now=later, refresh=True)
    assert result['tasks']['202608']['kind'] == 'refresh'
    assert result['tasks']['202608']['stage'] == 'pending_download'
    assert fake[0].submits == 14
    # The older failed task becoming due must not create requests on top of the
    # still running refresh month, even though per-account slots remain free.
    journal = Journal(setup.store_dir)
    journal.data['tasks']['202609'].update(stage='pending_download', next_retry_at=None)
    journal.save()
    tick(settings, fake, now=later, refresh=True)
    assert fake[0].submits == 14


def test_crash_after_index_switch_recovers_without_new_generation(setup, fake, monkeypatch):
    original = publication.publish
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit('power off')
    monkeypatch.setattr(publication, 'publish', crash)
    with pytest.raises(SystemExit):
        tick(setup, fake)
    revision = publication.read_index(setup.store_dir)['revision']
    monkeypatch.setattr(publication, 'publish', original)
    result = tick(setup, fake)
    assert result['counts'] == {'complete': 1}
    assert result['publication_revision'] == revision
    assert fake[0].submits == 10


def test_crash_after_candidate_move_before_index(setup, fake, monkeypatch):
    original = publication.atomic_json
    def crash(path, data):
        if path.name == 'publication.json':
            raise SystemExit('power off')
        original(path, data)
    monkeypatch.setattr(publication, 'atomic_json', crash)
    with pytest.raises(SystemExit):
        tick(setup, fake)
    assert not (setup.store_dir/'publication.json').exists()
    monkeypatch.setattr(publication, 'atomic_json', original)
    assert tick(setup, fake)['counts'] == {'complete':1}
    assert fake[0].submits == 10


def test_lock_and_read_only_status(tmp_path):
    s = Settings(store_dir=tmp_path/'absent')
    assert pipeline.status(s)['tasks'] == {}
    assert not s.store_dir.exists()
    with producer_lock(s.store_dir):
        with pytest.raises(PipelineBusy):
            with producer_lock(s.store_dir):
                pass


def test_corrupt_state_and_index_fail_closed(tmp_path):
    root = tmp_path/'s'
    (root/'.pipeline').mkdir(parents=True)
    (root/'.pipeline'/'state.json').write_text('{')
    with pytest.raises(json.JSONDecodeError):
        Journal(root)
    (root/'publication.json').write_text('{')
    with pytest.raises(json.JSONDecodeError):
        publication.resolve_month(root,'202608')


def test_legacy_writers_refused_after_publication(setup, fake):
    tick(setup, fake)
    with pytest.raises(ValueError,match='legacy'):
        publication.refuse_legacy_write(setup.store_dir)


def test_budget_is_global_across_months_and_accounts(setup):
    accounts = {'a':Account('accepted'),'b':Account('accepted')}
    s = setup.model_copy(update={'concurrency':2})
    result = pipeline.sync(s,'202607',now=NOW,accounts_factory=lambda _:accounts)
    assert result['counts'] == {'pending_download':2}
    assert [a.submits for a in accounts.values()] == [2,2]
    pipeline.sync(s,'202607',now=NOW,accounts_factory=lambda _:dict(reversed(list(accounts.items()))))
    assert [a.submits for a in accounts.values()] == [2,2]


def test_attach_checks_request_without_waiting_for_results(setup,fake,monkeypatch):
    def lost_reply(*args):
        raise OSError()
    monkeypatch.setattr(fake[0],'submit',lost_reply)
    tick(setup,fake)
    from climate_reanalysis.download import build_request,DATASET
    name = next(iter(Journal.read(setup.store_dir)['tasks']['202608']['jobs']))
    remote = Remote('attached',build_request(name,'202608',setup.area),'accepted')
    remote.collection_id = DATASET
    fake[0].remotes['attached'] = remote
    monkeypatch.setattr(jobs,'make_accounts',lambda s:{'account':fake[0]})
    pipeline.attach_job(setup,'202608',name,'attached')
    job = Journal.read(setup.store_dir)['tasks']['202608']['jobs'][name]
    assert job['request_id'] == 'attached'
    assert job['status'] == 'submitted'


def test_refresh_before_update_start_is_not_lost(setup,fake):
    tick(setup,fake)
    fake[1]['expver'] = '0001'
    s = setup.model_copy(update={'publish_lag_days':100})
    result = pipeline.sync(s,'202609',refresh=True,now=datetime(2026,12,11,tzinfo=timezone.utc),
                           accounts_factory=lambda _:{'account':fake[0]})
    assert result['tasks']['202608']['kind'] == 'refresh'
    assert result['tasks']['202608']['stage'] == 'complete'


def test_published_full_array_corruption_detected(setup,fake):
    tick(setup,fake)
    p = publication.resolve_month(setup.store_dir,'202608')
    # A value far from the first timestep must be read by validation too.
    g = zarr.open_group(str(p),mode='r+')
    g['t2m'][-2,-1,-1] = np.nan
    with pytest.raises(ValueError,match='non-finite'):
        publication.validate_month(p,'202608',{})


def test_config_path_changes_rejected_before_any_requests(setup,fake):
    fake[0].initial = 'accepted'
    tick(setup,fake)
    s = setup.model_copy(update={'raw_dir':setup.raw_dir/'different'})
    with pytest.raises(ValueError,match='paths changed'):
        tick(s,fake)
    assert fake[0].submits == 4


def test_retry_candidate_without_new_download(setup,fake,monkeypatch):
    validate = publication.validate_month
    monkeypatch.setattr(publication,'validate_month',lambda *_:(_ for _ in ()).throw(ValueError('broken candidate')))
    tick(setup,fake)
    pipeline.retry_task(setup,'202608')
    monkeypatch.setattr(publication,'validate_month',validate)
    assert tick(setup,fake)['counts'] == {'complete':1}
    assert fake[0].submits == 10


def test_retry_rejected_job_cleans_only_own_id(setup,fake,monkeypatch):
    fake[0].initial = 'rejected'
    deleted=[]
    monkeypatch.setattr(fake[0],'delete',lambda id:deleted.append(id),raising=False)
    result = tick(setup,fake)
    assert result['failed'] == 1
    fake[0].initial = 'successful'
    result = tick(setup,fake,now=NOW+timedelta(hours=1))
    assert result['counts'] == {'complete':1}
    assert deleted == ['1'] and fake[0].submits == 11


def test_unknown_submission_reset_requires_explicit_operator_action(setup,fake,monkeypatch):
    submit = fake[0].submit
    monkeypatch.setattr(fake[0],'submit',lambda *_:(_ for _ in ()).throw(OSError()))
    result = tick(setup,fake)
    name=next(iter(result['tasks']['202608']['jobs']))
    pipeline.retry_submission(setup,'202608',name)
    monkeypatch.setattr(fake[0],'submit',submit)
    assert tick(setup,fake)['counts'] == {'complete':1}


def test_managed_state_blocks_legacy_even_before_first_publication(setup,fake):
    fake[0].initial='accepted'
    tick(setup,fake)
    with pytest.raises(ValueError,match='legacy'):
        publication.refuse_legacy_write(setup.store_dir)


def test_invalid_task_identity_cannot_escape_work_directory(tmp_path):
    root=tmp_path/'s'
    (root/'.pipeline').mkdir(parents=True)
    (root/'.pipeline'/'state.json').write_text(json.dumps({'schema_version':1,'tasks':{
        '202608':{'month':'202608','id':'../../outside'}}}))
    with pytest.raises(ValueError,match='identity'):
        Journal(root)


def test_empty_sync_does_not_disable_legacy_commands(setup):
    pipeline.sync(setup,'209901',now=NOW,
                  accounts_factory=lambda _:pytest.fail('no network'))
    publication.refuse_legacy_write(setup.store_dir)


def test_uncertain_submit_reserves_account_capacity_in_same_tick(setup):
    account=Account()
    def uncertain(*args):
        account.submits += 1
        raise OSError('lost reply')
    account.submit=uncertain
    s=setup.model_copy(update={'concurrency':1})
    pipeline.sync(s,'202607',now=NOW,accounts_factory=lambda _:{'account':account})
    assert account.submits == 1


def test_remote_status_404_marks_deleted_for_retry(setup,fake):
    import requests
    fake[0].initial='accepted'
    tick(setup,fake)
    class GoneRemote:
        @property
        def status(self):
            raise requests.HTTPError(response=SimpleNamespace(status_code=404))
    fake[0].remotes['1']=GoneRemote()
    result=tick(setup,fake)
    job=next(iter(result['tasks']['202608']['jobs'].values()))
    assert job['status'] == 'deleted'


def test_lost_submit_reply_recovers_matching_job_on_second_page(setup, fake, monkeypatch):
    """Remote acceptance survives a lost reply; resume it through publication."""
    submit = fake[0].submit
    lost = {'once': True}
    def lose_reply(dataset, request):
        remote = submit(dataset, request)
        if lost['once']:
            lost['once'] = False
            raise OSError('reply lost')
        return remote
    monkeypatch.setattr(fake[0], 'submit', lose_reply)
    first = tick(setup, fake)
    assert first['failed'] == 1
    page2 = fake[0].get_jobs()
    monkeypatch.setattr(fake[0], 'get_jobs', lambda **kw:
                        SimpleNamespace(json={'jobs': []}, next=page2))
    result = tick(setup, fake, now=NOW+timedelta(minutes=10))
    assert result['counts'] == {'complete': 1}
    assert fake[0].submits == 10  # no duplicate of the accepted first request
    task = result['tasks']['202608']
    job = next(iter(task['jobs'].values()))
    assert job['request_id'] == '1'
    assert job['reconciliation']['outcome'] == 'attached'
    assert publication.validate_month(publication.resolve_month(setup.store_dir, '202608'),
                                      '202608', {})['hours'] == 744


def test_absent_last_submit_retries_after_grace_without_redownloading(setup, fake, monkeypatch):
    """The actual incident: keep nine files, recover only the missing tenth."""
    submit = fake[0].submit
    def absent_once(dataset, request):
        if fake[0].submits == 9:
            fake[0].submits += 1
            raise OSError('connection failed before acceptance')
        return submit(dataset, request)
    monkeypatch.setattr(fake[0], 'submit', absent_once)
    first = tick(setup, fake)
    kept = {n: dict(j) for n,j in first['tasks']['202608']['jobs'].items()
            if j['status'] == 'downloaded'}
    assert len(kept) == 9
    for minutes in (10, 20):
        result = tick(setup, fake, now=NOW+timedelta(minutes=minutes))
        assert fake[0].submits == 10 and result['publication_revision'] is None
    result = tick(setup, fake, now=NOW+timedelta(minutes=30))
    assert result['counts'] == {'complete': 1} and fake[0].submits == 11
    task = result['tasks']['202608']
    assert all(task['jobs'][n] == j for n,j in kept.items())
    last = task['jobs']['10m_v_component_of_wind']
    assert last['submission_recoveries'][-1]['outcome'] == 'confirmed_absent'


def test_lookup_failure_never_counts_as_absence_or_leaks_secrets(setup, fake, monkeypatch):
    monkeypatch.setattr(fake[0], 'submit', lambda *_: (_ for _ in ()).throw(OSError()))
    tick(setup, fake)
    monkeypatch.setattr(fake[0], 'get_jobs', lambda **kw:
                        (_ for _ in ()).throw(OSError('https://secret?token=private')))
    result = tick(setup, fake, now=NOW+timedelta(hours=2))
    task = result['tasks']['202608']
    job = next(iter(task['jobs'].values()))
    assert job['status'] == 'submitting'
    assert job['reconciliation']['absent_checks'] == 0
    assert 'lookup failed' in task['last_error']
    assert 'secret' not in json.dumps(result) and 'private' not in json.dumps(result)


def _uncertain_first(setup, fake, monkeypatch, *, accepted=False):
    submit = fake[0].submit
    once = {'fail': True}
    def lose_reply(dataset, request):
        if once['fail']:
            once['fail'] = False
            if accepted:
                submit(dataset, request)
            else:
                fake[0].submits += 1
            raise OSError('connection lost')
        return submit(dataset, request)
    monkeypatch.setattr(fake[0], 'submit', lose_reply)
    tick(setup, fake)
    return next(iter(Journal.read(setup.store_dir)['tasks']['202608']['jobs']))


def test_ambiguous_receipts_pause_until_unique_without_new_post(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch, accepted=True)
    fake[0].remotes['other'] = Remote('other', fake[0].remotes['1'].request)
    for minutes in (10, 20, 120):
        result = tick(setup, fake, now=NOW+timedelta(minutes=minutes))
        job = result['tasks']['202608']['jobs'][name]
        assert job['status'] == 'submitting'
        assert job['reconciliation']['outcome'] == 'ambiguous'
        assert 'multiple matching' in result['tasks']['202608']['last_error']
        assert fake[0].submits == 1 and result['publication_revision'] is None
    del fake[0].remotes['other']
    assert tick(setup, fake, now=NOW+timedelta(minutes=130))['counts'] == {'complete': 1}
    assert fake[0].submits == 10


def test_match_before_failed_next_page_is_not_attached(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch, accepted=True)
    page = fake[0].get_jobs()
    class BrokenPagination:
        json = page.json
        @property
        def next(self):
            raise OSError('later page failed')
    monkeypatch.setattr(fake[0], 'get_jobs', lambda **kw: BrokenPagination())
    result = tick(setup, fake, now=NOW+timedelta(hours=2))
    job = result['tasks']['202608']['jobs'][name]
    assert job['status'] == 'submitting' and 'request_id' not in job
    assert job['reconciliation']['outcome'] == 'lookup_failed' and fake[0].submits == 1


@pytest.mark.parametrize('bad_page', [None, {'jobs': None}, {'missing_jobs': []}])
def test_invalid_list_does_not_prove_absence(setup, fake, monkeypatch, bad_page):
    name = _uncertain_first(setup, fake, monkeypatch)
    page = None if bad_page is None else SimpleNamespace(json=bad_page, next=None)
    monkeypatch.setattr(fake[0], 'get_jobs', lambda **kw: page)
    result = tick(setup, fake, now=NOW+timedelta(hours=2))
    assert result['tasks']['202608']['jobs'][name]['reconciliation']['absent_checks'] == 0
    assert fake[0].submits == 1 and result['failed'] == 1


def test_page_limit_never_proves_absence(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch)
    page = SimpleNamespace(json={'jobs': []}, next=SimpleNamespace(json={'jobs': []}, next=None))
    monkeypatch.setattr(fake[0], 'get_jobs', lambda **kw: page)
    result = tick(setup.model_copy(update={'submission_reconcile_max_pages': 1}),
                  fake, now=NOW+timedelta(hours=2))
    assert result['tasks']['202608']['jobs'][name]['reconciliation']['absent_checks'] == 0
    assert fake[0].submits == 1 and result['failed'] == 1


def test_lookup_failure_resets_absence_and_checks_are_spaced(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch)
    tick(setup, fake, now=NOW+timedelta(minutes=10))
    tick(setup, fake, now=NOW+timedelta(minutes=11))
    assert fake[0].list_calls == 1
    list_jobs = fake[0].get_jobs
    monkeypatch.setattr(fake[0], 'get_jobs', lambda **kw: (_ for _ in ()).throw(OSError()))
    failed = tick(setup, fake, now=NOW+timedelta(minutes=20))
    assert failed['tasks']['202608']['jobs'][name]['reconciliation']['absent_checks'] == 0
    monkeypatch.setattr(fake[0], 'get_jobs', list_jobs)
    tick(setup, fake, now=NOW+timedelta(minutes=30))
    assert fake[0].submits == 1  # one successful absent lookup since the failure
    assert tick(setup, fake, now=NOW+timedelta(minutes=40))['counts'] == {'complete': 1}
    assert fake[0].submits == 11


def test_legacy_unknown_bypasses_old_backoff_and_recovers_receipt(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch, accepted=True)
    journal = Journal(setup.store_dir)
    task = journal.data['tasks']['202608']
    task['next_retry_at'] = (NOW+timedelta(days=1)).isoformat()
    job = task['jobs'][name]
    for key in ['submission_started_at', 'request_sha256', 'reconciliation']:
        job.pop(key, None)
    journal.save()
    result = tick(setup, fake, now=NOW+timedelta(minutes=10))
    assert result['counts'] == {'complete': 1} and fake[0].submits == 10


def test_legacy_absent_intent_starts_new_grace_before_retry(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch)
    journal = Journal(setup.store_dir)
    job = journal.data['tasks']['202608']['jobs'][name]
    for key in ['submission_started_at', 'request_sha256', 'reconciliation']:
        job.pop(key, None)
    journal.save()
    for minutes in (120, 130, 140):
        tick(setup, fake, now=NOW+timedelta(minutes=minutes))
        assert fake[0].submits == 1
    assert tick(setup, fake, now=NOW+timedelta(minutes=150))['counts'] == {'complete': 1}


def test_old_identical_receipt_and_other_area_are_not_reused(setup, fake, monkeypatch):
    _uncertain_first(setup, fake, monkeypatch, accepted=True)
    fake[0].remotes['1'].created_at = NOW-timedelta(days=1)
    wrong = dict(fake[0].remotes['1'].request, area=[55, 70, 15, 140])
    fake[0].remotes['other-area'] = Remote('other-area', wrong)
    for minutes in (10, 20):
        result = tick(setup, fake, now=NOW+timedelta(minutes=minutes))
        assert fake[0].submits == 1 and result['publication_revision'] is None
    result = tick(setup, fake, now=NOW+timedelta(minutes=30))
    assert result['counts'] == {'complete': 1} and fake[0].submits == 11
    assert '1' not in fake[0].resumes  # old receipts are excluded before a GET
    assert all(j['request_id'] not in {'1', 'other-area'}
               for j in result['tasks']['202608']['jobs'].values())


def test_changed_pending_request_pauses_before_query(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch)
    journal = Journal(setup.store_dir)
    journal.data['tasks']['202608']['jobs'][name]['request_sha256'] = 'different-request'
    journal.save()
    result = tick(setup, fake, now=NOW+timedelta(hours=2))
    assert result['failed'] == 1 and fake[0].submits == 1 and fake[0].list_calls == 0


def test_reconciliation_uses_original_account_only(setup, fake, monkeypatch):
    _uncertain_first(setup, fake, monkeypatch, accepted=True)
    other = Account()
    other.get_jobs = lambda **kw: pytest.fail('must not inspect unrelated account')
    other.submit = lambda *_: pytest.fail('must not submit on unrelated account')
    result = pipeline.sync(setup, '202608', now=NOW+timedelta(minutes=10),
                           accounts_factory=lambda _: {'account': fake[0], 'other': other})
    assert result['counts'] == {'complete': 1} and fake[0].submits == 10


def test_long_tick_records_actual_submit_time(setup, fake, monkeypatch):
    current = {'now': NOW}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current['now']
    monkeypatch.setattr(pipeline, 'datetime', Clock)
    submit = fake[0].submit
    def slow_then_fail(dataset, request):
        if fake[0].submits == 8:
            current['now'] = NOW+timedelta(minutes=31)
        if fake[0].submits == 9:
            fake[0].submits += 1
            raise OSError()
        return submit(dataset, request)
    monkeypatch.setattr(fake[0], 'submit', slow_then_fail)
    result = pipeline.sync(setup, '202608', accounts_factory=lambda _: {'account': fake[0]})
    job = result['tasks']['202608']['jobs']['10m_v_component_of_wind']
    assert job['submission_started_at'] == (NOW+timedelta(minutes=31)).isoformat()
    assert result['tasks']['202608']['next_retry_at'] == (NOW+timedelta(minutes=41)).isoformat()
    for minutes in (41, 51):
        tick(setup, fake, now=NOW+timedelta(minutes=minutes))
        assert fake[0].submits == 10


def test_crash_after_recovered_id_save_resumes_without_duplicate(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch, accepted=True)
    save = Journal.save
    def power_off(journal):
        save(journal)
        job = journal.data['tasks']['202608']['jobs'][name]
        if job.get('reconciliation', {}).get('outcome') == 'attached':
            raise SystemExit('power off after receipt save')
    monkeypatch.setattr(Journal, 'save', power_off)
    with pytest.raises(SystemExit):
        tick(setup, fake, now=NOW+timedelta(minutes=10))
    saved = Journal.read(setup.store_dir)['tasks']['202608']['jobs'][name]
    assert saved['request_id'] == '1' and saved['status'] == 'submitted'
    assert fake[0].submits == 1
    monkeypatch.setattr(Journal, 'save', save)
    result = tick(setup, fake, now=NOW+timedelta(minutes=11))
    assert result['counts'] == {'complete': 1} and fake[0].submits == 10


def test_crash_after_absence_save_rechecks_before_delayed_post(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch)
    for minutes in (10, 20):
        tick(setup, fake, now=NOW+timedelta(minutes=minutes))
    save = Journal.save
    def power_off(journal):
        save(journal)
        job = journal.data['tasks']['202608']['jobs'][name]
        if job.get('reconciliation', {}).get('outcome') == 'confirmed_absent':
            raise SystemExit('power off before retry admission')
    monkeypatch.setattr(Journal, 'save', power_off)
    with pytest.raises(SystemExit):
        tick(setup, fake, now=NOW+timedelta(minutes=30))
    assert fake[0].submits == 1
    monkeypatch.setattr(Journal, 'save', save)
    from climate_reanalysis.download import build_request
    # The original request appears while the producer is down.
    fake[0].remotes['late'] = Remote('late', build_request(name, '202608', setup.area))
    result = tick(setup, fake, now=NOW+timedelta(minutes=40))
    assert result['counts'] == {'complete': 1} and fake[0].submits == 10
    assert result['tasks']['202608']['jobs'][name]['request_id'] == 'late'


def test_confirmed_absence_respects_budget_and_rechecks_when_slot_frees(setup, fake, monkeypatch):
    from collections import Counter
    from climate_reanalysis.download import build_request
    name = _uncertain_first(setup, fake, monkeypatch)
    for minutes in (10, 20):
        tick(setup, fake, now=NOW+timedelta(minutes=minutes))
    s = setup.model_copy(update={'concurrency': 1})
    journal = Journal(s.store_dir)
    task = journal.data['tasks']['202608']
    full = Counter(account=2)  # the uncertain intent plus another active job
    assert not jobs.advance_downloads(pipeline.task_settings(s, task), task, journal,
                                     lambda: {'account': fake[0]}, full,
                                     now=NOW+timedelta(minutes=30))
    assert full['account'] == 1 and fake[0].submits == 1
    assert task['jobs'][name]['status'] == 'failed'
    fake[0].remotes['late'] = Remote('late', build_request(name, '202608', s.area))
    assert tick(s, fake, now=NOW+timedelta(minutes=40))['counts'] == {'complete': 1}
    assert fake[0].submits == 10


def test_malformed_receipt_pauses_and_never_proves_absence(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch, accepted=True)
    monkeypatch.setattr(fake[0], 'get_remote', lambda _: SimpleNamespace(
        json={'processID': fake[0].remotes['1'].collection_id, 'metadata': {}}))
    result = tick(setup, fake, now=NOW+timedelta(hours=2))
    job = result['tasks']['202608']['jobs'][name]
    assert job['status'] == 'submitting' and job['reconciliation']['absent_checks'] == 0
    assert job['reconciliation']['outcome'] == 'lookup_failed'
    assert fake[0].submits == 1 and result['failed'] == 1


def test_recovered_terminal_job_retries_without_deleting_receipt(setup, fake, monkeypatch):
    name = _uncertain_first(setup, fake, monkeypatch, accepted=True)
    fake[0].remotes['1'].status = 'rejected'
    monkeypatch.setattr(fake[0], 'delete', lambda _: pytest.fail('must not delete recovered job'),
                        raising=False)
    result = tick(setup, fake, now=NOW+timedelta(minutes=10))
    job = result['tasks']['202608']['jobs'][name]
    assert job['status'] == 'rejected' and job['request_id'] == '1'
    assert fake[0].submits == 1 and result['failed'] == 1
    result = tick(setup, fake, now=NOW+timedelta(minutes=70))
    assert result['counts'] == {'complete': 1} and fake[0].submits == 11
    retried = result['tasks']['202608']['jobs'][name]
    assert retried['previous_ids'] == ['1']
    assert retried['submission_recoveries'][-1]['outcome'] == 'attached'


def test_invalid_submit_receipt_is_reconciled_without_duplicate(setup, fake, monkeypatch):
    submit = fake[0].submit
    def invalid_receipt(dataset, request):
        remote = submit(dataset, request)
        return SimpleNamespace(request_id=None) if fake[0].submits == 1 else remote
    monkeypatch.setattr(fake[0], 'submit', invalid_receipt)
    first = tick(setup, fake)
    job = next(iter(first['tasks']['202608']['jobs'].values()))
    assert job['status'] == 'submitting' and 'request_id' not in job
    result = tick(setup, fake, now=NOW+timedelta(minutes=10))
    assert result['counts'] == {'complete': 1} and fake[0].submits == 10
