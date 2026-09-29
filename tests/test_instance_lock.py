import os
import subprocess
import sys
import textwrap

from screentime.instance_lock import InstanceLock, is_held, lock_path


def test_first_acquires_second_is_refused():
    a, b = InstanceLock(), InstanceLock()
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()


def test_release_lets_next_instance_start():
    a = InstanceLock()
    assert a.acquire()
    a.release()
    b = InstanceLock()
    assert b.acquire() is True
    b.release()


def test_is_held_reflects_holder_and_does_not_disturb_it():
    assert is_held() is False           # no lock file yet
    a = InstanceLock()
    a.acquire()
    assert is_held() is True
    assert is_held() is True            # probing twice must not steal it
    assert InstanceLock().acquire() is False
    a.release()
    assert is_held() is False


def test_stale_lock_file_from_dead_process_does_not_block():
    """flock is released by the kernel when the holder dies, so a leftover
    file (crash / SIGKILL) must never prevent the next start."""
    code = textwrap.dedent("""
        import os, sys
        sys.path.insert(0, %r)
        from screentime.instance_lock import InstanceLock
        assert InstanceLock().acquire()
        os._exit(9)   # die without releasing, like SIGKILL
    """) % os.getcwd()
    r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ))
    assert r.returncode == 9
    assert lock_path().exists()         # stale file is left behind...
    assert is_held() is False           # ...but nobody holds it
    b = InstanceLock()
    assert b.acquire() is True
    b.release()


def test_lock_held_by_another_live_process_is_detected():
    code = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        from screentime.instance_lock import InstanceLock
        l = InstanceLock(); assert l.acquire()
        print("ready", flush=True)
        time.sleep(30)
    """) % os.getcwd()
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env=dict(os.environ))
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert is_held() is True
        assert InstanceLock().acquire() is False
    finally:
        proc.kill()
        proc.wait()
    assert is_held() is False
