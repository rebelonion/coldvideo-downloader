import asyncio
import base64
from contextlib import asynccontextmanager
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from coldvideo_downloader import capture as h

URL = 'https://example.test/test/track'


def box(kind, payload=b''):
    return (len(payload) + 8).to_bytes(4, 'big') + kind + payload


def fragment(seq, prefix=b''):
    return prefix + box(b'moof', box(b'mfhd', b'\0' * 4 + seq.to_bytes(4, 'big'))) + box(b'mdat', b'audio')


INIT = box(b'ftyp', b'M4A \0\0\0\0') + box(b'moov')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_prefixed_and_extended_boxes_preserve_sequences_after_resume(self):
        store = h.Store(self.root / 'parts', URL)
        first = fragment(7, box(b'styp', b'msdh\0\0\0\0'))
        store.add('init', INIT)
        store.add('frag', first)
        resumed = h.Store(store.dir, URL + '#t=0')
        self.assertFalse(resumed.add('frag', first))
        resumed.add('frag', fragment(8, box(b'emsg', b'event')))
        self.assertEqual(sorted(resumed.frags), [7, 8])
        extended = b'\0\0\0\1moof' + (32).to_bytes(8, 'big') + box(b'mfhd', b'\0' * 4 + (9).to_bytes(4, 'big'))
        self.assertEqual(h.mfhd_seq(extended), 9)

    def test_missing_sequence_is_rejected_instead_of_inventing_an_order(self):
        with self.assertRaises(h.CaptureError):
            h.mfhd_seq(box(b'styp'))
        with self.assertRaises(h.CaptureError):
            h.mfhd_seq(fragment(1)[:-1])

    def test_conflicting_track_init_or_fragment_is_rejected(self):
        store = h.Store(self.root / 'parts', URL)
        with self.assertRaises(h.CaptureError):
            h.Store(store.dir, URL + 'other')
        store.add('init', INIT)
        with self.assertRaises(h.CaptureError):
            store.add('init', INIT + box(b'free'))
        store.add('frag', fragment(1))
        with self.assertRaises(h.CaptureError):
            store.add('frag', fragment(1)[:-1] + b'!')

    def test_unbound_legacy_parts_are_not_adopted(self):
        parts = self.root / 'parts'
        parts.mkdir()
        (parts / 'init.bin').write_bytes(INIT)
        with self.assertRaises(h.CaptureError):
            h.Store(parts, URL)

    def test_atomic_write_failure_preserves_previous_data(self):
        path = self.root / 'status.json'
        path.write_bytes(b'previous')
        with patch.object(h.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                h.atomic_write(path, b'new')
        self.assertEqual(path.read_bytes(), b'previous')
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_only_one_capture_can_use_a_parts_directory(self):
        parts = self.root / 'parts'
        with h.parts_lock(parts):
            with self.assertRaises(h.CaptureError):
                with h.parts_lock(parts):
                    self.fail('second capture acquired the lock')
        with h.parts_lock(parts):
            pass


class ProbeTests(unittest.TestCase):
    def report(self, spans, frames='100'):
        return json.dumps({'streams': [{'nb_read_frames': frames}],
                           'packets': [{'pts_time': str(t), 'duration_time': str(d)} for t, d in spans]})

    def test_failed_or_missing_ffprobe_is_never_accepted(self):
        with patch.object(h.shutil, 'which', return_value=None):
            with self.assertRaises(h.CaptureError):
                h.probe_media(Path('audio'), 4)
        failed = SimpleNamespace(returncode=1, stdout='', stderr='Invalid data')
        with patch.object(h.shutil, 'which', return_value='/fake/tool'), patch.object(h.subprocess, 'run', return_value=failed):
            with self.assertRaises(h.CaptureError):
                h.probe_media(Path('audio'), 4)
        partial = SimpleNamespace(returncode=0, stdout=self.report([(0, 4)]), stderr='Error decoding frame')
        with patch.object(h.shutil, 'which', return_value='/fake/tool'), patch.object(h.subprocess, 'run', return_value=partial):
            with self.assertRaises(h.CaptureError):
                h.probe_media(Path('audio'), 4)

    def test_leading_trailing_and_internal_gaps_fail_even_without_decode(self):
        cases = [[(2, 2)], [(0, 1)], [(0, 1), (2, 2)], [(0, float('nan'))], [(0, 0)]]
        for spans in cases:
            with self.subTest(spans=spans), patch.object(h.shutil, 'which', return_value='/fake/tool'), patch.object(h, 'run_media', return_value=self.report(spans)):
                with self.assertRaises(h.CaptureError):
                    h.probe_media(Path('audio'), 4, decode=False)

    def test_partial_coverage_can_be_checked_without_claiming_completion(self):
        with patch.object(h.shutil, 'which', return_value='/fake/tool'), patch.object(h, 'run_media', return_value=self.report([(0, 1), (1, 1)])):
            self.assertEqual(h.probe_media(Path('audio'), decode=False)['end'], 2)
            with self.assertRaises(h.CaptureError):
                h.probe_media(Path('audio'), 4, decode=False)


class FakeCDP:
    def __init__(self):
        self.functions = {name: object() for name in h.SINKS}
        self.objects = {}
        self.breakpoints = {}
        self.next_id = 0
        self.failing_sink = None

    async def send(self, method, params):
        if method == 'Runtime.evaluate':
            name = next(name for name, expr in h.SINKS.items() if expr == params['expression'])
            value = self.functions[name]
            if value is None:
                return {'result': {'type': 'object', 'subtype': 'null'}}
            self.next_id += 1
            oid = f'object-{self.next_id}'
            self.objects[oid] = value
            return {'result': {'type': 'function', 'objectId': oid}}
        if method == 'Runtime.callFunctionOn':
            left = self.objects[params['objectId']]
            right = self.objects[params['arguments'][0]['objectId']]
            return {'result': {'value': left is right}}
        if method == 'Debugger.setBreakpointOnFunctionCall':
            obj = self.objects[params['objectId']]
            if obj is self.functions.get(self.failing_sink):
                raise RuntimeError('cannot arm')
            self.next_id += 1
            bid = f'breakpoint-{self.next_id}'
            self.breakpoints[bid] = obj
            return {'breakpointId': bid}
        if method == 'Debugger.removeBreakpoint':
            del self.breakpoints[params['breakpointId']]
            return {}
        if method == 'Runtime.releaseObject':
            del self.objects[params['objectId']]
            return {}
        raise AssertionError(method)


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cdp = FakeCDP()
        self.errors = Mock()
        self.tracker = h.SinkTracker(self.cdp, self.errors)
        self.cid = self.tracker.created({'context': {'id': 1, 'origin': 'https://example.test', 'auxData': {'isDefault': True}}})

    async def test_every_sink_is_tracked_and_identity_changes_are_rearmed(self):
        self.assertTrue(await self.tracker.arm(self.cid))
        original = self.cdp.functions['SourceBuffer']
        self.cdp.functions['SourceBuffer'] = object()  # Both can stringify as native code.
        await self.tracker.arm(self.cid)
        records = self.tracker.contexts[self.cid]
        self.assertEqual(set(records), set(h.SINKS))
        self.assertEqual(len(records['SourceBuffer']), 2)
        self.assertIn(original, self.cdp.breakpoints.values())
        count = len(self.cdp.breakpoints)
        await self.tracker.arm(self.cid)
        self.assertEqual(len(self.cdp.breakpoints), count)
        self.assertEqual(len(self.cdp.objects), count)

    async def test_removed_sink_is_not_reported_as_armed_and_can_return(self):
        await self.tracker.arm(self.cid)
        self.cdp.functions['SourceBuffer'] = None
        await self.tracker.arm(self.cid)
        self.assertNotIn('SourceBuffer', self.tracker.active[self.cid])
        self.cdp.functions['SourceBuffer'] = object()
        await self.tracker.arm(self.cid)
        self.assertIn('SourceBuffer', self.tracker.active[self.cid])
        self.tracker.cleared({})
        self.assertFalse(await self.tracker.arm_all())

    async def test_force_renewal_failure_can_be_retried(self):
        await self.tracker.arm(self.cid)
        self.cdp.failing_sink = 'SourceBuffer'
        await self.tracker.arm(self.cid, force=True)
        self.assertNotIn('SourceBuffer', self.tracker.active[self.cid])
        self.cdp.failing_sink = None
        await self.tracker.arm(self.cid)
        self.assertIn('SourceBuffer', self.tracker.active[self.cid])

    async def test_play_alone_does_not_count_as_a_media_sink(self):
        for name in h.MEDIA_SINKS:
            self.cdp.functions[name] = None
        self.assertFalse(await self.tracker.arm(self.cid))


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_failure_closes_browser_and_saves_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.m4a'
            args = SimpleNamespace(url=URL, out=str(out), resume=False, start=0, no_verify=False, no_fix=False, max_seconds=None, grace_seconds=60, no_reconnect=False)
            page = Mock()
            page.goto = AsyncMock(side_effect=RuntimeError('navigation failed'))
            context = Mock()
            context.new_page = AsyncMock(return_value=page)
            cdp = Mock()
            cdp.send = AsyncMock(return_value={})
            context.new_cdp_session = AsyncMock(return_value=cdp)
            browser = Mock()
            browser.new_context = AsyncMock(return_value=context)
            browser.close = AsyncMock()
            playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
            @asynccontextmanager
            async def fake_playwright():
                yield playwright
            with patch.object(h, 'async_playwright', fake_playwright):
                self.assertEqual(await h.capture(args), 2)
            browser.close.assert_awaited_once()
            status = json.loads(out.with_suffix('.m4a.parts').joinpath('status.json').read_text())
            self.assertEqual(status['reason'], 'capture_error')
            self.assertFalse(status['complete'])
            self.assertTrue((Path(tmp) / 'out.raw.m4a').exists())

    async def test_pause_always_resumes_after_scope_failure(self):
        session = h.Session(SimpleNamespace(), SimpleNamespace())
        cdp = Mock()
        async def send(method, params):
            if method == 'Runtime.getProperties':
                raise RuntimeError('realm destroyed')
            return {}
        cdp.send = AsyncMock(side_effect=send)
        await session.paused(cdp, {'callFrames': [{'scopeChain': [{'type': 'local', 'object': {'objectId': 'scope'}}]}]})
        self.assertEqual(cdp.send.await_args_list[-1].args[0], 'Debugger.resume')
        self.assertEqual(session.errors['scope read'], 1)

    async def test_cancelled_startup_still_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.m4a'
            args = SimpleNamespace(url=URL, out=str(out), resume=False, start=0, no_verify=False, no_fix=False, max_seconds=None, grace_seconds=60, no_reconnect=False)
            playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=asyncio.CancelledError)))
            @asynccontextmanager
            async def fake_playwright():
                yield playwright
            with patch.object(h, 'async_playwright', fake_playwright):
                self.assertEqual(await h.capture(args), 2)
            status = json.loads(out.with_suffix('.m4a.parts').joinpath('status.json').read_text())
            self.assertEqual(status['reason'], 'interrupted')

    async def test_target_closed_during_navigation_reconnects_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.m4a'
            args = SimpleNamespace(url=URL, out=str(out), resume=False, start=120,
                                   no_verify=False, no_fix=False, max_seconds=None,
                                   grace_seconds=60, no_reconnect=False)
            page = Mock()
            page.goto = AsyncMock(side_effect=h.BrowserError('Target page, context or browser has been closed'))
            context = Mock()
            context.new_page = AsyncMock(return_value=page)
            cdp = Mock()
            cdp.send = AsyncMock(return_value={})
            context.new_cdp_session = AsyncMock(return_value=cdp)
            browsers = [Mock(), Mock()]
            for browser in browsers:
                browser.new_context = AsyncMock(return_value=context)
                browser.close = AsyncMock()
            playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=browsers)))
            @asynccontextmanager
            async def fake_playwright():
                yield playwright
            with patch.object(h, 'async_playwright', fake_playwright):
                self.assertEqual(await h.capture(args), 2)
            for browser in browsers:
                browser.close.assert_awaited_once()
            self.assertEqual([call.args[0] for call in page.goto.await_args_list], [URL + '#t=120', URL])
            status = json.loads(out.with_suffix('.m4a.parts').joinpath('status.json').read_text())
            self.assertEqual(status['reason'], 'connection_lost')
            self.assertEqual(status['reconnects'], 1)

    async def test_launch_failure_does_not_retry_before_connection_is_established(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.m4a'
            args = SimpleNamespace(url=URL, out=str(out), resume=False, start=0,
                                   no_verify=False, no_fix=False, max_seconds=None,
                                   grace_seconds=60, no_reconnect=False)
            launch = AsyncMock(side_effect=h.BrowserError('Target page, context or browser has been closed'))
            @asynccontextmanager
            async def fake_playwright():
                yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
            with patch.object(h, 'async_playwright', fake_playwright):
                self.assertEqual(await h.capture(args), 2)
            launch.assert_awaited_once()
            status = json.loads(out.with_suffix('.m4a.parts').joinpath('status.json').read_text())
            self.assertEqual(status['reason'], 'capture_error')


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = h.Store(Path(self.tmp.name) / 'parts', URL)
        self.args = SimpleNamespace(max_seconds=60, broken_seconds=5, stall_seconds=8, grace_seconds=60)
        self.session = h.Session(self.args, self.store)
        self.session.tracker = SimpleNamespace(arm_all=AsyncMock(return_value=True))

    async def monitor(self, advancing=True, healthy=False, coverage=None, total=1000, offset=0):
        clock = SimpleNamespace(now=0)
        clock.time = lambda: clock.now
        async def tick(seconds):
            clock.now += seconds
            if healthy:
                self.session.media_events += 1
                self.session.last_media = clock.now
        async def progress(*_args):
            current = int(clock.now) + offset if advancing else offset
            return {'result': {'value': f'{current // 60}:{current % 60:02d} / {total // 60}:{total % 60:02d}'}}
        with patch.object(h.asyncio, 'get_running_loop', return_value=clock), patch.object(h.asyncio, 'sleep', tick), patch.object(h, 'send', progress), patch.object(h.asyncio, 'to_thread', AsyncMock(return_value={'end': coverage})):
            with redirect_stdout(io.StringIO()):
                return await self.session.monitor(Mock())

    async def test_recovery_is_bounded_when_capture_never_returns(self):
        self.assertEqual(await self.monitor(), 'capture_broken')
        self.assertEqual(self.session.tracker.arm_all.await_count, 3)

    async def test_stalled_player_is_reported_without_claiming_server_throttling(self):
        self.assertEqual(await self.monitor(advancing=False), 'stalled')
        self.session.tracker.arm_all.assert_not_awaited()

    async def test_prebuffered_audio_does_not_trigger_recovery(self):
        self.store.add('init', INIT)
        self.store.add('frag', fragment(1))
        self.assertEqual(await self.monitor(coverage=1000), 'timeout')
        self.session.tracker.arm_all.assert_not_awaited()
        self.assertFalse((self.store.dir / 'coverage.m4a').exists())

    async def test_healthy_capture_does_not_trigger_recovery(self):
        self.assertEqual(await self.monitor(healthy=True), 'timeout')
        self.session.tracker.arm_all.assert_not_awaited()

    async def test_duplicate_fragment_during_replay_counts_as_capture_activity(self):
        data = fragment(1)
        self.store.add('frag', data)
        head = {'len': len(data), 'head': base64.b64encode(data[:32]).decode()}
        results = [{'result': {'value': head}}, {'result': {'value': base64.b64encode(data).decode()}}]
        with patch.object(h, 'send', AsyncMock(side_effect=results)):
            await self.session.consider(Mock(), 'object', 'Uint8Array(37)')
        self.assertEqual(len(self.store.frags), 1)
        self.assertEqual(self.session.media_events, 1)
        self.assertGreater(self.session.last_media, 0)

    async def test_automatic_allowance_finishes_a_track_longer_than_old_default(self):
        self.args.max_seconds = None
        self.assertEqual(await self.monitor(healthy=True, total=1800), 'end')
        self.assertEqual(self.session.playback_seconds, 1800)
        self.assertEqual(self.session.runtime['allowance_seconds'], 1860)

    async def test_automatic_allowance_uses_remaining_time_at_an_offset(self):
        self.args.max_seconds = None
        self.assertEqual(await self.monitor(healthy=True, total=1800, offset=600), 'end')
        self.assertEqual(self.session.playback_seconds, 1200)
        self.assertEqual(self.session.runtime['allowance_seconds'], 1260)

    async def test_grace_does_not_slide_forward_on_every_poll(self):
        self.args.max_seconds = None
        self.args.grace_seconds = 6
        self.assertEqual(await self.monitor(advancing=False, healthy=True, total=6), 'timeout')
        self.assertEqual(self.session.playback_seconds, 15)

    async def test_unknown_duration_still_has_a_finite_allowance(self):
        self.args.max_seconds = None
        self.assertEqual(await self.monitor(healthy=True, total=0), 'timeout')
        self.assertEqual(self.session.playback_seconds, 1200)

    async def test_explicit_limit_is_not_extended_by_track_duration(self):
        self.args.max_seconds = 62
        self.assertEqual(await self.monitor(healthy=True, total=1800), 'timeout')
        self.assertEqual(self.session.playback_seconds, 62)
        self.assertEqual(self.session.runtime['mode'], 'explicit')

    async def test_explicit_limit_consumed_by_first_attempt_is_not_reset(self):
        self.session.elapsed_playback = 10
        self.assertEqual(await self.monitor(healthy=True), 'timeout')
        self.assertEqual(self.session.playback_seconds, 50)

    async def test_disconnect_event_stops_monitor_even_when_progress_would_continue(self):
        self.session.mark_disconnected('CDP closed')
        self.assertEqual(await self.monitor(healthy=True), 'connection_lost')

    async def test_rate_control_failure_before_acceleration_keeps_passive_capture(self):
        self.session.rate = h.PlaybackRate(2)
        self.session.tracker.contexts = {}
        self.session.rate.poll = AsyncMock(side_effect=RuntimeError('rate unavailable'))
        self.assertEqual(await self.monitor(healthy=True), 'timeout')
        self.session.rate.poll.assert_awaited_once()
        self.assertEqual(self.session.rate.status['fallback_reason'], 'rate control unavailable')

    async def test_rate_control_failure_after_acceleration_stops_capture(self):
        self.session.rate = h.PlaybackRate(2)
        self.session.rate.status.update(actual=2, accelerated=True)
        self.session.tracker.contexts = {}
        self.session.rate.poll = AsyncMock(side_effect=RuntimeError('cannot control active rate'))
        self.assertEqual(await self.monitor(healthy=True), 'capture_error')


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = SimpleNamespace(url=URL, out=str(self.root / 'out.m4a'), resume=False,
                                    start=0, no_verify=False, no_fix=True,
                                    max_seconds=None, grace_seconds=60, no_reconnect=False)

    def status(self):
        return json.loads((self.root / 'out.m4a.parts' / 'status.json').read_text())

    async def test_single_reconnect_reuses_and_refills_saved_parts(self):
        self.args.rate = 2
        sessions = []
        async def run(session):
            sessions.append(session)
            session.duration = 4
            session.store.add('init', INIT)
            if len(sessions) == 1:
                session.store.add('frag', fragment(2))
                session.playback_seconds = 10
                session.rate.status['fallback_reason'] = 'player changed playback rate'
                session.report_error('scope read', h.BrowserError('Session closed'))
                session.disconnected = 'CDP closed'
                return 'connection_lost'
            self.assertTrue(session.reconnect)
            self.assertEqual(session.rate.status['fallback_reason'], 'player changed playback rate')
            cdp = Mock()
            await session.rate.poll(cdp, {}, 4)
            cdp.send.assert_not_called()
            self.assertEqual(session.elapsed_playback, 10)
            self.assertFalse(session.store.add('frag', fragment(2)))
            session.store.add('frag', fragment(1))
            session.playback_seconds = 4
            session.last_seconds = 4
            return 'end'
        with patch.object(h.Session, 'run', run), patch.object(h, 'probe_media', return_value={'decoded': True}):
            self.assertEqual(await h.capture(self.args), 0)
        self.assertEqual(len(sessions), 2)
        self.assertIs(sessions[0].store, sessions[1].store)
        self.assertEqual(Path(self.args.out).read_bytes(), INIT + fragment(1) + fragment(2))
        status = self.status()
        self.assertEqual(status['reconnects'], 1)
        self.assertEqual([a['reason'] for a in status['attempts']], ['connection_lost', 'end'])
        self.assertEqual(status['playback_seconds'], 14)
        self.assertEqual(status['errors']['scope read'], 1)
        self.assertEqual(status['attempts'][1]['rate']['requested'], 2)
        self.assertEqual(status['attempts'][1]['rate']['actual'], 1)

    async def test_buffer_slowdown_does_not_disable_acceleration_after_reconnect(self):
        self.args.rate = 4
        sessions = []
        async def run(session):
            sessions.append(session)
            if not session.reconnect:
                session.rate.status.update(accelerated=True, slowdown_reason='low playback buffer')
                session.mark_disconnected('CDP closed')
                return 'connection_lost'
            self.assertIsNone(session.rate.status['fallback_reason'])
            self.assertEqual(session.rate.status['requested'], 4)
            return 'timeout'
        with patch.object(h.Session, 'run', run):
            self.assertEqual(await h.capture(self.args), 2)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(self.status()['attempts'][0]['rate']['slowdown_reason'], 'low playback buffer')

    async def test_second_disconnect_exhausts_retry_and_keeps_parts(self):
        calls = []
        async def run(session):
            calls.append(session)
            session.store.add('init', INIT)
            session.store.add('frag', fragment(1))
            return 'connection_lost'
        with patch.object(h.Session, 'run', run):
            self.assertEqual(await h.capture(self.args), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.status()['reason'], 'connection_lost')
        self.assertEqual(self.status()['reconnects'], 1)
        self.assertEqual((self.root / 'out.raw.m4a').read_bytes(), INIT + fragment(1))

    async def test_stalls_errors_interruptions_and_timeouts_do_not_retry(self):
        for reason in ['stalled', 'capture_broken', 'capture_error', 'player_unavailable', 'interrupted', 'timeout']:
            with self.subTest(reason=reason), patch.object(h.Session, 'run', AsyncMock(return_value=reason)) as run:
                self.assertEqual(await h.capture(self.args), 2)
                run.assert_awaited_once()
                self.assertEqual(self.status()['reason'], reason)

    async def test_reconnect_can_be_disabled(self):
        self.args.no_reconnect = True
        with patch.object(h.Session, 'run', AsyncMock(return_value='connection_lost')) as run:
            self.assertEqual(await h.capture(self.args), 2)
        run.assert_awaited_once()
        self.assertEqual(self.status()['reconnects'], 0)

    async def test_exhausted_explicit_limit_prevents_reconnect(self):
        self.args.max_seconds = 10
        calls = []
        async def run(session):
            calls.append(session)
            session.playback_seconds = 10
            return 'connection_lost'
        with patch.object(h.Session, 'run', run):
            self.assertEqual(await h.capture(self.args), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.status()['reason'], 'timeout')

    async def test_disk_failure_prevents_reconnect(self):
        calls = []
        async def run(session):
            calls.append(session)
            session.report_error('capture buffer', OSError('disk full'))
            return 'connection_lost'
        with patch.object(h.Session, 'run', run):
            self.assertEqual(await h.capture(self.args), 2)
        self.assertEqual(len(calls), 1)

    async def test_failure_saving_retry_state_still_finalizes_parts(self):
        calls = []
        async def run(session):
            calls.append(session)
            session.store.add('init', INIT)
            session.store.add('frag', fragment(1))
            return 'connection_lost'
        original_write_json = h.write_json
        def fail_retry_state(path, value):
            if value.get('reason') == 'reconnecting':
                raise OSError('disk failure')
            original_write_json(path, value)
        with patch.object(h.Session, 'run', run), patch.object(h, 'write_json', fail_retry_state):
            self.assertEqual(await h.capture(self.args), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.status()['reason'], 'capture_error')
        self.assertEqual(self.status()['errors']['reconnect state'], 1)
        self.assertEqual((self.root / 'out.raw.m4a').read_bytes(), INIT + fragment(1))

    def test_only_transport_errors_qualify_as_disconnects(self):
        self.assertTrue(h.is_connection_error(h.BrowserError('Target page, context or browser has been closed')))
        self.assertTrue(h.is_connection_error(h.BrowserError('CDPSession.send: Session closed.')))
        for exc in [h.BrowserError('HTTP 429'), h.BrowserError('Navigation timeout'),
                    h.BrowserError('Execution context was destroyed'), TimeoutError(),
                    OSError('disk full'), h.CaptureError('conflicting data')]:
            with self.subTest(exc=exc):
                self.assertFalse(h.is_connection_error(exc))

    def test_cleanup_does_not_count_as_disconnect(self):
        session = h.Session(self.args, SimpleNamespace())
        session.closing = True
        session.mark_disconnected('intentional close')
        self.assertIsNone(session.disconnected)

    def test_timeout_after_confirmed_disconnect_is_not_a_fatal_storage_error(self):
        session = h.Session(self.args, SimpleNamespace())
        session.established = True
        session.mark_disconnected('CDP closed')
        session.report_error('session', TimeoutError())
        self.assertIsNone(session.fatal)
        self.assertEqual(session.disconnected, 'CDP closed')


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'out.m4a'
        self.out.write_bytes(b'previous successful download')
        self.store = h.Store(self.root / 'out.m4a.parts', URL)
        self.args = SimpleNamespace(out=str(self.out), no_verify=False, no_fix=True)
        self.session = h.Session(self.args, self.store)
        self.session.duration = 4

    def test_init_only_cannot_be_complete(self):
        self.store.add('init', INIT)
        self.assertEqual(h.finalize(self.args, self.store, self.session, 'end'), 2)
        self.assertEqual(json.loads((self.store.dir / 'status.json').read_text())['reason'], 'missing_media')
        self.assertEqual(self.out.read_bytes(), b'previous successful download')

    def test_final_candidate_failure_preserves_previous_output(self):
        self.store.add('init', INIT)
        self.store.add('frag', fragment(1))
        with patch.object(h, 'probe_media', side_effect=[{'decoded': True}, h.CaptureError('bad remux')]):
            self.assertEqual(h.finalize(self.args, self.store, self.session, 'end'), 2)
        self.assertEqual(self.out.read_bytes(), b'previous successful download')
        self.assertTrue((self.root / 'out.raw.m4a').exists())

    def test_missing_sequence_cannot_be_complete(self):
        self.store.add('init', INIT)
        self.store.add('frag', fragment(1))
        self.store.add('frag', fragment(3))
        self.assertEqual(h.finalize(self.args, self.store, self.session, 'end'), 2)
        self.assertEqual(json.loads((self.store.dir / 'status.json').read_text())['gaps'], [[2, 2]])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'media tools unavailable')
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.source = cls.root / 'source.m4a'
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                        '-t', '4', '-c:a', 'aac', '-movflags', '+empty_moov+default_base_moof', '-frag_duration', '500000',
                        str(cls.source)], check=True, capture_output=True)
        data = cls.source.read_bytes()
        offset = 0
        cls.init = b''
        cls.fragments = []
        pending = b''
        for kind, payload in h.boxes(data):
            size = len(payload) + 8
            raw = data[offset:offset + size]
            offset += size
            if kind in (b'ftyp', b'moov'):
                cls.init += raw
            elif kind == b'moof':
                pending = raw
            elif kind == b'mdat':
                cls.fragments.append(pending + raw)
        cls.addClassCleanup(cls.tmp.cleanup)

    def test_real_aac_is_assembled_remuxed_and_decoded(self):
        store = h.Store(self.root / 'complete.parts', URL)
        store.add('init', self.init)
        for data in reversed(self.fragments):
            store.add('frag', data)
        out = self.root / 'complete.m4a'
        args = SimpleNamespace(out=str(out), no_verify=False, no_fix=False)
        session = h.Session(args, store)
        session.duration = 4
        self.assertEqual(h.finalize(args, store, session, 'end'), 0)
        status = json.loads((store.dir / 'status.json').read_text())
        self.assertTrue(status['verified'])
        self.assertGreater(status['media']['frames'], 0)
        self.assertAlmostEqual(status['media']['end'], 4, delta=0.1)

    def test_real_truncated_and_gapped_audio_is_rejected(self):
        cases = {'leading': self.fragments[2:], 'trailing': self.fragments[:3],
                 'middle': self.fragments[:2] + self.fragments[3:]}
        for name, fragments in cases.items():
            with self.subTest(name=name):
                path = self.root / f'{name}.m4a'
                path.write_bytes(self.init + b''.join(fragments))
                with self.assertRaises(h.CaptureError):
                    h.probe_media(path, 4)

    def test_decode_opt_out_still_checks_timeline_and_reports_unverified(self):
        store = h.Store(self.root / 'unverified.parts', URL)
        store.add('init', self.init)
        for data in self.fragments:
            store.add('frag', data)
        args = SimpleNamespace(out=str(self.root / 'unverified.m4a'), no_verify=True, no_fix=True)
        session = h.Session(args, store)
        session.duration = 4
        self.assertEqual(h.finalize(args, store, session, 'end'), 0)
        status = json.loads((store.dir / 'status.json').read_text())
        self.assertFalse(status['verified'])
        self.assertFalse(status['media']['decoded'])


@unittest.skipUnless(shutil.which('google-chrome'), 'Google Chrome unavailable')
class ChromiumIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_cdp_detach_is_recognized_as_connection_loss(self):
        async with h.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                session = h.Session(SimpleNamespace(), SimpleNamespace())
                session.established = True
                closed = asyncio.Event()
                def detached(*_args):
                    session.mark_disconnected('CDP session closed')
                    closed.set()
                cdp.on('close', detached)
                await cdp.detach()
                await asyncio.wait_for(closed.wait(), 2)
                self.assertEqual(session.disconnected, 'CDP session closed')
                with self.assertRaises(h.BrowserError) as error:
                    await h.send(cdp, 'Runtime.evaluate', {'expression': '1'})
                self.assertTrue(h.is_connection_error(error.exception))
            finally:
                await browser.close()

    async def test_real_cdp_rearms_identical_source_replacements(self):
        # This fixture is about:blank; it makes no requests to the live site.
        async with h.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                contexts = []
                cdp.on('Runtime.executionContextCreated', lambda event: contexts.append(event['context']))
                await h.send(cdp, 'Runtime.enable')
                await h.send(cdp, 'Debugger.enable')
                cid = next(context['id'] for context in contexts if context.get('auxData', {}).get('isDefault'))
                errors = Mock()
                tracker = h.SinkTracker(cdp, errors)
                tracker.created({'context': {'id': cid, 'origin': 'https://example.test', 'auxData': {'isDefault': True}}})
                self.assertTrue(await tracker.arm(cid))
                self.assertTrue(await tracker.arm(cid))
                self.assertEqual(len(tracker.contexts[cid]['SourceBuffer']), 1)
                source = await h.send(cdp, 'Runtime.evaluate', {
                    'expression': 'SourceBuffer.prototype.appendBuffer.toString()', 'returnByValue': True})
                self.assertIn('[native code]', source['result']['value'])
                for _ in range(2):
                    await h.send(cdp, 'Runtime.evaluate', {
                        'expression': 'SourceBuffer.prototype.appendBuffer = function appendBuffer() {}'})
                    self.assertTrue(await tracker.arm(cid))
                self.assertEqual(len(tracker.contexts[cid]['SourceBuffer']), 3)
                self.assertTrue(await tracker.arm(cid, force=True))
                errors.assert_not_called()
            finally:
                await browser.close()


@unittest.skipUnless(shutil.which('google-chrome') and shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'Chrome and media tools unavailable')
class PlaybackRateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        source = cls.root / 'source.m4a'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=sample_rate=48000',
                        '-t', '30', '-c:a', 'aac', '-movflags', '+empty_moov+default_base_moof',
                        '-frag_duration', '500000', str(source)], check=True, capture_output=True)
        data = source.read_bytes()
        cls.source = base64.b64encode(data).decode()
        chunks = [b'']
        for kind, payload in h.boxes(data):
            raw = box(kind, payload)
            if kind == b'moof':
                chunks.append(raw)
            elif kind in (b'ftyp', b'moov', b'mdat'):
                chunks[-1] += raw
        cls.chunks = [base64.b64encode(chunk).decode() for chunk in chunks]

    async def asyncSetUp(self):
        playwright = await h.async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        self.browser = await playwright.chromium.launch(channel='chrome', headless=True)
        self.addAsyncCleanup(self.browser.close)
        page = await self.browser.new_page()
        self.cdp = await page.context.new_cdp_session(page)
        self.contexts = {}
        self.cdp.on('Runtime.executionContextCreated', lambda event: self.contexts.update(
            {event['context']['id']: {}}) if event['context'].get('auxData', {}).get('isDefault') else None)
        await h.send(self.cdp, 'Runtime.enable')

    async def evaluate(self, expression):
        result = await h.send(self.cdp, 'Runtime.evaluate', {
            'expression': expression, 'returnByValue': True, 'awaitPromise': True, 'userGesture': True})
        self.assertNotIn('exceptionDetails', result)
        return result['result'].get('value')

    async def play(self):
        await self.evaluate(f'''globalThis.audio = new Audio('data:audio/mp4;base64,{self.source}');
            audio.muted = true; audio.play();''')
        self.assertFalse(await self.evaluate('audio.isConnected'))

    async def seek(self, seconds):
        await self.evaluate(f'''new Promise(resolve=>{{
            audio.addEventListener('seeked',resolve,{{once:true}});audio.currentTime={seconds};
        }})''')

    async def test_detached_audio_slows_down_and_accelerates_after_refilling(self):
        await self.play()
        native = await self.evaluate('HTMLMediaElement.prototype.play.toString()')
        rate = h.PlaybackRate(2)
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(await self.evaluate('audio.playbackRate'), 2)
        await self.seek(22)
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(rate.status['slowdown_reason'], 'low playback buffer')
        self.assertIsNone(rate.status['fallback_reason'])
        self.assertEqual(await self.evaluate('audio.playbackRate'), 1)
        await self.seek(12)
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(await self.evaluate('audio.playbackRate'), 1)
        await self.seek(0)
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(await self.evaluate('audio.playbackRate'), 2)
        self.assertEqual(rate.status['speedups'], 2)
        self.assertEqual(rate.status['slowdowns'], 1)
        self.assertEqual(await self.evaluate('HTMLMediaElement.prototype.play.toString()'), native)

    async def test_all_requested_rates_can_finish_a_fully_buffered_tail(self):
        await self.play()
        for requested in (1.25, 1.5, 2, 2.75, 4, 8, 16):
            with self.subTest(rate=requested):
                await self.evaluate('audio.playbackRate=1;audio.play()')
                await self.seek(27)
                rate = h.PlaybackRate(requested)
                await rate.poll(self.cdp, self.contexts, 30)
                await rate.poll(self.cdp, self.contexts, 30)
                self.assertEqual(await self.evaluate('audio.playbackRate'), requested)
                self.assertIsNone(rate.status['fallback_reason'])

    async def test_player_rate_changes_are_not_repeatedly_overridden(self):
        await self.play()
        rate = h.PlaybackRate(1.5)
        await rate.poll(self.cdp, self.contexts, 100)
        await self.evaluate('audio.playbackRate=1')
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(rate.status['fallback_reason'], 'player changed playback rate')
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(await self.evaluate('audio.playbackRate'), 1)

    async def test_repeated_high_rate_cycles_wait_for_the_upper_buffer_threshold(self):
        fixture = await h.send(self.cdp, 'Runtime.evaluate', {'expression': '''(
            globalThis.bufferEnd=100,
            {currentTime:0, playbackRate:1, paused:false, ended:false, readyState:4,
             buffered:{length:1,start:()=>0,end:()=>bufferEnd}}
        )'''})
        rate = h.PlaybackRate(8)
        rate.media = fixture['result']['objectId']
        await rate.poll(self.cdp, self.contexts, 1000)
        self.assertEqual(rate.status['actual'], 8)
        for _ in range(3):
            await self.evaluate('bufferEnd=40')
            await rate.poll(self.cdp, self.contexts, 1000)
            self.assertEqual(rate.status['actual'], 1)
            self.assertIsNone(rate.status['fallback_reason'])
            await self.evaluate('bufferEnd=60')
            await rate.poll(self.cdp, self.contexts, 1000)
            self.assertEqual(rate.status['actual'], 1)
            await self.evaluate('bufferEnd=100')
            await rate.poll(self.cdp, self.contexts, 1000)
            self.assertEqual(rate.status['actual'], 8)
        self.assertEqual(rate.status['speedups'], 4)
        self.assertEqual(rate.status['slowdowns'], 3)

    async def test_high_rate_waits_for_a_larger_real_playback_buffer(self):
        await self.play()
        rate = h.PlaybackRate(4)
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(await self.evaluate('audio.playbackRate'), 1)
        self.assertEqual(rate.status['slowdown_reason'], 'buffer refilling')

    async def test_future_buffer_range_does_not_hide_a_gap(self):
        fixture = await h.send(self.cdp, 'Runtime.evaluate', {'expression': '''({
            currentTime:5, playbackRate:1, paused:false, ended:false, readyState:4,
            buffered:{length:2,start:i=>[0,50][i],end:i=>[8,100][i]}
        })'''})
        rate = h.PlaybackRate(2)
        rate.media = fixture['result']['objectId']
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(rate.status['actual'], 1)
        self.assertEqual(rate.status['buffer_seconds'], 3)
        self.assertFalse(rate.status['accelerated'])

    async def test_rejected_acceleration_is_disabled_without_repeated_attempts(self):
        fixture = await h.send(self.cdp, 'Runtime.evaluate', {'expression': '''({
            currentTime:0, paused:false, ended:false, readyState:4,
            buffered:{length:1,start:()=>0,end:()=>40},
            get playbackRate(){return 1;},set playbackRate(value){}
        })'''})
        rate = h.PlaybackRate(2)
        rate.media = fixture['result']['objectId']
        await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(rate.status['fallback_reason'], 'player rejected playback rate')
        with patch.object(h, 'send', AsyncMock()) as send:
            await rate.poll(self.cdp, self.contexts, 100)
            send.assert_not_awaited()

    async def test_failed_restore_does_not_claim_one_x_playback(self):
        fixture = await h.send(self.cdp, 'Runtime.evaluate', {'expression': '''({
            currentTime:5, paused:false, ended:false, readyState:4,
            buffered:{length:1,start:()=>0,end:()=>8},
            get playbackRate(){return 2;},set playbackRate(value){}
        })'''})
        rate = h.PlaybackRate(2)
        rate.media = fixture['result']['objectId']
        rate.status.update(actual=2, accelerated=True)
        with self.assertRaisesRegex(h.CaptureError, 'restore 1x'):
            await rate.poll(self.cdp, self.contexts, 100)
        self.assertEqual(rate.status['actual'], 2)

    async def test_ambiguous_playing_media_is_not_accelerated(self):
        await self.play()
        await self.evaluate(f'''globalThis.other = new Audio('data:audio/mp4;base64,{self.source}');
            other.muted=true; other.play();''')
        rate = h.PlaybackRate(2)
        await rate.poll(self.cdp, self.contexts, 30)
        self.assertIsNone(rate.media)
        self.assertEqual(await self.evaluate('[audio.playbackRate,other.playbackRate]'), [1, 1])

    async def test_accelerated_capture_preserves_original_timeline(self):
        page = self.root / 'player.html'
        page.write_text('''<button id="player-playpause" class="paused">Play</button>
            <span id="player-progress-text">0:00 / 0:30</span><script>
            const audio=new Audio(), media=new MediaSource();audio.muted=true;
            audio.src=URL.createObjectURL(media);
            const button=document.getElementById('player-playpause');
            let append;
            button.onclick=()=>{audio.play();button.className='';if(append)append();};
            setInterval(()=>{const t=Math.floor(audio.currentTime);
                document.getElementById('player-progress-text').innerText=
                    '0:'+String(t).padStart(2,'0')+' / 0:30';},100);
            media.addEventListener('sourceopen',()=>{
                const buffer=media.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
                const chunks=CHUNKS;let index=0;
                append=()=>{if(index===chunks.length){media.endOfStream();return;}
                    const bytes=Uint8Array.from(atob(chunks[index++]),c=>c.charCodeAt(0));
                    buffer.appendBuffer(bytes);};
                buffer.addEventListener('updateend',append);if(!audio.paused)append();
            });</script>'''.replace('CHUNKS', json.dumps(self.chunks)))
        out = self.root / 'captured.m4a'
        args = SimpleNamespace(url=page.as_uri(), out=str(out), rate=8, resume=False, start=0,
                               max_seconds=45, grace_seconds=60, broken_seconds=20,
                               stall_seconds=45, no_verify=False, no_fix=False, no_reconnect=False)
        log = io.StringIO()
        with redirect_stdout(log):
            result = await h.capture(args)
        self.assertEqual(result, 0, log.getvalue())
        status = json.loads(out.with_suffix('.m4a.parts').joinpath('status.json').read_text())
        self.assertTrue(status['verified'])
        self.assertAlmostEqual(status['media']['end'], 30, delta=0.1)
        self.assertTrue(status['attempts'][0]['rate']['accelerated'])
        self.assertEqual(status['attempts'][0]['rate']['actual'], 8)


class RateCliTests(unittest.TestCase):
    def test_unsupported_rates_are_rejected_before_browser_setup(self):
        for rate in ('0.5', '16.1', '100', 'nan', 'inf'):
            with self.subTest(rate=rate):
                result = subprocess.run([sys.executable, '-m', 'coldvideo_downloader', URL,
                                         '--rate', rate], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('rate must be a finite number from 1 to 16', result.stderr)


if __name__ == '__main__':
    unittest.main()
