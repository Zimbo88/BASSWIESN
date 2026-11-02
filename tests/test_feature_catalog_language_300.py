"""Coverage of human-readable feature-catalogue values, including inactive states."""
import ast
import json
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit


def test_all_catalogue_literals_have_full_phrase_translations():
    tree = ast.parse(Path("basswiesn/app/services/feature_status.py").read_text())
    fields = {"title", "category", "description", "maturity", "hardware_status", "blockers",
              "requirements", "security_status", "activation_method"}
    texts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_feature":
            for keyword in node.keywords:
                if keyword.arg in fields:
                    texts.update(value.value for value in ast.walk(keyword.value)
                                 if isinstance(value, ast.Constant) and isinstance(value.value, str))
        if isinstance(node, ast.Assign) and any(isinstance(value, ast.Name) and value.id.startswith("STATUS_") for value in node.targets):
            texts.add(node.value.value)
    # Protocol identifiers and established product terms, not untranslated
    # sentences. Any new unmatched text fails instead of enlarging this set.
    neutral = {"BASSWIESN_ENABLE_HTTPS=false", "BASSWIESN_EXPERIMENTAL_ANNOUNCEMENTS=false",
               "BASSWIESN_MEDIA_ENABLED=false", "BASSWIESN_STANDBY_CLOCK_RECOVERY_ENABLED=false",
               "BASSWIESN_TELNET_ENABLED=false", "BASSWIESN_WEBHOOKS_ENABLED=false",
               "Backup", "DLNA", "Discovery", "HTTPS", "Health Center", "Multiroom",
               "Restore", "SHA256", "Setup", "Support Bundle", "Updates", "Webhooks",
               "Write-Guard", "strict", "LAB"}
    program = """
const fs = require('fs'), vm = require('vm');
global.window = {};
vm.runInThisContext(fs.readFileSync('basswiesn/app/static/js/translations.js', 'utf8'));
const i18n = window.BasswiesnI18n;
const phrases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(phrases.map(text => {
  i18n.setLanguage('en'); const en = i18n.phrase(text);
  i18n.setLanguage('de'); const de = i18n.phrase(text);
  return {text, en, de};
})));
"""
    result = subprocess.run(["node", "-e", program], input=json.dumps(sorted(texts)),
                            text=True, capture_output=True, check=True, timeout=10)
    missing = [item["text"] for item in json.loads(result.stdout)
               if item["en"] == item["de"] and item["text"] not in neutral]
    assert missing == []
