"""Real curl against interrupted local HTTP; never submits to CDS."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
import subprocess

import pytest

from climate_reanalysis import cds


@pytest.fixture
def interrupted_server():
    payload = bytes(range(256)) * 384
    offsets = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            start = int(self.headers.get('Range', 'bytes=0-')[6:].split('-')[0])
            offsets.append(start)
            self.send_response(206 if start else 200)
            self.send_header('Content-Length', str(len(payload) - start))
            if start:
                self.send_header('Content-Range', f'bytes {start}-{len(payload)-1}/{len(payload)}')
            self.end_headers()
            self.wfile.write(payload[start:start + 32768])
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/asset', payload, offsets
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_disconnects_keep_every_new_byte(interrupted_server, tmp_path, monkeypatch):
    url, payload, offsets = interrupted_server
    target = tmp_path / 'data.nc'
    target.with_suffix('.nc.part').write_bytes(payload[:16384])
    monkeypatch.setattr(cds.time, 'sleep', lambda _: None)
    cds.curl_download(url, target, None, attempts=3, strict_size=True,
                      expected_bytes=len(payload))
    assert target.read_bytes() == payload
    assert offsets == [16384, 49152, 81920]
    assert not target.with_suffix('.nc.part').exists()


def test_sdk_size_avoids_unreliable_head_and_refreshes_same_job(tmp_path, monkeypatch):
    monkeypatch.setattr(cds, '_expected_size', lambda *_: pytest.fail('SDK size is available'))
    monkeypatch.setattr(cds.time, 'sleep', lambda _: None)
    configs = []

    def run(cmd, **kw):
        configs.append(kw['input'])
        part = Path(cmd[cmd.index('-o') + 1])
        with part.open('ab') as f:
            f.write(b'ab')
        return SimpleNamespace(returncode=18 if len(configs) == 1 else 0)

    refreshes = []

    def refresh():
        refreshes.append(True)
        return SimpleNamespace(asset={'href': 'https://obj/refreshed?secret=new'}, content_length=4)

    monkeypatch.setattr(subprocess, 'run', run)
    cds.curl_download('https://obj/original?secret=old', tmp_path / 'out', None,
                      strict_size=True, expected_bytes=4, refresh_results=refresh)
    assert len(refreshes) == 1
    assert 'original?secret=old' in configs[0] and 'refreshed?secret=new' in configs[1]


def test_transfer_errors_report_safe_progress_and_keep_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(cds.time, 'sleep', lambda _: None)
    target = tmp_path / 'out'
    part = tmp_path / 'out.part'
    part.write_bytes(b'ab')

    def run(cmd, **kw):
        assert 'https://obj/signed?token=secret' not in cmd
        assert 'user:password' not in ' '.join(cmd)
        assert '--retry' not in cmd and '--retry-all-errors' not in cmd
        assert '--max-time' in cmd
        with part.open('ab') as f:
            f.write(b'cd')
        return SimpleNamespace(returncode=56)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(cds.TransferError) as exc:
        cds.curl_download('https://obj/signed?token=secret', target,
                          'http://user:password@proxy:7890', attempts=1,
                          strict_size=True, expected_bytes=10)
    assert exc.value.grew and exc.value.have == 4
    assert '56' in str(exc.value) and '4/10' in str(exc.value)
    assert 'secret' not in str(exc.value) and 'password' not in str(exc.value)
    assert part.read_bytes() == b'abcd' and not target.exists()


def test_managed_oversize_stops_before_transfer_without_deleting(tmp_path, monkeypatch):
    part = tmp_path / 'out.part'
    part.write_bytes(b'too long')
    monkeypatch.setattr(subprocess, 'run', lambda *_a, **_k: pytest.fail('oversize must stop'))
    with pytest.raises(ValueError, match='exceeds'):
        cds.curl_download('https://obj/x', tmp_path / 'out', None,
                          strict_size=True, expected_bytes=4)
    assert part.read_bytes() == b'too long'


def test_changed_remote_size_does_not_append_another_object(tmp_path, monkeypatch):
    monkeypatch.setattr(cds.time, 'sleep', lambda _: None)
    calls = []

    def run(cmd, **kw):
        calls.append(True)
        Path(cmd[cmd.index('-o') + 1]).write_bytes(b'ab')
        return SimpleNamespace(returncode=18)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ValueError, match='size changed'):
        cds.curl_download('https://obj/x', tmp_path / 'out', None, attempts=2,
                          strict_size=True, expected_bytes=4,
                          refresh_results=lambda: SimpleNamespace(
                              asset={'href': 'https://obj/y'}, content_length=5))
    assert len(calls) == 1
    assert (tmp_path / 'out.part').read_bytes() == b'ab'


def test_shrinking_partial_stops_instead_of_publishing(tmp_path, monkeypatch):
    part = tmp_path / 'out.part'
    part.write_bytes(b'abcdef')

    def run(cmd, **kw):
        part.write_bytes(b'ab')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ValueError, match='shrank'):
        cds.curl_download('https://obj/x', tmp_path / 'out', None,
                          strict_size=True, expected_bytes=10)
    assert not (tmp_path / 'out').exists()


def test_size_metadata_falls_back_to_asset_without_head():
    assert cds.results_size(SimpleNamespace(asset={'file:size': '42'})) == 42
    with pytest.raises(ValueError, match='invalid CDS result size'):
        cds.results_size(SimpleNamespace(asset={'file:size': 'secret'}))
