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

    def get_results(self):
        return SimpleNamespace(asset={'href': self.request_id})


class Account:
    def __init__(self, initial='successful'):
        self.remotes = {}
        self.submits = 0
        self.resumes = []
        self.initial = initial

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


def test_uncertain_submit_does_not_automatically_resubmit(setup, fake, monkeypatch):
    def lost_reply(*args):
        fake[0].submits += 1
        raise OSError('reply lost')
    monkeypatch.setattr(fake[0], 'submit', lost_reply)
    assert tick(setup, fake)['failed'] == 1
    later = tick(setup, fake, now=NOW+timedelta(hours=2))
    assert fake[0].submits == 1
    assert 'attach-job' in later['tasks']['202608']['last_error']


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
