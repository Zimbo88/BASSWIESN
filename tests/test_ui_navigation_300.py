"""Human-style navigation checks against a loopback-only isolated application."""
import os
import json
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright
from basswiesn.app.routers import api

from test_mobile_release import _LiveServer


pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('mode', ['standard', 'lab'])
@pytest.mark.parametrize('viewport', [(430, 932), (932, 430), (390, 667), (320, 568)])
def test_more_menu_every_visible_entry_accepts_real_click(engine, mode, viewport, monkeypatch):
    monkeypatch.setattr(api, '_tcp_port_open', lambda *_args, **_kwargs: (False, 'offline fixture'))
    with _LiveServer() as server, sync_playwright() as playwright:
        response = httpx.post(server.url + '/api/system/settings', json={
            'ui_mode': mode, 'web_language': 'en',
            'show_startup_warning': 'false', 'first_run_warning_required': 'false',
        })
        assert response.status_code == 200
        browser = getattr(playwright, engine).launch(headless=True)
        page = browser.new_page(viewport={'width': viewport[0], 'height': viewport[1]},
                                is_mobile=True, has_touch=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        # Even browser-side external image/service fetches stay off the LAN.
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url + '/')
                   else route.abort())
        page.goto(server.url, wait_until='networkidle')
        page.wait_for_function('(mode) => document.body.classList.contains(mode + "-mode")', arg=mode)
        menu = page.locator('.advanced-nav')
        summary = menu.locator('summary')
        summary.click()
        menu.locator('.nav-button:visible').first.wait_for(state='visible')
        entries = menu.locator('.nav-button:visible').evaluate_all('(buttons) => buttons.map(b => b.dataset.view)')
        assert entries
        assert ("media" in entries) is (mode == "lab")
        assert page.locator('#update-admin').evaluate('node => node.classList.contains("lab-only")')
        for view in entries:
            target = menu.locator(f'.nav-button[data-view="{view}"]')
            target.scroll_into_view_if_needed()
            # A bounding box alone misses overlap by the sticky toolbar.
            assert target.evaluate('''node => {
                const box = node.getBoundingClientRect();
                const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
                return box.top >= 0 && box.bottom <= innerHeight && (hit === node || node.contains(hit));
            }'''), f'{engine}/{mode}/{viewport}: {view} is covered or outside viewport'
            target.click(timeout=3000)
            assert page.locator(f'#view-{view}.is-active').count() == 1
            assert not menu.evaluate('node => node.open')
            summary.click()
            menu.locator('.nav-button:visible').first.wait_for(state='visible')
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        output = os.environ.get('BASSWIESN_UI_ARTIFACT_DIR')
        if output:
            folder = Path(output)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f'more-{engine}-{mode}-{viewport[0]}x{viewport[1]}.png'))
        page.evaluate('''() => { document.querySelector('#operation-overlay').hidden = false;
                                syncBodyScrollLock(); }''')
        assert not menu.evaluate('node => node.open')
        page.locator('#operation-overlay-close').click()
        assert page.locator('#operation-overlay').is_hidden()
        assert errors == []
        browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_themes_lab_boundaries_and_remote_controls_first(engine, language, monkeypatch):
    monkeypatch.setattr(api, '_tcp_port_open', lambda *_args, **_kwargs: (False, 'offline fixture'))
    with _LiveServer() as server, sync_playwright() as playwright:
        settings = {'ui_mode': 'standard', 'web_language': language,
                    'show_startup_warning': 'false', 'first_run_warning_required': 'false'}
        assert httpx.post(server.url + '/api/system/settings', json=settings).status_code == 200
        browser = getattr(playwright, engine).launch(headless=True)
        page = browser.new_page(viewport={'width': 430, 'height': 932}, is_mobile=True, has_touch=True)
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url + '/') else route.abort())
        page.goto(server.url, wait_until='networkidle')
        for mode in ['easy', 'standard', 'lab']:
            page.locator('#ui-mode-switch').select_option(mode)
            page.wait_for_function('(mode) => document.body.classList.contains(mode + "-mode")', arg=mode)
            assert page.locator('#local-test-center').evaluate('node => node.closest(".view").id') == 'view-lab'
            if mode != 'easy':
                page.locator('.advanced-nav summary').click()
                page.locator('.advanced-nav .nav-button:visible').first.wait_for(state='visible')
                assert page.locator('.advanced-nav [data-view="lab"]').is_visible() is (mode == 'lab')
                page.keyboard.press('Escape')
            for theme in ['light', 'dark']:
                page.locator('[data-theme-select]').select_option(theme)
                assert page.locator('html').get_attribute('data-theme') == theme
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Wait for the selected theme to be painted before reading
                # descendant computed colours, including WebKit's hidden panels.
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                contrast = page.evaluate('''() => {
                  const canvas = document.createElement('canvas');
                  canvas.width = canvas.height = 1;
                  const ctx = canvas.getContext('2d', {willReadFrequently: true});
                  const rgb = value => {
                    // color-mix can serialize to color(srgb ...) rather than
                    // rgb(...). Let the browser resolve either colour space.
                    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = value; ctx.fillRect(0, 0, 1, 1);
                    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
                    return [r, g, b, a / 255];
                  };
                  const blend = (front, back) => front.slice(0, 3).map((v, i) => v * front[3] + back[i] * (1 - front[3]));
                  const background = node => !node ? [255, 255, 255]
                    : blend(rgb(getComputedStyle(node).backgroundColor), background(node.parentElement));
                  const lum = color => color.map(c => c / 255).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4)
                    .reduce((total, c, i) => total + c * [.2126, .7152, .0722][i], 0);
                  const selectors = ['.about-release-copy p', '.setup-rebuild-safety', '.command.danger',
                    '.setup-warning-box strong', '.setup-flow-step input', '.setup-progress-item span', '.requires-ssh'];
                  // Inactive controls are exempt from the text contrast gate;
                  // WebKit uses native disabled colours inside hidden dialogs.
                  return selectors.flatMap(selector => [...document.querySelectorAll(selector)].filter(node => !node.matches(':disabled')).map(node => {
                    const bg = background(node), fg = blend(rgb(getComputedStyle(node).color), bg);
                    const a = lum(fg), b = lum(bg), ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                    return {selector, id: node.id, cssColor: getComputedStyle(node).color,
                      visible: node.getClientRects().length > 0, ratio, foreground: fg, background: bg};
                  })).filter(item => item.ratio < 4.5);
                }''')
                assert contrast == [], f'{engine}/{mode}/{theme}: {contrast}'
        page.reload(wait_until='networkidle')
        assert page.locator('html').get_attribute('data-theme') == 'dark'
        # A persisted theme never changes devices or applies any radio setting.
        writes = []
        def remote_fixture(route):
            path = route.request.url.split(server.url, 1)[-1]
            if route.request.method != 'GET':
                writes.append(path)
            replies = {
                '/api/system/settings': {'web_language': language},
                '/api/devices': [{'device_id': 'EXAMPLE', 'name': 'Example radio'}],
                '/api/stations': [], '/api/presets/EXAMPLE': [],
                '/api/devices/EXAMPLE/remote-state': {'verified': True, 'volume': 1, 'source': 'STANDBY', 'now_playing': {}},
                '/api/devices/EXAMPLE/live-reconnect': {'enabled': False},
                '/api/devices/EXAMPLE/metadata/display': {'mode': 'CUSTOM', 'fields': ['station', 'clock'],
                    'field_order': ['station', 'artist', 'title', 'clock', 'other']},
            }
            route.fulfill(status=200 if path in replies else 404, content_type='application/json', body=json.dumps(replies.get(path, {})))
        page.route('**/api/**', remote_fixture)
        page.goto(server.url + '/remote/EXAMPLE', wait_until='networkidle')
        assert page.locator('#remote-presets button').count() == 6
        assert page.locator('#remote-volume-label').inner_text() == '1%'
        assert not page.locator('#remote-details').evaluate('node => node.open')
        assert page.locator('#remote-presets').evaluate('node => !!(node.compareDocumentPosition(document.querySelector("#remote-display-panel")) & Node.DOCUMENT_POSITION_FOLLOWING)')
        assert page.locator('#remote-volume').bounding_box()['y'] < page.locator('#remote-display-panel').bounding_box()['y']
        for theme in ['light', 'dark']:
            page.locator('[data-theme-select]').select_option(theme)
            assert page.locator('html').get_attribute('data-theme') == theme
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            output = os.environ.get('BASSWIESN_UI_ARTIFACT_DIR')
            if output:
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(Path(output) / f'remote-{engine}-{language}-{theme}.png'))
        assert writes == []
        browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_preset_evidence_and_clickable_timeline_are_readonly_and_escaped(engine, language, monkeypatch):
    monkeypatch.setattr(api, '_tcp_port_open', lambda *_args, **_kwargs: (False, 'offline fixture'))
    with _LiveServer() as server, sync_playwright() as playwright:
        assert httpx.post(server.url + '/api/system/settings', json={
            'ui_mode': 'standard', 'web_language': language,
            'show_startup_warning': 'false', 'first_run_warning_required': 'false',
        }).status_code == 200
        browser = getattr(playwright, engine).launch(headless=True)
        page = browser.new_page(viewport={'width': 430, 'height': 932}, is_mobile=True, has_touch=True)
        writes = []
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: writes.append(request.url) if request.method != 'GET' else None)
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url + '/') else route.abort())
        page.goto(server.url, wait_until='networkidle')
        # loadAll chains multiple fetch batches. networkidle can fall between
        # them; wait for its final local overview before injecting fixtures.
        page.wait_for_function('document.querySelector("#local-test-summary")?.children.length > 0')
        writes.clear()
        page.evaluate('''() => {
          state.devices = [{device_id: 'EXAMPLE', protected: false}, {device_id: 'PROTECTED', protected: true}];
          for (const id of ['key-device-select', 'settings-device-select']) {
            const select = document.getElementById(id);
            select.replaceChildren(new Option('Example', 'EXAMPLE'), new Option('Protected', 'PROTECTED'));
            select.value = 'EXAMPLE';
          }
          syncRadioEntryLinks();
        }''')
        assert page.locator('#controls-remote-link').get_attribute('href') == '/remote/EXAMPLE'
        assert page.locator('#settings-display-link').get_attribute('href') == '/remote/EXAMPLE#remote-display-panel'
        assert page.locator('#controls-remote-link').text_content() == ('Kompakte Fernbedienung öffnen' if language == 'de' else 'Open compact remote')
        page.evaluate('''() => {
          for (const id of ['key-device-select', 'settings-device-select']) document.getElementById(id).value = 'PROTECTED';
          syncRadioEntryLinks();
        }''')
        for link_id in ['controls-remote-link', 'settings-display-link']:
            assert page.locator('#' + link_id).get_attribute('href') is None
            assert page.locator('#' + link_id).is_hidden()
        assert page.evaluate('''() => {
          const fixtures = [
            '<section class="panel">Provider available. Label for a healthy source.</section>',
            '<section class="panel" data-capability-status="experimental">Explicit experiment</section>',
            '<section class="panel danger-panel">Destructive action</section>',
          ].map(html => { const host = document.createElement('div'); host.innerHTML = html;
                         const node = host.firstChild; document.body.append(node); return node; });
          markRiskPanels();
          const result = !fixtures[0].classList.contains('feature-limited')
            && fixtures[1].classList.contains('feature-limited')
            && fixtures[2].classList.contains('feature-risk');
          fixtures.forEach(node => node.remove());
          return result;
        }''')
        page.locator('[data-view="presets"]').click()
        page.evaluate('''() => {
          state.presetStatus = { slots: [{button: 1, state: 'unknown', verdict: 'UNKNOWN',
            message: 'provider availability was not observed',
            radio: {title: 'Example station', source: 'LOCAL_INTERNET_RADIO'},
            basswiesn: {title: '<img src=x onerror=alert(1)>', source: 'LOCAL_INTERNET_RADIO'},
            checks: [
              {id: 'radio_readback', status: 'VALID'},
              {id: 'provider_availability', status: 'UNKNOWN'},
              {id: 'stream_reachability', status: 'VALID'},
              {id: 'hardware_button_playability', status: 'UNKNOWN', affects_verdict: false},
            ]}]};
          renderPresetChecker();
        }''')
        grid = page.locator('#preset-checker-grid')
        assert grid.locator('img').count() == 0
        assert grid.locator('details[open]').count() == 0
        assert grid.get_by_text('nicht getestet.' if language == 'de' else 'not tested.', exact=False).count() >= 1
        grid.locator('details > summary').first.click()
        text = grid.inner_text()
        assert ('kein Fehlernachweis' if language == 'de' else 'not proof of a fault') in text
        assert ('Stream-Erreichbarkeit' if language == 'de' else 'Stream reachability') in text
        assert 'provider availability was not observed' not in text
        if language == 'en':
            for german in ['Letzter Sync', 'Quelle:', 'vorhanden', 'Prüfdetails', 'Konfiguration']:
                assert german not in text
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

        page.locator('[data-view="health"]').click()
        page.evaluate('''() => {
          window.fixtureTimeline = {items: [{event_id: 'fixture-1', occurred_at: '2026-01-01T12:00:00Z',
            domain: 'REPORTING', code: 'REPORT_RESPONSE', severity: 'INFO', message: 'Report response',
            redacted: true, evidence: [{http_status: 200, nextReportIn: 60, sample: '<img src=x onerror=alert(2)>'}]}]};
          renderDiagnosticTimeline(window.fixtureTimeline);
        }''')
        timeline = page.locator('#diagnostics-timeline')
        assert timeline.locator('details[open]').count() == 0
        timeline.locator('summary').click()
        assert 'nextReportIn' in timeline.locator('pre').inner_text()
        assert '200' in timeline.locator('pre').inner_text()
        assert timeline.locator('img').count() == 0
        page.evaluate('renderDiagnosticTimeline(window.fixtureTimeline)')
        assert timeline.locator('details[open]').count() == 1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # Do not display evidence from an endpoint lacking its redaction contract.
        page.evaluate('''() => { fixtureTimeline.items[0].redacted = false;
                                renderDiagnosticTimeline(fixtureTimeline); }''')
        assert timeline.locator('pre').count() == 0
        assert writes == []
        assert errors == []
        browser.close()
