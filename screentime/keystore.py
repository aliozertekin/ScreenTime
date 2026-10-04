"""Where the database key lives, and how it is never lost.

The key is 32 random bytes (from `secrets`). It is never derived from anything
predictable and never hard-coded. It is stored, in order of preference:

1. **The system keyring** via the freedesktop Secret Service D-Bus API. On KDE
   Plasma that is KWallet (which implements the Secret Service API); on GNOME
   it is GNOME Keyring. Nothing on disk besides the keyring's own encrypted
   store, which is tied to your login password.
2. **A key file** (`$XDG_CONFIG_HOME/screentime/keys/<store id>.key`, mode 0600)
   when no keyring can be used silently. It is deliberately *not* in the data
   directory, so backups/sync/SQLite-browser access to the database alone
   don't carry the key -- but it is weaker: anyone who can read your whole
   home directory gets both. Settings shows this state and offers to move the
   key into the keyring.

The daemon never shows a keyring prompt (it would pop a dialog at login); it
waits and retries. Interactive unlocking happens from the GUI / CLI.

Losing the key means losing the data, so a **recovery key** (a checksummed,
human-copyable encoding of the same 32 bytes) can be exported at any time.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("screentime.keystore")

KEY_BYTES = 32
SECRET_ATTRS_BASE = {"application": "screentime", "purpose": "database-key"}


# ----------------------------------------------------------------- exceptions
class KeyStoreError(Exception):
    pass


class KeyUnavailableError(KeyStoreError):
    """The key may exist but cannot be read right now.
    `retryable` = worth waiting (keyring locked / not started yet)."""
    def __init__(self, reason: str, retryable: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class KeyNotFoundError(KeyStoreError):
    """No backend has a key for this store (lost, or never created on this machine)."""


# --------------------------------------------------------------------- helpers
def generate_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def key_fingerprint(key: bytes) -> str:
    """Short deterministic *identifier* for a key (SHA-256 is fine here: it is an
    id, not a cipher -- and the key is 256 random bits, so it reveals nothing)."""
    return hashlib.sha256(b"screentime/key-id/v1" + key).hexdigest()[:16]


def encode_recovery_key(key: bytes) -> str:
    """32 key bytes + 4 checksum bytes -> Base32 in groups of 4: catches typos."""
    if len(key) != KEY_BYTES:
        raise ValueError("key must be 32 bytes")
    body = key + hashlib.sha256(key).digest()[:4]
    text = base64.b32encode(body).decode().rstrip("=")
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def decode_recovery_key(text: str) -> bytes:
    cleaned = "".join(ch for ch in text.upper() if ch.isalnum())
    cleaned += "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(cleaned)
    except (binascii.Error, ValueError):
        raise ValueError("not a valid recovery key (only letters A-Z and digits 2-7 are used)") from None
    if len(raw) != KEY_BYTES + 4:
        raise ValueError("recovery key has the wrong length")
    key, check = raw[:KEY_BYTES], raw[KEY_BYTES:]
    # The last Base32 character carries two unused bits; a typo that only changes
    # those decodes to the same bytes. Insist on the canonical spelling so EVERY
    # single-character typo is caught (this made the typo test flaky ~1 run in 30).
    if base64.b32encode(raw).decode().rstrip("=") != cleaned.rstrip("="):
        raise ValueError("recovery key is not spelled canonically -- check for typos")
    if hashlib.sha256(key).digest()[:4] != check:
        raise ValueError("recovery key checksum does not match -- check for typos")
    return key


def default_config_dir() -> Path:
    from . import platform as _platform
    return _platform.paths().config_dir()


def secure_delete_file(path: Path) -> bool:
    """Best effort: overwrite with zeros, fsync, unlink. On SSDs, copy-on-write
    filesystems (btrfs, ZFS), journaling or snapshotted volumes this can NOT
    guarantee the old bytes are gone -- see the README limitations."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "r+b", buffering=0) as f:
            remaining = size
            while remaining > 0:
                n = min(remaining, 1 << 20)
                f.write(b"\0" * n)
                remaining -= n
            f.flush()
            os.fsync(f.fileno())
    except FileNotFoundError:
        return True
    except OSError as e:
        log.debug("overwrite of %s failed: %s", path, e)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log.warning("could not remove %s: %s", path, e)
        return False


# ------------------------------------------------------------ key file backend
class KeyFileBackend:
    name = "keyfile"

    def __init__(self, config_dir: Path):
        self.dir = Path(config_dir) / "keys"

    def _path(self, store_id: bytes) -> Path:
        return self.dir / f"{store_id.hex()}.key"

    def load(self, store_id: bytes) -> Optional[bytes]:
        p = self._path(store_id)
        try:
            st = p.stat()
            raw = p.read_text().strip()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise KeyUnavailableError(f"cannot read key file: {e}", retryable=False) from e
        if st.st_mode & 0o077:
            log.warning("key file %s was accessible to others; tightening permissions", p)
            os.chmod(p, 0o600)
        try:
            key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise KeyUnavailableError("key file is corrupt", retryable=False) from None
        if len(key) != KEY_BYTES:
            raise KeyUnavailableError("key file has the wrong length", retryable=False)
        return key

    def store(self, store_id: bytes, key: bytes):
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        tmp = self._path(store_id).with_suffix(".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, base64.b64encode(key) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path(store_id))        # atomic: never a half-written key

    def delete(self, store_id: bytes):
        secure_delete_file(self._path(store_id))


# -------------------------------------------------------- Secret Service backend
_SS = "org.freedesktop.secrets"
_SS_PATH = "/org/freedesktop/secrets"
_COLLECTION = "/org/freedesktop/secrets/aliases/default"


class SecretServiceBackend:
    """Minimal client for org.freedesktop.Secret.Service (KWallet, GNOME Keyring,
    KeePassXC, ...), talking D-Bus directly through Gio: no extra dependency.
    Uses the "plain" transfer algorithm, which is fine on the per-user session
    bus (the same thing `secret-tool` does).

    interactive=False (the daemon) never waits on a prompt: a locked keyring is
    reported as KeyUnavailableError(retryable=True) instead."""
    name = "secret-service"

    def __init__(self, interactive: bool = False, timeout_ms: int = 8000, prompt_timeout_s: float = 180.0,
                 bus_address: Optional[str] = None):
        self.interactive = interactive
        self.timeout_ms = timeout_ms
        self.prompt_timeout_s = prompt_timeout_s
        self._bus_address = bus_address
        self._bus = None
        self._session = None

    # ---- plumbing
    def _conn(self):
        if self._bus is None:
            from gi.repository import Gio
            try:
                if self._bus_address:
                    self._bus = Gio.DBusConnection.new_for_address_sync(
                        self._bus_address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                        Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
                else:
                    self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except Exception as e:
                raise KeyUnavailableError(f"no D-Bus session bus: {e}", retryable=False) from e
        return self._bus

    def _call(self, path, iface, method, params, reply_type, dest=_SS):
        from gi.repository import Gio, GLib
        try:
            return self._conn().call_sync(dest, path, iface, method, params, GLib.VariantType(reply_type),
                                          Gio.DBusCallFlags.NONE, self.timeout_ms, None)
        except GLib.Error as e:
            msg = e.message or str(e)
            if any(s in msg for s in ("ServiceUnknown", "NameHasNoOwner", "not provided", "No such interface")):
                raise KeyUnavailableError("no Secret Service (keyring) is running", retryable=False) from None
            raise KeyUnavailableError(f"Secret Service error: {msg}", retryable=True) from None

    def _get_session(self) -> str:
        from gi.repository import GLib
        if self._session is None:
            out = self._call(_SS_PATH, "org.freedesktop.Secret.Service", "OpenSession",
                             GLib.Variant("(sv)", ("plain", GLib.Variant("s", ""))), "(vo)")
            self._session = out.unpack()[1]
        return self._session

    def _run_prompt(self, prompt_path: str):
        """Show the keyring's own dialog and wait for it (interactive only)."""
        from gi.repository import Gio, GLib
        if not self.interactive:
            raise KeyUnavailableError("the keyring is locked (unlock it, or open ScreenTime)", retryable=True)
        result = {}

        def on_completed(_c, _s, _p, _i, _sig, params):
            dismissed, _val = params.unpack()
            result["dismissed"] = dismissed

        sub = self._conn().signal_subscribe(_SS, "org.freedesktop.Secret.Prompt", "Completed", prompt_path,
                                            None, Gio.DBusSignalFlags.NONE, on_completed)
        try:
            self._call(prompt_path, "org.freedesktop.Secret.Prompt", "Prompt", GLib.Variant("(s)", ("",)), "()")
            ctx, deadline = GLib.MainContext.default(), time.monotonic() + self.prompt_timeout_s
            while "dismissed" not in result and time.monotonic() < deadline:
                ctx.iteration(True if not ctx.pending() else False)
                time.sleep(0.01)
        finally:
            self._conn().signal_unsubscribe(sub)
        if "dismissed" not in result:
            raise KeyUnavailableError("timed out waiting for the keyring prompt", retryable=True)
        if result["dismissed"]:
            raise KeyUnavailableError("the keyring prompt was dismissed", retryable=True)

    def _unlock(self, paths: list):
        from gi.repository import GLib
        out = self._call(_SS_PATH, "org.freedesktop.Secret.Service", "Unlock", GLib.Variant("(ao)", (paths,)), "(aoo)")
        _unlocked, prompt = out.unpack()
        if prompt != "/":
            self._run_prompt(prompt)

    @staticmethod
    def _attrs(store_id: bytes) -> dict:
        return {**SECRET_ATTRS_BASE, "store": store_id.hex()}

    # ---- backend API
    def load(self, store_id: bytes) -> Optional[bytes]:
        from gi.repository import GLib
        out = self._call(_SS_PATH, "org.freedesktop.Secret.Service", "SearchItems",
                         GLib.Variant("(a{ss})", (self._attrs(store_id),)), "(aoao)")
        unlocked, locked = out.unpack()
        if not unlocked and not locked:
            return None
        item = (unlocked or locked)[0]
        if not unlocked:
            self._unlock([item])
        sec = self._call(item, "org.freedesktop.Secret.Item", "GetSecret",
                         GLib.Variant("(o)", (self._get_session(),)), "((oayays))").unpack()[0]
        try:
            key = base64.b64decode(bytes(sec[2]), validate=True)
        except (binascii.Error, ValueError):
            raise KeyUnavailableError("the keyring entry is not a ScreenTime key", retryable=False) from None
        if len(key) != KEY_BYTES:
            raise KeyUnavailableError("the keyring entry has the wrong length", retryable=False)
        return key

    def store(self, store_id: bytes, key: bytes):
        from gi.repository import GLib
        locked = self._call(_COLLECTION, "org.freedesktop.DBus.Properties", "Get",
                            GLib.Variant("(ss)", ("org.freedesktop.Secret.Collection", "Locked")), "(v)"
                            ).unpack()[0]
        if locked:
            self._unlock([_COLLECTION])
        props = {
            "org.freedesktop.Secret.Item.Label": GLib.Variant("s", "ScreenTime database key"),
            "org.freedesktop.Secret.Item.Attributes": GLib.Variant("a{ss}", self._attrs(store_id)),
        }
        secret = (self._get_session(), b"", base64.b64encode(key), "text/plain")
        out = self._call(_COLLECTION, "org.freedesktop.Secret.Collection", "CreateItem",
                         GLib.Variant("(a{sv}(oayays)b)", (props, secret, True)), "(oo)")
        _item, prompt = out.unpack()
        if prompt != "/":
            self._run_prompt(prompt)

    def delete(self, store_id: bytes):
        from gi.repository import GLib
        out = self._call(_SS_PATH, "org.freedesktop.Secret.Service", "SearchItems",
                         GLib.Variant("(a{ss})", (self._attrs(store_id),)), "(aoao)")
        for item in sum((list(x) for x in out.unpack()), []):
            res = self._call(item, "org.freedesktop.Secret.Item", "Delete", None, "(o)")
            prompt = res.unpack()[0]
            if prompt != "/":
                self._run_prompt(prompt)


# --------------------------------------------------------------------- manager
class KeyManager:
    """Chooses where the key lives and finds it again. `backends` (a list of
    backend factories `f(interactive) -> backend`) is injectable for tests."""

    def __init__(self, config_dir: Optional[Path] = None, backends: Optional[list] = None):
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()
        from . import platform as _platform
        if backends:
            self._factories = backends
        elif _platform.is_windows():
            from .platform.windows.keystore import backend_factories
            self._factories = backend_factories(self.config_dir)
        else:
            self._factories = [
                lambda interactive: SecretServiceBackend(interactive=interactive),
                lambda interactive: KeyFileBackend(self.config_dir),
            ]
        # The backend that counts as "the system keyring" for move_to_keyring().
        self.keyring_backend_name = "credential-manager" if (not backends and _platform.is_windows()) \
            else "secret-service"

    # ---- persisted, non-secret settings (which backend; which key)
    @property
    def settings_path(self) -> Path:
        return self.config_dir / "security.json"

    def read_settings(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            return {}

    def _write_settings(self, **fields):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = {**self.read_settings(), "version": 1, **fields}
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.settings_path)

    def _backends(self, interactive: bool) -> list:
        order = [f(interactive) for f in self._factories]
        preferred = self.read_settings().get("backend")
        order.sort(key=lambda b: 0 if b.name == preferred else 1)        # stable: preferred first
        return order

    # ---- operations
    def get_key(self, store_id: bytes, interactive: bool = False) -> bytes:
        """Find the key. Raises KeyUnavailableError(retryable) if a keyring might
        have it but is locked/starting, else KeyNotFoundError."""
        pending: Optional[KeyUnavailableError] = None
        for backend in self._backends(interactive):
            try:
                key = backend.load(store_id)
            except KeyUnavailableError as e:
                log.debug("%s: %s", backend.name, e)
                if e.retryable or pending is None:
                    pending = e if (pending is None or e.retryable) else pending
                continue
            if key is not None:
                expected = self.read_settings().get("key_id")
                if expected and expected != key_fingerprint(key):
                    log.warning("the key from %s is not the one this install recorded", backend.name)
                return key
        if pending is not None and pending.retryable:
            raise pending
        raise KeyNotFoundError("no key for this database was found (keyring entry / key file missing)")

    def create_key(self, store_id: bytes, interactive: bool = False) -> tuple:
        """Generate and store a new key. Returns (key, backend_name). Falls back
        to the key file when no keyring works *silently* -- and says so."""
        key = generate_key()
        for backend in [f(interactive) for f in self._factories]:
            try:
                backend.store(store_id, key)
                if backend.load(store_id) != key:               # never trust a write we haven't read back
                    raise KeyUnavailableError(f"{backend.name} did not return the key it was given", retryable=False)
            except KeyUnavailableError as e:
                log.info("cannot keep the key in %s (%s); trying the next option", backend.name, e)
                continue
            self._write_settings(backend=backend.name, key_id=key_fingerprint(key), store_id=store_id.hex(),
                                 created=int(time.time()))
            return key, backend.name
        raise KeyStoreError("could not store the key anywhere")

    def import_key(self, store_id: bytes, key: bytes, interactive: bool = False) -> str:
        """Put a known key (e.g. from a recovery key) back into the best backend."""
        for backend in [f(interactive) for f in self._factories]:
            try:
                backend.store(store_id, key)
                if backend.load(store_id) != key:
                    continue
            except KeyUnavailableError:
                continue
            self._write_settings(backend=backend.name, key_id=key_fingerprint(key), store_id=store_id.hex())
            return backend.name
        raise KeyStoreError("could not store the key anywhere")

    def move_to_keyring(self, store_id: bytes) -> str:
        """Interactive: copy the key file's key into the keyring, verify, then
        destroy the file. Nothing is deleted unless the keyring returns the key."""
        key = self.get_key(store_id, interactive=True)
        ss = next(b for b in (f(True) for f in self._factories) if b.name == self.keyring_backend_name)
        ss.store(store_id, key)
        if ss.load(store_id) != key:
            raise KeyStoreError("the keyring did not return the key; the key file was kept")
        self._write_settings(backend=self.keyring_backend_name, key_id=key_fingerprint(key), store_id=store_id.hex())
        for f in self._factories:                       # destroy the weaker copy (key file / DPAPI file)
            fallback = f(False)
            if fallback.name in ("keyfile", "dpapi-file"):
                fallback.delete(store_id)
        return self.keyring_backend_name

    def current_backend(self) -> Optional[str]:
        return self.read_settings().get("backend")
