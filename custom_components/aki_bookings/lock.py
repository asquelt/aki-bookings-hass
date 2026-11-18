"""The lock, reached only through Home Assistant's own matter.* actions (the ones behind "Manage access").

DryRunLock has the same interface and touches nothing: it keeps its users in memory, so the
reconciler runs the same code path in dry-run.
"""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .const import LOCK_CALL_TIMEOUT_S


class LockError(Exception):
    """A lock operation failed. `duplicate` = the lock already holds this PIN on another user."""

    def __init__(self, message: str, duplicate: bool = False) -> None:
        super().__init__(message)
        self.duplicate = duplicate


def _describe(err: BaseException) -> str:
    text = str(err).strip()
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


class MatterLock:
    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self.entity_id)
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    async def _call(self, service: str, data: dict | None = None, response: bool = True) -> dict:
        try:
            async with asyncio.timeout(LOCK_CALL_TIMEOUT_S):
                result = await self.hass.services.async_call(
                    "matter", service, {"entity_id": self.entity_id, **(data or {})},
                    blocking=True, return_response=response,
                )
        except asyncio.TimeoutError as err:
            raise LockError(f"{service}: timeout after {LOCK_CALL_TIMEOUT_S} s") from err
        except Exception as err:  # noqa: BLE001 - every failure of a lock action is handled the same way
            status = str((getattr(err, "translation_placeholders", None) or {}).get("status") or "")
            duplicate = status == "duplicate" or "duplicate" in str(err).lower()
            raise LockError(f"{service}: {'duplicate' if duplicate else _describe(err)}", duplicate) from err
        if not response:
            return {}
        if isinstance(result, dict) and isinstance(result.get(self.entity_id), dict):
            return result[self.entity_id]
        raise LockError(f"{service}: unexpected response")

    async def info(self) -> dict:
        return await self._call("get_lock_info")

    async def users(self) -> tuple[list[dict], int | None]:
        data = await self._call("get_lock_users")
        return list(data.get("users") or []), data.get("max_users")

    async def add_pin(self, pin: str) -> tuple[int, int | None]:
        """Write a PIN as a new credential on a new user. Returns (credential_index, user_index)."""
        data = await self._call("set_lock_credential", {"credential_type": "pin", "credential_data": pin})
        return int(data["credential_index"]), data.get("user_index")

    async def name_user(self, user_index: int, name: str) -> None:
        await self._call("set_lock_user", {"user_index": user_index, "user_name": name}, response=False)

    async def pin_exists(self, credential_index: int) -> tuple[bool, int | None]:
        data = await self._call("get_lock_credential_status", {"credential_type": "pin", "credential_index": credential_index})
        return bool(data.get("credential_exists")), data.get("user_index")

    async def clear_user(self, user_index: int) -> None:
        """Remove a user; the lock drops their PIN with them."""
        await self._call("clear_lock_user", {"user_index": user_index}, response=False)

    async def clear_pin(self, credential_index: int) -> None:
        """Remove one PIN by its slot; used only when the lock never said which user it created."""
        await self._call("clear_lock_credential", {"credential_type": "pin", "credential_index": credential_index}, response=False)


class DryRunLock:
    """Stands in for the lock in dry-run: same answers, no Matter traffic."""

    available = True

    def __init__(self, records: dict[str, dict], max_users: int = 10) -> None:
        self._max = max_users
        self._users: dict[int, dict[str, Any]] = {}
        for rec in records.values():
            if rec.get("user_index") is not None:
                self._users[rec["user_index"]] = {
                    "user_index": rec["user_index"],
                    "user_name": f"RES-{rec.get('ref')}" if rec.get("named") else None,
                }

    async def info(self) -> dict:
        return {"max_users": self._max, "dry_run": True}

    async def users(self) -> tuple[list[dict], int | None]:
        return [dict(u) for u in self._users.values()], self._max

    async def add_pin(self, pin: str) -> tuple[int, int | None]:
        index = next((i for i in range(1, self._max + 1) if i not in self._users), None)
        if index is None:
            raise LockError("set_lock_credential: no available user slots")
        self._users[index] = {"user_index": index, "user_name": None}
        return index, index

    async def name_user(self, user_index: int, name: str) -> None:
        self._users[user_index]["user_name"] = name

    async def pin_exists(self, credential_index: int) -> tuple[bool, int | None]:
        return credential_index in self._users, credential_index

    async def clear_user(self, user_index: int) -> None:
        self._users.pop(user_index, None)

    async def clear_pin(self, credential_index: int) -> None:
        self._users.pop(credential_index, None)
