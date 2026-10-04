"""The Linux-side Windows build tooling, tested with synthetic MSYS2 packages (no Wine, no network)."""
import hashlib
import http.server
import io
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging" / "windows"
sys.path.insert(0, str(PKG))

import assemble_bundle as ab            # noqa: E402
import bundle_check as bc               # noqa: E402
import make_manifest as mm              # noqa: E402
import make_zip as mz                   # noqa: E402
import msys2_fetch as mf                # noqa: E402

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")


# ---------------------------------------------------------------- synthetic repo
def _tar_zst(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return subprocess.run(["zstd", "-c", "-q"], input=buf.getvalue(), capture_output=True, check=True).stdout


PREFIX = "mingw-w64-ucrt-x86_64-"


def make_repo(tmp_path):
    """name -> (deps, provides, files). Returns (dir served, expected closure for 'app')."""
    spec = {
        "app": (["libb>=1.0", "virtual-c"], [], {"ucrt64/bin/app.dll": b"app", "ucrt64/share/doc/readme": b"doc"}),
        "libb": (["libd"], [], {"ucrt64/bin/libb.dll": b"b"}),
        "libc-impl": ([], ["virtual-c=1"], {"ucrt64/bin/libc-impl.dll": b"c"}),
        "libd": ([], [], {"ucrt64/bin/libd.dll": b"d"}),
        "unrelated": ([], [], {"ucrt64/bin/other.dll": b"x"}),
    }
    d = tmp_path / "repo"
    d.mkdir()
    descs = {}
    for short, (deps, provs, files) in spec.items():
        name = PREFIX + short
        fname = f"{name}-1.0-1-any.pkg.tar.zst"
        blob = _tar_zst({".PKGINFO": b"x", **files})
        (d / fname).write_bytes(blob)
        sections = {"NAME": [name], "VERSION": ["1.0-1"], "FILENAME": [fname], "SHA256SUM": [hashlib.sha256(blob).hexdigest()],
                    "DEPENDS": [PREFIX + x if "virtual" not in x else x for x in deps],
                    "PROVIDES": provs}
        descs[f"{name}-1.0-1/desc"] = "".join(f"%{k}%\n" + "\n".join(v) + "\n\n" for k, v in sections.items() if v).encode()
    (d / "ucrt64.db").write_bytes(_tar_zst(descs))
    return d, {PREFIX + n for n in ("app", "libb", "libd", "libc-impl")}


@pytest.fixture
def served(tmp_path):
    repo, closure = make_repo(tmp_path)
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(repo), **k)   # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", repo, closure
    srv.shutdown()


# --------------------------------------------------------------------- msys2_fetch
@needs_zstd
def test_db_parse_and_dependency_closure_with_versions_and_provides(served):
    url, repo, closure = served
    pkgs = mf.parse_db((repo / "ucrt64.db").read_bytes())
    assert set(mf.resolve(pkgs, [PREFIX + "app"])) == closure                    # follows >= deps and virtual provides
    assert PREFIX + "unrelated" not in mf.resolve(pkgs, [PREFIX + "app"])


@needs_zstd
def test_unresolvable_dependency_is_a_clear_error(served):
    pkgs = mf.parse_db((served[1] / "ucrt64.db").read_bytes())
    pkgs[PREFIX + "app"]["depends"].append("does-not-exist")
    with pytest.raises(mf.FetchError, match="does-not-exist"):
        mf.resolve(pkgs, [PREFIX + "app"])


@needs_zstd
def test_lock_then_hash_verified_download_and_extract(served, tmp_path):
    url, repo, closure = served
    packages = tmp_path / "packages.txt"
    packages.write_text("# roots\napp\n")
    args = ["--lock", str(tmp_path / "msys2.lock"), "--packages", str(packages), "--cache", str(tmp_path / "cache"),
            "--stage", str(tmp_path / "stage"), "--repo-urls", url, "--prefix", PREFIX, "--env", "ucrt64"]
    assert mf.main(args) == 0                                                      # no lock yet -> creates it
    lock = (tmp_path / "msys2.lock").read_text()
    assert all(n in lock for n in closure) and "unrelated" not in lock
    stage = tmp_path / "stage" / "ucrt64"
    assert (stage / "bin" / "libd.dll").read_bytes() == b"d" and not (tmp_path / "stage" / ".PKGINFO").exists()
    # second run uses the lock only: even with the database gone from the mirror it works
    (repo / "ucrt64.db").unlink()
    shutil.rmtree(tmp_path / "stage")
    assert mf.main(args) == 0 and (stage / "bin" / "libb.dll").exists()


@needs_zstd
def test_tampered_package_is_rejected(served, tmp_path):
    url, repo, _ = served
    packages = tmp_path / "packages.txt"
    packages.write_text("app\n")
    args = ["--lock", str(tmp_path / "msys2.lock"), "--packages", str(packages), "--cache", str(tmp_path / "cache"),
            "--stage", str(tmp_path / "stage"), "--repo-urls", url, "--prefix", PREFIX]
    assert mf.main(args) == 0
    victim = next(repo.glob("*libd*"))
    victim.write_bytes(victim.read_bytes() + b"tampered")
    shutil.rmtree(tmp_path / "cache" / victim.name, ignore_errors=True)
    (tmp_path / "cache" / victim.name).unlink(missing_ok=True)
    shutil.rmtree(tmp_path / "stage")
    assert mf.main(args) == 1                                                      # hash from the lock no longer matches


def test_lock_file_format_is_validated(tmp_path):
    bad = tmp_path / "x.lock"
    bad.write_text("pkg.tar.zst nothex name\n")
    with pytest.raises(mf.FetchError):
        mf.read_lock(bad)
    bad.write_text("# only comments\n")
    with pytest.raises(mf.FetchError):
        mf.read_lock(bad)


# ----------------------------------------------------------------- assemble/check
def test_prune_rules_keep_runtime_and_drop_development_files():
    keep = ["bin/libgtk-4-1.dll", "bin/python.exe", "lib/python3.12/os.py", "lib/girepository-1.0/Gtk-4.0.typelib",
            "share/glib-2.0/schemas/org.gtk.Settings.gschema.xml", "share/icons/Adwaita/index.theme", "etc/fonts/fonts.conf",
            "lib/python3.12/site-packages/gi/__init__.py", "lib/gdk-pixbuf-2.0/2.10.0/loaders/libpixbufloader-svg.dll"]
    drop = ["bin/gtk4-demo.exe", "bin/pip.exe", "include/gtk-4.0/gtk/gtk.h", "lib/libgtk-4.dll.a", "lib/pkgconfig/gtk4.pc",
            "share/locale/de/LC_MESSAGES/gtk40.mo", "share/doc/gtk4/readme", "lib/python3.12/test/test_os.py",
            "lib/python3.12/idlelib/x.py", "lib/python3.12/site-packages/psutil/tests/t.py", "lib/tcl8.6/init.tcl"]
    assert [r for r in keep if ab.skip_file(r)] == []
    assert [r for r in drop if not ab.skip_file(r)] == []


def fake_runtime(root: Path) -> Path:
    files = {
        "bin/python.exe": b"MZpy", "bin/pythonw.exe": b"MZpyw", "bin/libgtk-4-1.dll": b"g", "bin/libadwaita-1-0.dll": b"a",
        "bin/libglib-2.0-0.dll": b"g", "bin/libgobject-2.0-0.dll": b"g", "bin/libgio-2.0-0.dll": b"g",
        "bin/libgirepository-2.0-0.dll": b"g", "bin/libcairo-2.dll": b"c", "bin/libgdk_pixbuf-2.0-0.dll": b"p",
        "bin/gdk-pixbuf-query-loaders.exe": b"MZq", "bin/gtk4-demo.exe": b"nope",
        "lib/python3.12/os.py": b"", "lib/python3.12/site-packages/gi/__init__.py": b"",
        "lib/python3.12/site-packages/psutil/__init__.py": b"", "lib/python3.12/site-packages/cryptography/__init__.py": b"",
        "lib/python3.12/test/x.py": b"",
        "share/icons/Adwaita/index.theme": b"[Icon Theme]",
        "share/glib-2.0/schemas/org.test.gschema.xml": b'<schemalist><schema id="org.test" path="/org/test/"><key name="k" type="b"><default>true</default></key></schema></schemalist>',
    }
    for t in ("Gtk-4.0", "Adw-1", "Gdk-4.0", "GLib-2.0", "GObject-2.0", "Gio-2.0", "Pango-1.0", "cairo-1.0", "GdkPixbuf-2.0"):
        files[f"lib/girepository-1.0/{t}.typelib"] = b"t"
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


@pytest.mark.skipif(shutil.which("glib-compile-schemas") is None or shutil.which("rsvg-convert") is None
                    or shutil.which("convert") is None, reason="needs glib-compile-schemas, rsvg-convert, ImageMagick")
def test_assemble_produces_a_complete_relocatable_bundle(tmp_path):
    runtime = fake_runtime(tmp_path / "ucrt64")
    lock = tmp_path / "msys2.lock"
    lock.write_text("# lock\n")
    out = tmp_path / "ScreenTime"
    rc = ab.main(["--runtime", str(runtime), "--repo", str(ROOT), "--out", str(out), "--version", "9.9.9",
                  "--lock", str(lock), "--work", str(tmp_path / "work")])
    assert rc == 0
    assert bc.required_files(out) == []                                         # every runtime file GTK/Python need is present
    assert not (out / "bin" / "gtk4-demo.exe").exists() and not (out / "lib" / "python3.12" / "test").exists()
    assert (out / "share/glib-2.0/schemas/gschemas.compiled").stat().st_size > 0   # schemas compiled
    assert (out / "bin/screentime-gui.exe").read_bytes() == (out / "bin/pythonw.exe").read_bytes()
    assert (out / "lib/python3.12/site-packages/screentime-app.pth").read_text().strip() == "../../../app"
    assert not (out / "app/screentime/resources/gnome-extension").exists()         # Linux-only helpers stay out
    ico = (out / "screentime.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00" and int.from_bytes(ico[4:6], "little") >= 5     # real multi-size .ico
    assert b"\r\n" in (out / "ScreenTime.cmd").read_bytes()                        # batch files need CRLF
    assert "bin\\screentime-gui.exe" in (out / "ScreenTime.cmd").read_text()


def test_bundle_check_reports_missing_files(tmp_path):
    fake_runtime(tmp_path)
    missing = bc.required_files(tmp_path)
    assert "screentime.ico" in missing and "app/screentime/daemon.py" in missing


def test_pe_import_parser_on_a_real_windows_executable():
    pytest.importorskip("pefile")
    imports = bc.pe_imports(ROOT / "tests" / "data" / "sample_pe_x64.exe")        # setuptools' launcher (MIT)
    assert "kernel32.dll" in imports and "vcruntime140.dll" in imports
    assert bc.is_system("KERNEL32.dll") and bc.is_system("api-ms-win-crt-runtime-l1-1-0.dll")
    assert not bc.is_system("vcruntime140.dll")                                   # MSVC runtime is NOT part of Windows


def test_dependency_check_flags_a_missing_dll_and_accepts_bundled_and_system_ones(tmp_path):
    (tmp_path / "bin").mkdir()
    for n in ("libgtk-4-1.dll", "libglib-2.0-0.dll", "python.exe"):
        (tmp_path / "bin" / n).write_bytes(b"MZ")
    graph = {"libgtk-4-1.dll": {"libglib-2.0-0.dll", "kernel32.dll", "libpango-1.0-0.dll"},
             "libglib-2.0-0.dll": {"msvcrt.dll", "api-ms-win-crt-heap-l1-1-0.dll"}, "python.exe": {"libgtk-4-1.dll"}}
    missing = bc.check_dependencies(tmp_path, imports=lambda p: graph[p.name])
    assert missing == {"libpango-1.0-0.dll": {"bin/libgtk-4-1.dll"}}
    assert bc.check_dependencies(tmp_path, imports=lambda p: graph[p.name], extra_system=["libpango-1.0-0"]) == {}


def test_unparseable_pe_is_reported_not_ignored(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "broken.dll").write_bytes(b"not a pe")
    bad = bc.check_dependencies(tmp_path)
    assert any(k.startswith("<unparseable") for k in bad)


# ----------------------------------------------------------- zip / manifest / versions
def test_zip_is_deterministic_and_has_one_top_folder(tmp_path):
    b = tmp_path / "b"
    (b / "bin").mkdir(parents=True)
    (b / "bin" / "a.dll").write_bytes(b"A" * 1000)
    (b / "ScreenTime.cmd").write_bytes(b"x")
    z1, z2 = tmp_path / "1.zip", tmp_path / "2.zip"
    mz.make_zip(b, z1, 1_700_000_000)
    os.utime(b / "bin" / "a.dll", (1, 1))                                        # different mtime must not matter
    mz.make_zip(b, z2, 1_700_000_000)
    assert z1.read_bytes() == z2.read_bytes()
    names = zipfile.ZipFile(z1).namelist()
    assert all(n.startswith("ScreenTime/") for n in names) and "ScreenTime/ScreenTime.cmd" in names


def test_manifest_has_hashes_and_refuses_missing_artifacts(tmp_path):
    with pytest.raises(SystemExit):
        mm.build(tmp_path, "1.2.3", "abc", {})
    (tmp_path / "ScreenTime-1.2.3-setup.exe").write_bytes(b"MZ setup")
    (tmp_path / "ScreenTime-1.2.3-portable.zip").write_bytes(b"PK zip")
    text = mm.build(tmp_path, "1.2.3", "abc123-dirty", {"wine": "wine-9.0"})
    kv = dict(line.split("=", 1) for line in text.splitlines())
    assert kv["version"] == "1.2.3" and kv["commit"] == "abc123-dirty" and kv["wine"] == "wine-9.0"
    assert kv["installer_sha256"] == hashlib.sha256(b"MZ setup").hexdigest()
    assert kv["portable_sha256"] == hashlib.sha256(b"PK zip").hexdigest()
    assert (tmp_path / "SHA256SUMS").read_text().count("\n") == 2


def test_artifact_names_follow_the_project_version():
    import screentime
    script = (ROOT / "scripts" / "build-windows.sh").read_text()
    assert "ScreenTime-$VERSION-setup.exe" in script and "ScreenTime-$VERSION-portable.zip" in script
    assert "1.4.0" not in script and screentime.__version__ not in script           # never hard-coded
    iss = (PKG / "installer.iss").read_text()
    assert "OutputBaseFilename=ScreenTime-{#AppVersion}-setup" in iss


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shell_scripts_pass_shellcheck():
    for s in (ROOT / "scripts" / "build-windows.sh", PKG / "container" / "build.sh"):
        r = subprocess.run(["shellcheck", "-x", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout


def test_host_script_fails_clearly_without_a_container_engine(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "dirname").symlink_to(shutil.which("dirname"))          # everything except podman/docker
    bash = shutil.which("bash")
    r = subprocess.run([bash, str(ROOT / "scripts" / "build-windows.sh")], capture_output=True, text=True,
                       env={"PATH": str(bindir), "HOME": str(tmp_path)})
    assert r.returncode != 0 and "no container engine found" in r.stderr and "pacman -S podman" in r.stderr
    r = subprocess.run([bash, str(ROOT / "scripts" / "build-windows.sh"), "--nonsense"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode != 0 and "unknown option" in r.stderr


def test_packaging_inputs_are_consistent():
    env = (PKG / "toolchain.env").read_text()
    assert "INNO_URL=" in env and "MSYS2_ENV=ucrt64" in env
    roots = [l for l in (PKG / "packages.txt").read_text().splitlines() if l and not l.startswith("#")]
    assert {"python", "python-gobject", "gtk4", "libadwaita", "librsvg", "adwaita-icon-theme"} <= set(roots)
    assert (ROOT / "scripts" / "build-windows.sh").stat().st_mode & 0o111
    assert (PKG / "container" / "build.sh").stat().st_mode & 0o111
