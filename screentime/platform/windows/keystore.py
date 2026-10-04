"""Windows key backends for keystore.KeyManager.

  credential-manager  The 32-byte database key as a generic credential in the
                      current user's Windows Credential Manager vault
                      (CredWriteW/CredReadW). Persisted for this machine only
                      (CRED_PERSIST_LOCAL_MACHINE: never roamed). The vault is
                      encrypted with a key tied to the user's logon.
  dpapi-file          Fallback if Credential Manager cannot be used: the key
                      wrapped with DPAPI (CryptProtectData, current-user scope)
                      in %APPDATA%\\ScreenTime\\keys\\<store id>.dpapi.

HONEST SECURITY MODEL: both are bound to this Windows user account. Code that
runs as the same user (including malware running as you) can ask Windows for
the key, and neither is a hardware/TPM-sealed secret. That is a weaker promise
than "locked until you type a password", so the README says so; the recovery
key remains the only way to move the data to another machine or account. These
are NOT equivalent to Secret Service / KWallet and the UI does not claim that.

Neither backend ever logs key material.
"""
from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Optional

from ...keystore import KEY_BYTES, KeyUnavailableError, secure_delete_file
from .win32 import ERROR_ACCESS_DENIED, Win32Api, Win32Error, default_api

log = logging.getLogger("screentime.keystore.windows")

CRED_TARGET_PREFIX = "ScreenTime/database-key/"


def _decode(raw: bytes, source: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise KeyUnavailableError(f"{source} entry is not a ScreenTime key", retryable=False) from None
    if len(key) != KEY_BYTES:
        raise KeyUnavailableError(f"{source} entry has the wrong length", retryable=False)
    return key


class CredentialManagerBackend:
    name = "credential-manager"

    def __init__(self, api: Optional[Win32Api] = None, interactive: bool = False):
        self._api = api
        self.interactive = interactive          # accepted for interface parity; never prompts

    @property
    def api(self) -> Win32Api:
        if self._api is None:
            self._api = default_api()
        return self._api

    @staticmethod
    def _target(store_id: bytes) -> str:
        return CRED_TARGET_PREFIX + store_id.hex()

    def load(self, store_id: bytes) -> Optional[bytes]:
        try:
            raw = self.api.cred_read(self._target(store_id))
        except Win32Error as e:
            # Vault service not up yet / transient: worth waiting for.
            raise KeyUnavailableError(f"Windows Credential Manager unavailable (error {e.code})",
                                      retryable=e.code != ERROR_ACCESS_DENIED) from e
        return None if raw is None else _decode(raw, "Credential Manager")

    def store(self, store_id: bytes, key: bytes):
        try:
            self.api.cred_write(self._target(store_id), "screentime", base64.b64encode(key),
                                "ScreenTime database key")
        except Win32Error as e:
            raise KeyUnavailableError(f"cannot write to Windows Credential Manager (error {e.code})",
                                      retryable=False) from e

    def delete(self, store_id: bytes):
        try:
            self.api.cred_delete(self._target(store_id))
        except Win32Error as e:
            raise KeyUnavailableError(f"cannot delete the credential (error {e.code})", retryable=False) from e


class DpapiFileBackend:
    name = "dpapi-file"

    def __init__(self, config_dir: Path, api: Optional[Win32Api] = None):
        self.dir = Path(config_dir) / "keys"
        self._api = api

    @property
    def api(self) -> Win32Api:
        if self._api is None:
            self._api = default_api()
        return self._api

    def _path(self, store_id: bytes) -> Path:
        return self.dir / f"{store_id.hex()}.dpapi"

    def load(self, store_id: bytes) -> Optional[bytes]:
        p = self._path(store_id)
        try:
            blob = p.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise KeyUnavailableError(f"cannot read key file: {e}", retryable=False) from e
        try:
            return _decode(self.api.dpapi_unprotect(blob), "DPAPI key file")
        except Win32Error as e:
            # Wrong user / profile reset: waiting will not help.
            raise KeyUnavailableError(f"the key file cannot be decrypted by this Windows account "
                                      f"(error {e.code})", retryable=False) from e

    def store(self, store_id: bytes, key: bytes):
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            blob = self.api.dpapi_protect(base64.b64encode(key))
        except Win32Error as e:
            raise KeyUnavailableError(f"DPAPI failed (error {e.code})", retryable=False) from e
        tmp = self._path(store_id).with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            import os
            os.fsync(f.fileno())
        tmp.replace(self._path(store_id))                # atomic: never a half-written key

    def delete(self, store_id: bytes):
        secure_delete_file(self._path(store_id))


def backend_factories(config_dir: Path, api: Optional[Win32Api] = None):
    return [
        lambda interactive: CredentialManagerBackend(api, interactive),
        lambda interactive: DpapiFileBackend(config_dir, api),
    ]
