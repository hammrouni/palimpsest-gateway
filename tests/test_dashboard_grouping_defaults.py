"""The key tables open grouped by provider, and each page remembers the operator's choice."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard" / "app.js"


def _source() -> str:
    return JS.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_key_tables_default_to_grouping_by_provider():
    js = _source()

    assert "savedChoice('group.keys', 'provider'" in js
    assert "savedChoice('group.poolKeys', 'provider'" in js
    # Usage and Requests keep their own defaults (by model / ungrouped).
    assert "savedChoice('group.usage', 'model'" in js
    assert "savedChoice('group.requests', ''" in js


def test_every_grouping_choice_is_saved_when_changed():
    js = _source()

    for name in ("group.keys", "group.poolKeys", "group.usage", "group.requests"):
        assert f"saveChoice('{name}'" in js, name


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_saved_choice_falls_back_when_storage_is_missing_blocked_or_stale():
    js = _source()
    storage_start = js.index("function withStorage(")
    storage_end = js.index("\n}\n", storage_start) + 3
    choice_start = js.index("function savedChoice(")
    choice_end = js.index("/* Grouping. Rows gather", choice_start)
    script = (
        js[storage_start:storage_end]
        + js[choice_start:choice_end]
        + """
const allowed = ['', 'provider', 'state'];
const make = (store) => ({ sessionStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } } });
const blocked = { get sessionStorage() { throw new Error('blocked'); } };
const out = {};
var window = make({});
out.empty = savedChoice('group.keys', 'provider', allowed);
saveChoice('group.keys', 'state');
out.saved = savedChoice('group.keys', 'provider', allowed);
saveChoice('group.keys', '');
out.savedNone = savedChoice('group.keys', 'provider', allowed);
window = make({ 'palimpsest.group.keys': 'nonsense' });
out.stale = savedChoice('group.keys', 'provider', allowed);
window = blocked;
out.blocked = savedChoice('group.keys', 'provider', allowed);
saveChoice('group.keys', 'state');  // must not throw
console.log(JSON.stringify(out));
"""
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "empty": "provider",
        "saved": "state",
        "savedNone": "",  # an explicit "No grouping" is remembered too
        "stale": "provider",
        "blocked": "provider",
    }
