import re

import httpx
import pytest
from playwright.sync_api import sync_playwright

from tests.test_mobile_release import _LiveServer


pytestmark = [pytest.mark.browser, pytest.mark.integration]


ENGLISH_GERMAN_LEAKS = re.compile(
    r"\b(?:Aktualisieren|Entfernen|Einstellungen|Geräteeinstellungen|Geräte|"
    r"Wiedergabe|Lautstärke|Bestätigung|Vorschau|Abbrechen|Änderungen|"
    r"Senderlogo|Radios suchen|Keine Änderung|Noch kein|Bitte zuerst|"
    r"Sicherung|Aufräumen|Protokolle|Hauptradio|Weitere Räume|Ausgewählte|"
    r"Anwenden|Fehlgeschlagen|Gespeichert|Radio wählen|Zuerst|Schritt|"
    r"Quelle|Zustand|Darstellung|Hardwarevalidierung|Voraussetzungen|"
    r"Erforderlich|Nicht erforderlich|Vollständig erkannt|Öffnen|Schreiben|"
    r"Geführte|Geräteschutz|Erreichbare|Sicherer|Optionale|Identität|Abschluss|"
    r"der|die|das|und|wird|werden|kann|keine|kein|noch|auswählen|"
    r"gespeichert|prüfen|anzeigen|hinzufügen|löschen|zurücksetzen|"
    r"nur|für|mit|ohne|einem|einer|einen|bereits|gefunden|verbunden)\b",
    re.IGNORECASE,
)

GERMAN_ENGLISH_LEAKS = re.compile(
    r"\b(?:Configured radios|Add radio|Device Settings|Target radio|"
    r"Current Slots|Online station search|Remote Control|No devices configured|"
    r"No usable devices|Select a radio|Preview only|Review backup|"
    r"Factory reset was not sent|Search failed|No online search results|"
    r"Reload|Refresh radios|Apply changes|Save settings|Manual only|"
    r"Read status|Current state|Download|Remove member|Search connected radios|"
    r"the|and|is|are|from|with|without|only|select|choose|add|remove|save|"
    r"search|load|available|settings|device|devices|station|playback|"
    r"preview|failed|success|open|close|cancel|continue|started|finished)\b",
    re.IGNORECASE,
)


def _visible_view_strings(page) -> list[str]:
    return page.locator(".view.is-active").evaluate(
        """(view) => {
          const values = [];
          const walker = document.createTreeWalker(view, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const node = walker.currentNode;
            const parent = node.parentElement;
            if (!parent || parent.closest('script, style, code, pre:not([data-i18n-static])')) continue;
            const style = getComputedStyle(parent);
            if (!parent.getClientRects().length || style.display === 'none' || style.visibility === 'hidden') continue;
            const value = node.textContent.replace(/\\s+/g, ' ').trim();
            if (value && !value.startsWith('{') && !value.startsWith('[')) values.push(value);
          }
          view.querySelectorAll('input[placeholder], textarea[placeholder], [title], [aria-label]').forEach((node) => {
            if (!node.getClientRects().length || getComputedStyle(node).display === 'none' || getComputedStyle(node).visibility === 'hidden') return;
            for (const name of ['placeholder', 'title', 'aria-label']) {
              const value = (node.getAttribute(name) || '').replace(/\\s+/g, ' ').trim();
              if (value) values.push(value);
            }
          });
          return values;
        }"""
    )


def _language_leaks(page, pattern: re.Pattern[str]) -> list[str]:
    leaks: set[str] = set()
    buttons = page.locator(".topnav > .nav-button:visible")
    for index in range(buttons.count()):
        button = buttons.nth(index)
        button.click()
        for line in _visible_view_strings(page):
            if pattern.search(line):
                leaks.add(line.strip())
    return sorted(leaks)


@pytest.mark.parametrize("mode", ["easy", "standard", "lab"])
def test_english_and_german_visible_ui_are_consistent_in_every_mode(mode):
    with _LiveServer() as server, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        for language, pattern in (("en", ENGLISH_GERMAN_LEAKS), ("de", GERMAN_ENGLISH_LEAKS)):
            response = httpx.post(
                f"{server.url}/api/system/settings",
                json={
                    "web_language": language,
                    "ui_mode": mode,
                    "show_startup_warning": "false",
                    "first_run_warning_required": "false",
                },
                timeout=3,
            )
            response.raise_for_status()
            page.goto(server.url, wait_until="networkidle")
            page.wait_for_function("(code) => document.documentElement.lang === code", arg=language)
            page.wait_for_timeout(100)
            leaks = _language_leaks(page, pattern)
            assert not leaks, f"{language}/{mode} visible translation leaks: {leaks}"
        browser.close()


@pytest.mark.parametrize(
    ("language", "source", "expected", "preserved_code"),
    [
        ("en", "Routing: active · Wiedergabe gesperrt · PLAYBACK_FAILED", "playback blocked", "PLAYBACK_FAILED"),
        ("en", "Letzter Sync: unbekannt · Quelle: LOCAL_INTERNET_RADIO", "Last sync: unknown", "LOCAL_INTERNET_RADIO"),
        ("de", "using persisted radio snapshot · PLAYBACK_FAILED", "gespeicherter Radio-Snapshot wird verwendet", "PLAYBACK_FAILED"),
        ("de", "physical preset-button playback requires a manual step", "Wiedergabe über die physische Presettaste erfordert einen manuellen Schritt", ""),
    ],
)
def test_dynamic_runtime_copy_is_localized_without_changing_protocol_codes(language, source, expected, preserved_code):
    with _LiveServer() as server, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        httpx.post(
            f"{server.url}/api/system/settings",
            json={"web_language": language, "show_startup_warning": "false", "first_run_warning_required": "false"},
            timeout=3,
        ).raise_for_status()
        page.goto(server.url, wait_until="networkidle")
        page.evaluate(
            """value => {
              const node = document.createElement('p');
              node.id = 'runtime-localization-fixture';
              node.textContent = value;
              document.querySelector('.view.is-active').append(node);
            }""",
            source,
        )
        page.wait_for_function(
            "expected => document.querySelector('#runtime-localization-fixture')?.textContent.includes(expected)",
            arg=expected,
        )
        rendered = page.locator("#runtime-localization-fixture").inner_text()
        assert expected in rendered
        if preserved_code:
            assert preserved_code in rendered
        browser.close()
