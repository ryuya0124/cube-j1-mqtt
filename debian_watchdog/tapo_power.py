#!/usr/bin/env python3
"""Safely power-cycle one explicitly allowlisted Tapo P110M."""

import asyncio
import argparse
import json
from pathlib import Path


ARM_PHRASE = "ALLOW_CUBE_J1_POWER_CYCLE"


class TapoSafetyError(RuntimeError):
    """The configured device did not satisfy the power-off safety checks."""


class TapoRestoreError(RuntimeError):
    """The relay may have opened and repeated ON attempts could not confirm recovery."""

    relay_was_touched = True


def _normal_mac(value):
    return "".join(ch for ch in str(value).lower() if ch in "0123456789abcdef")


def _read_identity(device):
    sys_info = getattr(device, "sys_info", {}) or {}
    device_id = getattr(device, "device_id", None)
    if not device_id:
        device_id = sys_info.get("device_id") or sys_info.get("deviceId")
    return {
        "model": str(getattr(device, "model", "") or ""),
        "mac": str(getattr(device, "mac", "") or ""),
        "device_id": str(device_id or ""),
        "alias": str(getattr(device, "alias", "") or ""),
        "is_on": bool(getattr(device, "is_on", False)),
    }


class TapoPowerController:
    def __init__(self, config, connector=None, sleeper=asyncio.sleep):
        self.config = dict(config or {})
        self._connector = connector
        self._sleeper = sleeper

    @classmethod
    def from_path(cls, path):
        path = Path(path)
        return cls(json.loads(path.read_text()))

    @property
    def armed(self):
        required = ("host", "username", "password", "expected_mac",
                    "expected_device_id")
        forbidden = [_normal_mac(value)
                     for value in self.config.get("forbidden_macs", [])]
        expected_mac = _normal_mac(self.config.get("expected_mac", ""))
        return (self.config.get("enabled") is True
                and self.config.get("allow_power_off") == ARM_PHRASE
                and all(str(self.config.get(key, "")).strip() for key in required)
                and any(forbidden)
                and expected_mac not in forbidden)

    async def _connect(self):
        if self._connector is not None:
            return await self._connector(self.config)
        from kasa import Credentials, Discover
        credentials = Credentials(
            str(self.config["username"]), str(self.config["password"]))
        device = await Discover.discover_single(
            str(self.config["host"]), credentials=credentials,
            discovery_timeout=5, timeout=5)
        if device is None:
            raise RuntimeError("P110M did not answer at the configured host")
        return device

    async def probe(self):
        """Read identity and state. This method never changes relay state."""
        device = await self._connect()
        await device.update()
        return _read_identity(device)

    def _validate_identity(self, identity):
        if not self.armed:
            raise TapoSafetyError("smart-plug power-off is not armed")
        actual_model = identity["model"].split("(", 1)[0].strip().upper()
        expected_model = str(self.config.get("expected_model", "P110M")).upper()
        if actual_model != expected_model:
            raise TapoSafetyError("unexpected smart-plug model")
        if _normal_mac(identity["mac"]) != _normal_mac(self.config["expected_mac"]):
            raise TapoSafetyError("smart-plug MAC does not match the Cube allowlist")
        forbidden_macs = {_normal_mac(value)
                          for value in self.config.get("forbidden_macs", [])}
        if _normal_mac(identity["mac"]) in forbidden_macs:
            raise TapoSafetyError("smart-plug MAC belongs to an existing protected plug")
        if identity["device_id"] != str(self.config["expected_device_id"]):
            raise TapoSafetyError("smart-plug device ID does not match the Cube allowlist")
        forbidden_ids = {str(value) for value in
                         self.config.get("forbidden_device_ids", []) if str(value)}
        if identity["device_id"] in forbidden_ids:
            raise TapoSafetyError("smart-plug device ID belongs to an existing protected plug")
        expected_alias = str(self.config.get("expected_alias", "")).strip()
        if expected_alias and identity["alias"] != expected_alias:
            raise TapoSafetyError("smart-plug alias does not match the Cube allowlist")

    async def power_cycle(self):
        """Validate the exact plug, cycle it, and make repeated efforts to restore ON."""
        if not self.armed:
            raise TapoSafetyError("smart-plug power-off is not armed")
        device = await self._connect()
        await device.update()
        identity = _read_identity(device)
        self._validate_identity(identity)

        if not identity["is_on"]:
            await device.turn_on()
            await device.update()
            if not bool(getattr(device, "is_on", False)):
                raise RuntimeError("allowlisted Cube plug was OFF and could not be restored")
            return {**identity, "result": "restored_already_off_plug", "off_seconds": 0}

        off_seconds = min(max(int(self.config.get("off_seconds", 15)), 5), 60)
        on_attempts = min(max(int(self.config.get("on_attempts", 6)), 3), 12)
        on_retry_seconds = min(max(int(self.config.get("on_retry_seconds", 5)), 2), 30)
        off_error = None
        try:
            await device.turn_off()
        except Exception as exc:  # A lost reply can still mean that the relay opened.
            off_error = type(exc).__name__
        await self._sleeper(off_seconds)

        last_error = None
        for attempt in range(1, on_attempts + 1):
            try:
                await device.turn_on()
                await self._sleeper(1)
                await device.update()
                if bool(getattr(device, "is_on", False)):
                    return {
                        **identity,
                        "result": "power_cycled",
                        "off_seconds": off_seconds,
                        "on_attempts": attempt,
                        "off_reply_error": off_error,
                    }
                last_error = "relay state remained OFF"
            except Exception as exc:
                last_error = "{}: {}".format(type(exc).__name__, str(exc)[:120])
            if attempt < on_attempts:
                await self._sleeper(on_retry_seconds)
        raise TapoRestoreError(
            "Cube plug ON could not be confirmed after {} attempts: {}"
            .format(on_attempts, last_error))


def main():
    parser = argparse.ArgumentParser(
        description="Read a configured Tapo plug identity without changing relay state")
    parser.add_argument("command", choices=("probe",))
    parser.add_argument("config", nargs="?", default="/run/secrets/tapo.json")
    args = parser.parse_args()
    controller = TapoPowerController.from_path(args.config)
    identity = asyncio.run(controller.probe())
    print(json.dumps(identity, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
