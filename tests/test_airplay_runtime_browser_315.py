"""Offline browser: explicit actions, bounded profiles, no automatic retries."""
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize('language,width', [('de', 393), ('en', 1440)])
def test_runtime_controls_do_not_start_on_load_or_repeat_uncertain_write(language, width):
    scripts = {name: Path(f'basswiesn/app/static/js/{name}.js').read_text()
               for name in ('api', 'translations', 'language-extension', 'locale-301', 'airplay-runtime')}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 852})
        calls, errors = [], []
        state = {'version': 1, 'available': True, 'maturity': 'LAB', 'hardware_acceptance': 'NOT_ESTABLISHED',
            'profiles': [{'id': 'single', 'targets': ['AP2-ONE'], 'consumed': False,
                          'volume_limit': 5, 'maximum_seconds': 600}], 'sessions': []}
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route(r):
            path = r.request.url.removeprefix('https://example.invalid')
            if path == '/':
                assets = ''.join(f'<script src="/{name}.js"></script>' for name in scripts if name != 'airplay-runtime')
                return r.fulfill(content_type='text/html; charset=utf-8', body=f'<html lang="{language}"><head><meta charset="utf-8"></head><body class="lab-mode"><section class="airplay-readiness-panel"></section>{assets}<script>BasswiesnI18n.setLanguage("{language}")</script><script src="/airplay-runtime.js"></script></body></html>')
            if path.endswith('.js') and path[1:-3] in scripts:
                return r.fulfill(content_type='application/javascript; charset=utf-8', body=scripts[path[1:-3]])
            calls.append((r.request.method, path))
            if path == '/api/airplay-bridge/runtime/status':
                return r.fulfill(json={'ok': True, 'result': state})
            if path == '/api/airplay-bridge/runtime/start':
                body = r.request.post_data_json
                state['profiles'][0]['consumed'] = True
                state['sessions'] = [{'request_id': body['request_id'], 'state': 'WAITING',
                    'targets': ['AP2-ONE'], 'receipt': {}}]
                return r.abort()  # submission accepted, response lost
            if path == '/api/airplay-bridge/runtime/stop':
                assert r.request.post_data_json['request_id'] == state['sessions'][0]['request_id']
                state['sessions'][0]['state'] = 'FINISHED'
                return r.fulfill(json={'ok': True, 'result': state})
            return r.abort()
        page.route('**/*', route)
        page.goto('https://example.invalid')
        box = page.locator('#airplay-runtime')
        expect(box).to_be_visible()
        expect(box.get_by_role('button', name='Sitzung starten' if language == 'de' else 'Start session')).to_be_disabled()
        assert not calls
        box.get_by_role('button', name='Status lesen' if language == 'de' else 'Read status').click()
        expect(box.locator('select')).to_contain_text('AP2-ONE')
        box.locator('input[type=checkbox]').check()
        box.get_by_role('button', name='Sitzung starten' if language == 'de' else 'Start session').click()
        expect(box.get_by_role('button', name='Sitzung starten' if language == 'de' else 'Start session')).to_be_disabled()
        page.reload()
        assert sum(method == 'POST' for method, _ in calls) == 1
        box.get_by_role('button', name='Status lesen' if language == 'de' else 'Read status').click()
        box.get_by_role('button', name='Beenden und wiederherstellen' if language == 'de' else 'Stop and restore').click()
        box.get_by_role('button', name='Status lesen' if language == 'de' else 'Read status').click()
        expect(box).to_contain_text('Wiederherstellung bestätigt' if language == 'de' else 'restoration verified')
        assert sum(method == 'POST' for method, _ in calls) == 2
        page.evaluate('BasswiesnI18n.setLanguage("en")')
        expect(box.get_by_role('status')).to_contain_text('Local service reachable')
        expect(box.get_by_role('button', name='Start session')).to_be_disabled()
        assert not errors
        browser.close()
