"""The web side and the secure side share no code but the shared modules. See issue #120.

`pig` runs on a public-facing machine and `pigsty` runs where the data ends up. pigsty may
talk to Pig over HTTP, but it never imports Pig's modules or opens its database, and the
web service never loads the secure side's code. The package layout says so; these check
it, each in a fresh interpreter, so that what one test imported can't hide what another
needs.
"""

from __future__ import annotations

import json
import subprocess
import sys

from psych_ingestor.pig.config import TaskDefinition
from psych_ingestor.pigsty.config import PIG_TASK_SETTINGS


def loaded_after_importing(*modules: str) -> list[str]:
    """Every psych_ingestor module a fresh interpreter has loaded after importing these."""
    imports = "; ".join(f"import {module}" for module in modules)
    script = (
        f"import sys, json; {imports}; "
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.startswith('psych_ingestor'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_the_web_side_loads_nothing_from_pigsty():
    loaded = loaded_after_importing("psych_ingestor.pig.app", "psych_ingestor.pig.cli")
    assert "psych_ingestor.pig.app" in loaded
    assert [
        module for module in loaded if module.startswith("psych_ingestor.pigsty")
    ] == []


def test_pigsty_loads_nothing_from_the_web_side():
    loaded = loaded_after_importing("psych_ingestor.pigsty.cli")
    assert "psych_ingestor.pigsty.cli" in loaded
    assert [
        module
        for module in loaded
        if module == "psych_ingestor.pig" or module.startswith("psych_ingestor.pig.")
    ] == []


def test_pigsty_knows_every_pig_toml_task_setting():
    """pigsty.toml's error for a pig.toml setting in a task names it as one. pigsty can't
    ask Pig's model which settings those are, so it lists them, and this keeps the list
    honest. The fields load_config fills in aren't settings anyone writes."""
    filled_in = {"code", "source", "public_key", "signing_keys"}
    assert set(TaskDefinition.model_fields) - filled_in == PIG_TASK_SETTINGS
