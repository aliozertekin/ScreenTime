"""smoke.ps1 only runs on a Windows runner, so a parse error used to surface after a ~10 minute build.
Catch it in the Linux test job instead."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted(p for p in ROOT.rglob("*.ps1") if ".git" not in p.parts)
# "$name:" inside a double-quoted string is parsed as a scoped/drive variable ($env:X is fine, $f: is not).
BAD = re.compile(r'\$(?!(?:env|script|global|local|private|using|variable|function|alias):)[A-Za-z_]\w*:(?!:)')


def _double_quoted_strings(text):
    return re.findall(r'"(?:`.|[^"`])*"', text)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_variable_directly_followed_by_a_colon_in_strings(script):
    offenders = [s for s in _double_quoted_strings(script.read_text()) if BAD.search(s)]
    assert offenders == [], 'use "${name}:" instead of "$name:" -> ' + repr(offenders)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_powershell_parses_the_script(script):
    code = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:PS_SCRIPT,[ref]$t,[ref]$e);"
            "if($e.Count){$e|%{'{0}:{1} {2}' -f $_.Extent.File,$_.Extent.StartLineNumber,$_.Message};exit 1}")
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", code], capture_output=True, text=True,
                       env={"PS_SCRIPT": str(script), "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
                            "PATH": "/usr/bin:/bin:/opt/pwsh:" + os.environ.get("PATH", ""), "HOME": "/tmp"})
    assert r.returncode == 0, r.stdout + r.stderr
