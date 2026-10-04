from pathlib import Path
from types import SimpleNamespace

from screentime.platform.windows import runtime_env as re_


def bundle(tmp_path):
    root = tmp_path / "Program Files" / "ScreenTime"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "gdk-pixbuf-query-loaders.exe").write_bytes(b"MZ")
    (root / "bin" / "screentime-gui.exe").write_bytes(b"MZ")
    ld = root / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders"
    ld.mkdir(parents=True)
    (ld / "libpixbufloader-svg.dll").write_bytes(b"MZ")
    return root


def runner(calls, out='"C:/x/svg.dll"\n'):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    return run


def test_generates_a_per_location_cache_once_and_exports_it(tmp_path):
    root, env, calls = bundle(tmp_path), {}, []
    cache_dir = tmp_path / "run"
    exe = str(root / "bin" / "screentime-gui.exe")
    cache = re_.prepare(env, exe, cache_dir, runner(calls))
    assert cache.read_text().startswith('"C:/x/svg.dll"') and env["GDK_PIXBUF_MODULE_FILE"] == str(cache)
    assert calls[0][1]["env"]["GDK_PIXBUF_MODULEDIR"].endswith("loaders")
    env2 = {}
    assert re_.prepare(env2, exe, cache_dir, runner(calls)) == cache and len(calls) == 1      # reused, not regenerated


def test_moved_bundle_gets_a_fresh_cache(tmp_path):
    a, b = bundle(tmp_path / "a"), bundle(tmp_path / "b")
    c1 = re_.prepare({}, str(a / "bin" / "screentime-gui.exe"), tmp_path / "run", runner([]))
    c2 = re_.prepare({}, str(b / "bin" / "screentime-gui.exe"), tmp_path / "run", runner([]))
    assert c1 != c2


def test_noop_outside_the_packaged_build_or_when_already_set(tmp_path):
    env = {}
    assert re_.prepare(env, str(tmp_path / "python.exe"), tmp_path, runner([])) is None and env == {}
    root = bundle(tmp_path)
    env = {"GDK_PIXBUF_MODULE_FILE": "X"}
    assert re_.prepare(env, str(root / "bin" / "screentime-gui.exe"), tmp_path / "r", runner([])) is None
    assert env["GDK_PIXBUF_MODULE_FILE"] == "X"


def test_failure_to_generate_is_not_fatal(tmp_path):
    root, env = bundle(tmp_path), {}
    bad = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom")      # noqa: E731
    assert re_.prepare(env, str(root / "bin" / "screentime-gui.exe"), tmp_path / "r", bad) is None
    assert "GDK_PIXBUF_MODULE_FILE" not in env
    def raising(cmd, **kw): raise OSError("blocked")
    assert re_.prepare(env, str(root / "bin" / "screentime-gui.exe"), tmp_path / "r", raising) is None
