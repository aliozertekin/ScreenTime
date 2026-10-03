"""Key generation, recovery keys, the key-file backend and the manager's rules."""
import base64
import os
import stat

import pytest

from screentime import keystore as K
from screentime.keystore import (KeyFileBackend, KeyManager, KeyNotFoundError, KeyStoreError,
                                 KeyUnavailableError)

SID = bytes(range(16))


# ----------------------------------------------------------------------- keys
def test_generated_keys_are_32_random_bytes_and_never_repeat():
    keys = {K.generate_key() for _ in range(200)}
    assert len(keys) == 200 and all(len(k) == 32 for k in keys)


def test_key_is_not_derived_from_anything_predictable():
    import hashlib
    k = K.generate_key()
    assert k != hashlib.sha256(b"ScreenTime").digest() and k != hashlib.sha256(b"screentime").digest()


def test_fingerprint_is_a_stable_identifier_that_is_not_the_key():
    k = K.generate_key()
    assert K.key_fingerprint(k) == K.key_fingerprint(k) and len(K.key_fingerprint(k)) == 16
    assert k.hex()[:16] != K.key_fingerprint(k) and K.key_fingerprint(k) != K.key_fingerprint(K.generate_key())


# ------------------------------------------------------------------ recovery key
def test_recovery_key_roundtrip_and_is_human_friendly():
    k = K.generate_key()
    text = K.encode_recovery_key(k)
    assert K.decode_recovery_key(text) == k
    groups = text.split("-")
    assert all(len(g) == 4 for g in groups[:-1]) and set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567-")
    assert K.decode_recovery_key(text.lower()) == k                              # case-insensitive
    assert K.decode_recovery_key("  " + text.replace("-", " ") + "\n") == k       # tolerant of spacing


def test_recovery_key_catches_every_single_character_typo():
    k = K.generate_key()
    text = K.encode_recovery_key(k)
    chars = [i for i, c in enumerate(text) if c != "-"]
    for i in chars:
        wrong = "A" if text[i] != "A" else "B"
        with pytest.raises(ValueError):
            K.decode_recovery_key(text[:i] + wrong + text[i + 1:])


@pytest.mark.parametrize("bad", ["", "ABC", "not a key!", "1111-1111", "A" * 100, "AAAA-" * 12])
def test_recovery_key_rejects_garbage(bad):
    with pytest.raises(ValueError):
        K.decode_recovery_key(bad)


def test_recovery_key_requires_32_bytes():
    with pytest.raises(ValueError):
        K.encode_recovery_key(b"short")


# ------------------------------------------------------------------ key file backend
@pytest.fixture
def kf(tmp_path):
    return KeyFileBackend(tmp_path / "cfg")


def test_keyfile_store_load_roundtrip_with_private_permissions(kf):
    key = K.generate_key()
    kf.store(SID, key)
    assert kf.load(SID) == key
    path = kf._path(SID)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(kf.dir.stat().st_mode) == 0o700


def test_keyfile_missing_returns_none_and_other_store_ids_are_isolated(kf):
    kf.store(SID, K.generate_key())
    assert kf.load(bytes(16)) is None


def test_keyfile_is_not_stored_as_raw_key_bytes_in_the_data_directory(kf, tmp_path, monkeypatch):
    """The key lives under the *config* dir (never next to the database)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from screentime import storage
    assert not str(kf.dir).startswith(str(storage.data_dir()))


def test_keyfile_loosened_permissions_are_tightened(kf):
    kf.store(SID, K.generate_key())
    os.chmod(kf._path(SID), 0o644)
    kf.load(SID)
    assert stat.S_IMODE(kf._path(SID).stat().st_mode) == 0o600


@pytest.mark.parametrize("content", ["!!!not base64!!!", base64.b64encode(b"too short").decode(), ""])
def test_corrupt_keyfile_is_reported_not_trusted(kf, content):
    kf.dir.mkdir(parents=True)
    kf._path(SID).write_text(content)
    with pytest.raises(KeyUnavailableError) as e:
        kf.load(SID)
    assert e.value.retryable is False


def test_keyfile_write_is_atomic_no_partial_file_is_ever_visible(kf, monkeypatch):
    key = K.generate_key()
    kf.store(SID, key)
    real_replace = os.replace
    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        kf.store(SID, K.generate_key())
    monkeypatch.setattr(os, "replace", real_replace)
    assert kf.load(SID) == key                          # the old key is intact


def test_keyfile_delete_overwrites_before_unlinking(kf, tmp_path):
    """A hard link keeps the old inode reachable, so we can see what delete left behind."""
    kf.store(SID, K.generate_key())
    link = tmp_path / "survivor"
    os.link(kf._path(SID), link)
    original = link.read_bytes()
    kf.delete(SID)
    assert not kf._path(SID).exists()
    assert link.read_bytes() == b"\0" * len(original)       # content was zeroed, not just unlinked


# ------------------------------------------------------------------ fake backends
class Fake:
    def __init__(self, name, interactive=False, store=None, load_error=None, store_error=None, read_back_wrong=False):
        self.name, self.data = name, store if store is not None else {}
        self.load_error, self.store_error, self.read_back_wrong = load_error, store_error, read_back_wrong
        self.deleted = []

    def load(self, sid):
        if self.load_error:
            raise self.load_error
        return self.data.get(sid)

    def store(self, sid, key):
        if self.store_error:
            raise self.store_error
        self.data[sid] = (bytes(32) if self.read_back_wrong else key)

    def delete(self, sid):
        self.deleted.append(sid)
        self.data.pop(sid, None)


def manager(tmp_path, *backends):
    return KeyManager(tmp_path / "cfg", backends=[(lambda interactive, b=b: b) for b in backends])


def test_create_key_prefers_the_keyring(tmp_path):
    ring, file_ = Fake("secret-service"), Fake("keyfile")
    key, backend = manager(tmp_path, ring, file_).create_key(SID)
    assert backend == "secret-service" and ring.data[SID] == key and SID not in file_.data


def test_create_key_falls_back_to_keyfile_when_keyring_cannot_be_used_silently(tmp_path):
    ring = Fake("secret-service", store_error=KeyUnavailableError("locked", True))
    file_ = Fake("keyfile")
    km = manager(tmp_path, ring, file_)
    key, backend = km.create_key(SID)
    assert backend == "keyfile" and file_.data[SID] == key
    assert km.current_backend() == "keyfile"


def test_create_key_never_trusts_a_write_it_cannot_read_back(tmp_path):
    ring = Fake("secret-service", read_back_wrong=True)
    file_ = Fake("keyfile")
    key, backend = manager(tmp_path, ring, file_).create_key(SID)
    assert backend == "keyfile" and file_.data[SID] == key


def test_create_key_fails_loudly_if_nothing_works(tmp_path):
    err = KeyUnavailableError("nope", False)
    with pytest.raises(KeyStoreError):
        manager(tmp_path, Fake("secret-service", store_error=err), Fake("keyfile", store_error=err)).create_key(SID)


def test_settings_record_backend_and_key_id_but_never_the_key(tmp_path):
    km = manager(tmp_path, Fake("keyfile"))
    key, _ = km.create_key(SID)
    text = km.settings_path.read_text()
    assert K.key_fingerprint(key) in text and "keyfile" in text
    assert key.hex() not in text and base64.b64encode(key).decode() not in text


def test_get_key_prefers_the_recorded_backend(tmp_path):
    ring, file_ = Fake("secret-service"), Fake("keyfile")
    km = manager(tmp_path, ring, file_)
    key, _ = km.create_key(SID)
    ring.data.clear()
    file_.data[SID] = key
    assert km.get_key(SID) == key                       # recorded backend (ring) is empty -> found in the other


def test_locked_keyring_is_retryable_not_not_found(tmp_path):
    ring = Fake("secret-service", load_error=KeyUnavailableError("locked", True))
    file_ = Fake("keyfile")
    with pytest.raises(KeyUnavailableError) as e:
        manager(tmp_path, ring, file_).get_key(SID)
    assert e.value.retryable is True


def test_key_missing_everywhere_is_not_found(tmp_path):
    with pytest.raises(KeyNotFoundError):
        manager(tmp_path, Fake("secret-service"), Fake("keyfile")).get_key(SID)


def test_no_keyring_at_all_is_not_a_retry_loop(tmp_path):
    ring = Fake("secret-service", load_error=KeyUnavailableError("no service", False))
    with pytest.raises(KeyNotFoundError):
        manager(tmp_path, ring, Fake("keyfile")).get_key(SID)


def test_found_key_wins_even_if_the_preferred_backend_is_locked(tmp_path):
    ring = Fake("secret-service", load_error=KeyUnavailableError("locked", True))
    file_ = Fake("keyfile")
    km = manager(tmp_path, ring, file_)
    km._write_settings(backend="secret-service")
    key = K.generate_key()
    file_.data[SID] = key
    assert km.get_key(SID) == key


def test_import_key_restores_into_the_best_backend(tmp_path):
    ring, file_ = Fake("secret-service"), Fake("keyfile")
    km = manager(tmp_path, ring, file_)
    key = K.generate_key()
    assert km.import_key(SID, key) == "secret-service" and ring.data[SID] == key
    assert km.read_settings()["key_id"] == K.key_fingerprint(key)


# ------------------------------------------------------------------ move to keyring
def test_move_to_keyring_deletes_the_file_only_after_the_ring_returns_the_key(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    real_file = KeyFileBackend(cfg)
    ring = Fake("secret-service")
    km = KeyManager(cfg, backends=[lambda i: ring, lambda i: real_file])
    key = K.generate_key()
    real_file.store(SID, key)
    km._write_settings(backend="keyfile")
    assert km.move_to_keyring(SID) == "secret-service"
    assert ring.data[SID] == key and real_file.load(SID) is None
    assert km.current_backend() == "secret-service"


def test_move_to_keyring_keeps_the_file_if_the_ring_does_not_return_the_key(tmp_path):
    cfg = tmp_path / "cfg"
    real_file = KeyFileBackend(cfg)
    ring = Fake("secret-service", read_back_wrong=True)
    km = KeyManager(cfg, backends=[lambda i: ring, lambda i: real_file])
    key = K.generate_key()
    real_file.store(SID, key)
    km._write_settings(backend="keyfile")
    with pytest.raises(KeyStoreError):
        km.move_to_keyring(SID)
    assert real_file.load(SID) == key                    # nothing was destroyed
    assert km.current_backend() == "keyfile"


def test_move_to_keyring_keeps_the_file_if_the_ring_is_unavailable(tmp_path):
    cfg = tmp_path / "cfg"
    real_file = KeyFileBackend(cfg)
    ring = Fake("secret-service", store_error=KeyUnavailableError("locked", True))
    km = KeyManager(cfg, backends=[lambda i: ring, lambda i: real_file])
    key = K.generate_key()
    real_file.store(SID, key)
    with pytest.raises(KeyUnavailableError):
        km.move_to_keyring(SID)
    assert real_file.load(SID) == key


def test_secure_delete_handles_missing_files(tmp_path):
    assert K.secure_delete_file(tmp_path / "nope") is True
