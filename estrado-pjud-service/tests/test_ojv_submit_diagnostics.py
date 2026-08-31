"""Synthetic count-only telemetry; not a model of the provider's protocol."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.ojv import browser_login as login
from app.ojv.errors import OjvTimeoutError
from tests.test_ojv_browser_login import _Page, _Counted, _install_fake_browser

SENTINEL = 'SECRET-url-body-header-cookie-title-console-error'


class Request:
    url = post_data_buffer = headers = SENTINEL
    failure = 'net::ERR_CONNECTION_RESET ' + SENTINEL
    resource_type = 'fetch'

    def is_navigation_request(self):
        return False


class DiagnosticPage(_Page):
    def __init__(self, *, broken=False, on_submit=None):
        super().__init__()
        self.listeners = {}
        self.broken = broken
        self.on_submit = on_submit
        submit = self.form.get_by_role('button', name='Ingresar', exact=True)

        async def click(**kwargs):
            self.actions.append('submit')
            if self.on_submit:
                await self.on_submit(self)

        submit.click = click
        self.form.get_by_role = lambda *args, **kwargs: submit

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value):
        for callback in tuple(self.listeners.get(event, [])):
            callback(value)

    async def goto(self, url, **kwargs):
        self.url = url + '?private=' + SENTINEL
        self.emit('requestfailed', Request())
        self.emit('pageerror', RuntimeError(SENTINEL))

    def locator(self, selector):
        if ':visible' in selector:
            if self.broken:
                raise RuntimeError(SENTINEL)
            return Alerts()
        return super().locator(selector)


class Alerts(_Counted):
    async def all_inner_texts(self):
        return []

    async def count(self):
        return 0


def diagnostic_record(caplog):
    record = next(r for r in caplog.records if 'pjud_private_login_failed' in r.msg)
    assert SENTINEL not in repr(record.__dict__)
    return record.getMessage()


async def run_timeout(monkeypatch, page):
    monkeypatch.setattr(login, '_LOGIN_TIMEOUT_S', .04)
    context, browser = _install_fake_browser(monkeypatch, page)
    start = time.monotonic()
    with pytest.raises(OjvTimeoutError):
        await login.login_official_ojv(SecretStr('11.111.111-1'), SecretStr(SENTINEL),
                                     proxy_url=None, user_agent='official-test-agent')
    assert time.monotonic() - start < .3
    assert context.closed and browser.closed


async def test_failure_record_counts_only_submit_window_and_never_sensitive_objects(monkeypatch, caplog):
    async def submit(page):
        request = Request()
        page.emit('request', request)
        page.emit('response', SimpleNamespace(status=503, request=request, url=SENTINEL))
        page.emit('requestfinished', request)
        page.emit('requestfailed', request)
        page.emit('pageerror', RuntimeError(SENTINEL))

    page = DiagnosticPage(on_submit=submit)
    await run_timeout(monkeypatch, page)
    message = diagnostic_record(caplog)
    assert 'submit_started_fetch=1' in message
    assert 'submit_finished_fetch=1' in message
    assert 'submit_failed_fetch=1' in message
    assert 'submit_http_5xx=1' in message
    assert 'submit_transport_reset=1' in message
    assert 'submit_js_errors=1' in message
    assert 'submit_inspection=checked_none' in message
    assert 'submit_location=official_entry' in message
    assert page.listeners['requestfailed'] == []
    assert page.listeners['pageerror'] == []


@pytest.mark.parametrize('broken,expected', [(False, 'checked_none'), (True, 'inspection_unavailable')])
async def test_failed_and_empty_inspection_keep_same_timeout_but_distinct_diagnostics(monkeypatch, caplog, broken, expected):
    await run_timeout(monkeypatch, DiagnosticPage(broken=broken))
    assert f'submit_inspection={expected}' in diagnostic_record(caplog)


async def test_cancellation_disables_listeners_before_cleanup(monkeypatch):
    probes = []
    callbacks = []
    cleanup_observations = []
    original_start = login._SubmitProbe.start

    def start(probe, page, deadline):
        original_start(probe, page, deadline)
        probes.append(probe)
        callbacks.extend(probe._listeners)

    monkeypatch.setattr(login._SubmitProbe, 'start', start)

    async def cancel(page):
        raise asyncio.CancelledError()

    page = DiagnosticPage(on_submit=cancel)
    context, browser = _install_fake_browser(monkeypatch, page)
    original_page_close, original_context_close = page.close, context.close

    def observe_cleanup(resource):
        probe = probes[0]
        before = dict(probe.counts)
        detached = all(callback not in page.listeners.get(event, ()) for event, callback in callbacks)
        # Retained callbacks must also be inert before close starts. Record the
        # evidence here: assertions inside close could be swallowed by cleanup.
        for _event, callback in callbacks:
            callback(Request())
        page.emit('pageerror', RuntimeError(SENTINEL))
        cleanup_observations.append((resource, probe.active, detached, before, dict(probe.counts)))

    async def page_close():
        observe_cleanup('page')
        await original_page_close()

    async def context_close():
        observe_cleanup('context')
        await original_context_close()

    monkeypatch.setattr(page, 'close', page_close)
    monkeypatch.setattr(context, 'close', context_close)
    with pytest.raises(asyncio.CancelledError):
        await login.login_official_ojv(SecretStr('11.111.111-1'), SecretStr(SENTINEL),
                                     proxy_url=None, user_agent='official-test-agent')
    assert [resource for resource, *_rest in cleanup_observations] == ['page', 'context']
    for resource, active, detached, before, after in cleanup_observations:
        assert not active, f'submit probe still active before {resource} cleanup'
        assert detached, f'submit listeners still attached before {resource} cleanup'
        assert before == after, f'submit callbacks changed counters during {resource} cleanup'
    assert page.listeners.get('pageerror') == []
    assert context.closed and browser.closed


def test_probe_is_bounded_stops_even_when_detach_fails_and_retains_no_browser_objects():
    assert hasattr(login, '_SubmitProbe'), 'missing bounded submit probe'
    probe = login._SubmitProbe()
    page = DiagnosticPage()
    probe.start(page, time.monotonic() + 999999)
    callbacks = [callback for callbacks in page.listeners.values() for callback in callbacks]
    for index in range(1100):
        page.emit('request', Request())
        page.emit('requestfailed', Request())
        page.emit('pageerror', RuntimeError(SENTINEL))
        page.emit('response', SimpleNamespace(status=700 + index))
    assert probe.counts['started_fetch'] == 999
    assert probe.counts['js_errors'] == 999
    assert probe.counts['http_other'] == 999
    assert probe.click_remaining_ms == 45000
    page.remove_listener = lambda *_: (_ for _ in ()).throw(RuntimeError(SENTINEL))
    probe.stop(page, time.monotonic() - 1)
    before = probe.summary()
    for callback in callbacks:
        callback(Request())
    assert probe.summary() == before
    assert probe.return_remaining_ms == 0
    assert SENTINEL not in repr(vars(probe))
    assert not any(value is page or isinstance(value, Request) for value in vars(probe).values())


async def test_unobserved_expired_resolver_is_not_reported_as_checked_none():
    assert hasattr(login, '_SubmitProbe'), 'missing bounded submit probe'
    probe = login._SubmitProbe()
    result = await login._resolve_post_submit(DiagnosticPage(), time.monotonic() - 1, probe)
    assert isinstance(result, OjvTimeoutError)
    assert probe.inspection == 'inspection_unavailable'
    assert probe.counts['inspection_attempts'] == 0


@pytest.mark.parametrize('location,expected', [
    ('https://oficinajudicialvirtual.pjud.cl/home/index.php?x=' + SENTINEL, 'official_entry'),
    ('https://oficinajudicialvirtual.pjud.cl/indexN.php?x=' + SENTINEL, 'official_landing'),
    ('https://oficinajudicialvirtual.pjud.cl/other?' + SENTINEL, 'official_other'),
    ('https://evil.invalid/' + SENTINEL, 'untrusted'),
    ('https://oficinajudicialvirtual.pjud.cl:invalid/' + SENTINEL, 'unavailable'),
])
def test_submit_location_is_closed(location, expected):
    assert hasattr(login, '_submit_location'), 'missing closed location classifier'
    assert login._submit_location(SimpleNamespace(url=location)) == expected


@pytest.mark.parametrize('rejection,challenge,expected', [(True, False, 'rejection'), (False, True, 'challenge')])
async def test_classifier_observations_preserve_existing_error_class(rejection, challenge, expected):
    from app.ojv.errors import InvalidCredentialsError, FamiliaBlockedError

    class ClassifiedAlerts(Alerts):
        async def all_inner_texts(self):
            return ['RUT o clave incorrecta ' + SENTINEL] if rejection else []

        async def count(self):
            return int(challenge)

    page = SimpleNamespace(url=login._OFFICIAL_ENTRY, locator=lambda _: ClassifiedAlerts())
    probe = login._SubmitProbe()
    result = await login._resolve_post_submit(page, time.monotonic() + .1, probe)
    assert isinstance(result, InvalidCredentialsError if rejection else FamiliaBlockedError)
    assert probe.inspection == expected
    assert SENTINEL not in probe.summary()


async def test_probe_does_not_change_success_when_listener_registration_fails(monkeypatch):
    async def submit(page):
        page.url = login._OFFICIAL_LANDING

    page = DiagnosticPage(on_submit=submit)
    original_on = page.on

    def broken_on(name, callback):
        if name in {'pageerror', 'requestfailed', 'requestfinished'}:
            raise RuntimeError(SENTINEL)
        original_on(name, callback)

    page.on = broken_on
    context, browser = _install_fake_browser(monkeypatch, page)
    result = await login.login_official_ojv(SecretStr('11.111.111-1'), SecretStr(SENTINEL),
                                          proxy_url=None, user_agent='official-test-agent')
    assert result.cookies[0].name == 'AUTH'
    assert context.closed and browser.closed


def test_finite_event_families_and_malformed_event_safety():
    probe = login._SubmitProbe()
    page = DiagnosticPage()
    probe.start(page, time.monotonic() + 1)
    original_keys = set(probe.counts)
    for resource, navigation, expected in [('xhr', False, 'fetch'), ('fetch', True, 'navigation'), (SENTINEL, False, 'other')]:
        request = SimpleNamespace(resource_type=resource, is_navigation_request=lambda: navigation)
        page.emit('request', request)
        assert probe.counts['started_' + expected] == 1
    for status, expected in [(100, '1xx'), (200, '2xx'), (300, '3xx'), (400, '4xx'), (500, '5xx'), (SENTINEL, 'other')]:
        page.emit('response', SimpleNamespace(status=status))
        assert probe.counts['http_' + expected] == 1
    for code, expected in [('ERR_NAME_NOT_RESOLVED', 'dns'), ('ERR_PROXY_CONNECTION_FAILED', 'proxy'), ('ERR_TIMED_OUT', 'timeout'), ('ERR_ABORTED', 'aborted'), (SENTINEL, 'other')]:
        request = Request()
        request.failure = 'net::' + code + ' ' + SENTINEL
        page.emit('requestfailed', request)
        assert probe.counts['transport_' + expected] == 1
    page.emit('request', object())
    assert probe.counts['observer_errors'] == 1
    assert set(probe.counts) == original_keys
    assert SENTINEL not in probe.summary()
    probe.stop(page, time.monotonic() + .5)
    assert 0 <= probe.return_remaining_ms <= probe.click_remaining_ms <= 1000
    assert 0 <= probe.elapsed_ms <= 1000


def test_probe_records_existing_deadline_without_allocating_a_new_budget(monkeypatch):
    from app.ojv import submit_diagnostics

    monkeypatch.setattr(submit_diagnostics.time, 'monotonic', lambda: 10.0)
    probe = login._SubmitProbe()
    page = DiagnosticPage()
    probe.start(page, 20.0)
    monkeypatch.setattr(submit_diagnostics.time, 'monotonic', lambda: 12.0)
    probe.stop(page, 20.0)
    assert probe.click_remaining_ms == 10000
    assert probe.return_remaining_ms == 8000
    assert probe.elapsed_ms == 2000


async def test_timeout_during_click_stops_probe_without_extending_login_budget(monkeypatch, caplog):
    async def stalled_click(page):
        page.emit('pageerror', RuntimeError(SENTINEL))
        await asyncio.sleep(1)

    page = DiagnosticPage(on_submit=stalled_click)
    await run_timeout(monkeypatch, page)
    message = diagnostic_record(caplog)
    assert 'stage=submit ' in message
    assert 'submit_return_remaining_ms=0 ' in message
    assert 'submit_inspection_attempts=0 ' in message
    assert 'submit_js_errors=1 ' in message
    assert page.listeners['pageerror'] == []


async def test_landing_and_cleanup_events_are_outside_submit_window(monkeypatch):
    observed = []
    original_stop = login._SubmitProbe.stop

    def stop(probe, page, deadline):
        original_stop(probe, page, deadline)
        observed.append(probe)

    monkeypatch.setattr(login._SubmitProbe, 'stop', stop)

    async def submit(page):
        page.url = login._OFFICIAL_LANDING

    page = DiagnosticPage(on_submit=submit)
    original_locator = page.locator

    def landing_locator(selector):
        if selector == 'a[href="#infousuario"]':
            page.emit('pageerror', RuntimeError(SENTINEL))
        return original_locator(selector)

    page.locator = landing_locator
    _install_fake_browser(monkeypatch, page)
    result = await login.login_official_ojv(SecretStr('11.111.111-1'), SecretStr(SENTINEL),
                                          proxy_url=None, user_agent='official-test-agent')
    assert result.cookies[0].name == 'AUTH'
    assert len(observed) == 1
    assert observed[0].counts['js_errors'] == 0
    assert observed[0].counts['inspection_attempts'] == 0
    assert observed[0].location == 'official_landing'
