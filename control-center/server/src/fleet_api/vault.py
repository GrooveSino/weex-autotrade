from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class CredentialMaterial:
    api_key: SecretStr
    api_secret: SecretStr
    passphrase: SecretStr
    proxy_url: SecretStr

    def __repr__(self) -> str:
        return "CredentialMaterial(**********)"


class CredentialVaultError(RuntimeError):
    pass


class CredentialVault(Protocol):
    def put(self, instance_id: str, material: CredentialMaterial) -> None: ...

    def get(self, instance_id: str) -> CredentialMaterial | None: ...

    def remove(self, instance_id: str) -> None: ...

    def __len__(self) -> int: ...

    def close(self) -> None: ...


class EphemeralCredentialVault:
    """Process-memory-only placeholder; production must replace this with an encrypted vault."""

    def __init__(self) -> None:
        self._values: dict[str, CredentialMaterial] = {}
        self._lock = RLock()

    def put(self, instance_id: str, material: CredentialMaterial) -> None:
        with self._lock:
            self._values[instance_id] = material

    def get(self, instance_id: str) -> CredentialMaterial | None:
        with self._lock:
            return self._values.get(instance_id)

    def remove(self, instance_id: str) -> None:
        with self._lock:
            self._values.pop(instance_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def close(self) -> None:
        return None


class EncryptedSQLiteCredentialVault:
    def __init__(self, path: Path, master_key: SecretStr) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._cipher = Fernet(master_key.get_secret_value().encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise CredentialVaultError("FLEET_MASTER_KEY must be a valid Fernet key") from exc
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS encrypted_credentials (
                instance_id TEXT PRIMARY KEY,
                ciphertext BLOB NOT NULL,
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            )
            """
        )
        self._connection.commit()
        self._lock = RLock()

    def _encrypt(self, material: CredentialMaterial) -> bytes:
        plaintext = json.dumps(
            {
                "api_key": material.api_key.get_secret_value(),
                "api_secret": material.api_secret.get_secret_value(),
                "passphrase": material.passphrase.get_secret_value(),
                "proxy_url": material.proxy_url.get_secret_value(),
            },
            separators=(",", ":"),
        ).encode()
        return self._cipher.encrypt(plaintext)

    def _decrypt(self, ciphertext: bytes) -> CredentialMaterial:
        try:
            payload = json.loads(self._cipher.decrypt(ciphertext))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialVaultError("stored credentials cannot be decrypted with FLEET_MASTER_KEY") from exc
        return CredentialMaterial(
            api_key=SecretStr(payload["api_key"]),
            api_secret=SecretStr(payload["api_secret"]),
            passphrase=SecretStr(payload["passphrase"]),
            proxy_url=SecretStr(payload["proxy_url"]),
        )

    def put(self, instance_id: str, material: CredentialMaterial) -> None:
        ciphertext = self._encrypt(material)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO encrypted_credentials(instance_id, ciphertext)
                VALUES (?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET ciphertext = excluded.ciphertext
                """,
                (instance_id, ciphertext),
            )

    def get(self, instance_id: str) -> CredentialMaterial | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT ciphertext FROM encrypted_credentials WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
        return self._decrypt(row[0]) if row else None

    def remove(self, instance_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM encrypted_credentials WHERE instance_id = ?",
                (instance_id,),
            )

    def __len__(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) FROM encrypted_credentials").fetchone()
        return int(row[0])

    def verify_all(self) -> None:
        with self._lock:
            rows = self._connection.execute("SELECT ciphertext FROM encrypted_credentials").fetchall()
        for row in rows:
            self._decrypt(row[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()
