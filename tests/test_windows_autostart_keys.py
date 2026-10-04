"""Task Scheduler autostart + Credential Manager / DPAPI key storage (fake OS)."""
import subprocess

import pytest

from screentime import keystore as K
from screentime.platform.windows import autostart as wa
from screentime.platform.windows import instance_lock as wl
from screentime.platform.windows import keystore as wk
from windows_fakes import FakeWin32

EXE = r"C:\Program Files\Screen Time\bin\screentime-daemon.exe"


class FakeSchtasks:
    """Models `schtasks` for the single task, and records every call."""
    def __init__(self, monkeypatch, exe=EXE):
        self.task_xml = None
        self.calls = []
        self.exe = exe
        monkeypatch.setattr(wa, "_run", self.run)
        monkeypatch.setattr(wa, "_command_parts", lambda: (self.exe, ["-m", "screentime.daemon"]))
        monkeypatch.setattr(wa, "start_now", lambda: self.calls.append(["start_now"]) or True)
        monkeypatch.setattr(wa, "daemon_is_running", lambda: False)
        monkeypatch.setattr(wa, "current_user", lambda: r"PC\Ali Veli")

    def run(self, args, input_=None, timeout=20):
        self.calls.append(args)
        op = args[1].lower() if len(args) > 1 else ""
        cp = lambda rc, out="", err="": subprocess.CompletedProcess(args, rc, out, err)   # noqa: E731
        if op == "/create":
            self.task_xml = open(args[args.index("/XML") + 1], encoding="utf-16").read()
            return cp(0)
        if op == "/query":
            return cp(0, self.task_xml) if self.task_xml else cp(1, "", "ERROR: The system cannot find the file specified.")
        if op == "/delete":
            had, self.task_xml = self.task_xml, None
            return cp(0 if had else 1)
        return cp(0)

    def creates(self):
        return [c for c in self.calls if len(c) > 1 and c[1].lower() == "/create"]


@pytest.fixture
def sch(monkeypatch):
    return FakeSchtasks(monkeypatch)


def test_task_is_per_user_logon_least_privilege_with_unlimited_runtime(sch):
    assert wa.enable() == "task-scheduler"
    x = sch.task_xml
    assert "<LogonTrigger>" in x and r"<UserId>PC\Ali Veli</UserId>" in x
    assert "<RunLevel>LeastPrivilege</RunLevel>" in x and "InteractiveToken" in x
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in x
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in x
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in x
    assert "SYSTEM" not in x and "HighestAvailable" not in x                      # never elevated
    assert ["start_now"] in sch.calls                                              # also starts it now


def test_path_with_spaces_and_arguments_round_trip(sch):
    wa.enable()
    info = wa.parse_task_xml(sch.task_xml)
    assert info.exists and info.enabled
    assert info.command == EXE and info.arguments == "-m screentime.daemon"
    assert wa.task_is_current(info)


def test_unicode_install_path_is_preserved(monkeypatch):
    s = FakeSchtasks(monkeypatch, exe=r"C:\Kullanıcılar\Ünal\ScreenTime\bin\screentime-daemon.exe")
    wa.enable()
    assert wa.parse_task_xml(s.task_xml).command == s.exe


def test_enable_twice_overwrites_one_task_never_duplicates(sch):
    wa.enable(); wa.enable()
    assert len(sch.creates()) == 2 and all("/F" in c and c[c.index("/TN") + 1] == wa.TASK_NAME for c in sch.creates())
    assert sch.task_xml.count("<Exec>") == 1


def test_disable_removes_the_task(sch):
    wa.enable()
    assert wa.is_enabled()
    wa.disable()
    assert not wa.is_enabled() and sch.task_xml is None


def test_enable_failure_raises_instead_of_pretending(sch, monkeypatch):
    monkeypatch.setattr(wa, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 5, "", "Access is denied."))
    with pytest.raises(RuntimeError, match="Access is denied"):
        wa.enable()


def test_reconcile_does_nothing_if_the_user_did_not_opt_in(sch):
    assert wa.reconcile(False) is None
    assert sch.calls == []                                   # never even queried; never re-enables a disabled startup


def test_reconcile_recreates_a_deleted_task_exactly_once(sch):
    assert wa.reconcile(True) == "task-scheduler"
    assert len(sch.creates()) == 1
    assert wa.reconcile(True) == "already-enabled"
    assert len(sch.creates()) == 1                           # healthy -> untouched


def test_reconcile_repairs_a_stale_path_after_an_upgrade_or_move(sch):
    wa.enable()
    sch.exe = r"D:\Apps\ScreenTime\bin\screentime-daemon.exe"          # installed somewhere else now
    assert not wa.task_is_current(wa.query_task())
    wa.reconcile(True)
    assert wa.parse_task_xml(sch.task_xml).command == sch.exe


def test_reconcile_repairs_a_task_disabled_in_task_scheduler_only_when_opted_in(sch):
    wa.enable()
    sch.task_xml = sch.task_xml.replace("<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>")
    assert not wa.is_enabled()
    assert wa.reconcile(False) is None and not wa.is_enabled()          # user turned it off: respected
    wa.reconcile(True)
    assert wa.is_enabled()


def test_status_reports_the_contract_fields(sch, monkeypatch):
    monkeypatch.setattr(wa.Path, "exists", lambda self: True)
    monkeypatch.setattr(wa, "task_scheduler_available", lambda: True)
    st = wa.get_status(None)
    assert (st.enabled, st.mechanism, st.task_state) == (False, None, "missing")
    wa.enable()
    st = wa.get_status(None)
    assert (st.enabled, st.mechanism, st.task_state, st.installed) == (True, "task-scheduler", "enabled", True)
    sch.exe = r"E:\moved\screentime-daemon.exe"
    assert wa.get_status(None).task_state == "stale"


def test_task_scheduler_unavailable_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(wa, "_run", lambda *a, **k: (_ for _ in ()).throw(OSError("no schtasks")))
    monkeypatch.setattr(wa, "daemon_is_running", lambda: False)
    assert wa.query_task().exists is False and wa.is_enabled() is False
    assert wa.get_status(None).task_state == "unavailable"


def test_daemon_command_matching():
    assert wa._is_daemon_cmdline([r"C:\x\bin\screentime-daemon.exe", "-m", "screentime.daemon"])
    assert wa._is_daemon_cmdline([r"C:\py\pythonw.exe", "-m", "screentime.daemon"])
    assert not wa._is_daemon_cmdline([r"C:\Windows\notepad.exe", r"C:\notes\screentime-daemon.log"])
    assert not wa._is_daemon_cmdline([])


def test_stop_now_asks_nicely_before_terminating(monkeypatch):
    api = FakeWin32()
    lock = wl.InstanceLock(api=api); lock.acquire()
    monkeypatch.setattr(wl, "default_api", lambda: api)
    monkeypatch.setattr(wa, "_find_daemon_processes", lambda: pytest.fail("must not terminate when it exits by itself"))
    import threading, time
    threading.Timer(0.15, lock.release).start()             # the daemon notices the event and exits
    assert wa.stop_now(wait_seconds=3) is True
    assert api.events[wl.stop_event_name()] is True


# --------------------------------------------------------------------- keys
SID = bytes(range(16))


@pytest.fixture
def api():
    return FakeWin32()


def km(tmp_path, api):
    return K.KeyManager(config_dir=tmp_path / "cfg", backends=wk.backend_factories(tmp_path / "cfg", api))


def test_new_key_goes_to_credential_manager_and_round_trips(tmp_path, api):
    m = km(tmp_path, api)
    key, backend = m.create_key(SID)
    assert backend == "credential-manager"
    assert api.creds[wk.CRED_TARGET_PREFIX + SID.hex()]                 # stored under a per-store target
    assert m.get_key(SID) == key
    assert m.read_settings()["backend"] == "credential-manager" and "key" not in m.read_settings()


def test_falls_back_to_dpapi_file_when_credential_manager_fails(tmp_path, api):
    api.cred_error = 1312                                               # e.g. vault service unavailable
    m = km(tmp_path, api)
    key, backend = m.create_key(SID)
    assert backend == "dpapi-file"
    f = tmp_path / "cfg" / "keys" / f"{SID.hex()}.dpapi"
    assert f.exists() and key not in f.read_bytes()                     # the raw key is not on disk
    assert m.get_key(SID) == key


def test_unavailable_vault_is_retryable_so_the_daemon_waits_instead_of_losing_data(tmp_path, api):
    m = km(tmp_path, api)
    key, _ = m.create_key(SID)
    api.cred_error = 1312
    with pytest.raises(K.KeyUnavailableError) as e:
        m.get_key(SID)
    assert e.value.retryable is True
    api.cred_error = None
    assert m.get_key(SID) == key                                        # same key comes back; never a second one


def test_corrupt_credential_is_not_silently_replaced(tmp_path, api):
    m = km(tmp_path, api)
    m.create_key(SID)
    api.creds[wk.CRED_TARGET_PREFIX + SID.hex()] = b"not base64 !!"
    with pytest.raises(K.KeyStoreError):
        m.get_key(SID)


def test_dpapi_blob_from_another_user_is_reported_not_guessed(tmp_path, api):
    api.cred_error = 1312
    m = km(tmp_path, api)
    m.create_key(SID)
    api.dpapi_error = 13
    with pytest.raises(K.KeyStoreError):
        m.get_key(SID)


def test_recovery_key_import_restores_to_the_best_backend(tmp_path, api):
    m = km(tmp_path, api)
    key = K.generate_key()
    recovery = K.encode_recovery_key(key)
    assert m.import_key(SID, K.decode_recovery_key(recovery)) == "credential-manager"
    assert m.get_key(SID) == key


def test_dpapi_key_can_be_moved_into_credential_manager(tmp_path, api):
    api.cred_error = 1312
    m = km(tmp_path, api)
    key, backend = m.create_key(SID)
    assert backend == "dpapi-file"
    api.cred_error = None
    m.keyring_backend_name = "credential-manager"
    assert m.move_to_keyring(SID) == "credential-manager"
    assert not list((tmp_path / "cfg" / "keys").glob("*.dpapi"))
    assert m.get_key(SID) == key and m.read_settings()["backend"] == "credential-manager"


def test_secrets_never_appear_in_logs(tmp_path, api, caplog):
    import base64, logging
    caplog.set_level(logging.DEBUG)
    m = km(tmp_path, api)
    key, _ = m.create_key(SID)
    m.get_key(SID)
    text = caplog.text
    assert key.hex() not in text and base64.b64encode(key).decode() not in text
    assert K.encode_recovery_key(key) not in text


# ------------------------------------------------------------------ uninstall
def test_uninstall_keeps_usage_data_by_default_and_removes_the_startup_task(sch, monkeypatch):
    from screentime.platform.windows import cleanup
    calls = []
    monkeypatch.setattr(wa, "stop_now", lambda *a, **k: calls.append("stop") or True)
    wa.enable()
    assert sch.task_xml
    assert cleanup.main(["uninstall"]) == 0
    assert sch.task_xml is None and calls == ["stop"]


def test_uninstall_with_delete_data_removes_store_key_and_logs(tmp_path, monkeypatch, api):
    from screentime import platform as plat, storage
    from screentime.platform.windows import cleanup
    monkeypatch.setenv("SCREENTIME_PLATFORM", "windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "L"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "R"))
    plat.reset_for_tests()
    try:
        dd = storage.data_dir()
        (dd / "logs").mkdir(); (dd / "logs" / "daemon.log").write_text("x")
        (dd / "screentime.sec").write_bytes(b"\0" * 64)
        monkeypatch.setattr(storage, "read_store_id", lambda p: SID)
        km_ = K.KeyManager()
        wk.CredentialManagerBackend(api).store(SID, K.generate_key())
        (km_.config_dir).mkdir(parents=True)
        (km_.config_dir / "security.json").write_text("{}")
        cleanup.delete_user_data(print, api)
        assert api.creds == {}                                          # key gone from Credential Manager
        assert not (dd / "screentime.sec").exists() and not (dd / "logs").exists()
        assert not (km_.config_dir / "security.json").exists()
    finally:
        monkeypatch.delenv("SCREENTIME_PLATFORM")
        plat.reset_for_tests()
