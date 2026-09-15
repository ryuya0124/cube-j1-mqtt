import unittest

from tapo_power import ARM_PHRASE, TapoPowerController, TapoSafetyError


class FakePlug:
    def __init__(self, *, mac="AA:BB:CC:DD:EE:FF", device_id="new-cube-plug",
                 model="P110M(JP)", alias="Cube J1 power", is_on=True,
                 fail_on_attempts=0):
        self.mac = mac
        self.device_id = device_id
        self.model = model
        self.alias = alias
        self.is_on = is_on
        self.fail_on_attempts = fail_on_attempts
        self.off_calls = 0
        self.on_calls = 0

    async def update(self):
        return None

    async def turn_off(self):
        self.off_calls += 1
        self.is_on = False

    async def turn_on(self):
        self.on_calls += 1
        if self.on_calls <= self.fail_on_attempts:
            raise RuntimeError("temporary failure")
        self.is_on = True


def config(**changes):
    value = {
        "enabled": True,
        "allow_power_off": ARM_PHRASE,
        "host": "192.0.2.10",
        "username": "user@example.com",
        "password": "secret",
        "expected_model": "P110M",
        "expected_mac": "AA-BB-CC-DD-EE-FF",
        "expected_device_id": "new-cube-plug",
        "expected_alias": "Cube J1 power",
        "forbidden_macs": ["11:22:33:44:55:66"],
        "forbidden_device_ids": ["existing-p110m"],
        "off_seconds": 5,
        "on_attempts": 4,
        "on_retry_seconds": 2,
    }
    value.update(changes)
    return value


async def no_sleep(_seconds):
    return None


class TapoPowerSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def make_controller(self, plug, **changes):
        async def connector(_config):
            return plug
        return TapoPowerController(config(**changes), connector=connector,
                                   sleeper=no_sleep)

    async def test_disabled_configuration_never_connects(self):
        connected = False

        async def connector(_config):
            nonlocal connected
            connected = True
            return FakePlug()

        controller = TapoPowerController(config(enabled=False), connector=connector,
                                         sleeper=no_sleep)
        with self.assertRaises(TapoSafetyError):
            await controller.power_cycle()
        self.assertFalse(connected)

    async def test_mac_mismatch_never_turns_off(self):
        plug = FakePlug(mac="00:11:22:33:44:55")
        controller = await self.make_controller(plug)
        with self.assertRaisesRegex(TapoSafetyError, "MAC"):
            await controller.power_cycle()
        self.assertEqual(plug.off_calls, 0)

    async def test_device_id_mismatch_never_turns_off(self):
        plug = FakePlug(device_id="existing-p110m")
        controller = await self.make_controller(plug)
        with self.assertRaisesRegex(TapoSafetyError, "device ID"):
            await controller.power_cycle()
        self.assertEqual(plug.off_calls, 0)

    async def test_existing_protected_mac_cannot_be_armed(self):
        plug = FakePlug(mac="11:22:33:44:55:66")
        controller = await self.make_controller(
            plug, expected_mac="11:22:33:44:55:66")
        self.assertFalse(controller.armed)
        with self.assertRaises(TapoSafetyError):
            await controller.power_cycle()
        self.assertEqual(plug.off_calls, 0)

    async def test_exact_allowlist_cycles_and_retries_on(self):
        plug = FakePlug(fail_on_attempts=2)
        controller = await self.make_controller(plug)
        result = await controller.power_cycle()
        self.assertEqual(result["result"], "power_cycled")
        self.assertEqual(plug.off_calls, 1)
        self.assertEqual(plug.on_calls, 3)
        self.assertTrue(plug.is_on)

    async def test_already_off_is_only_turned_on(self):
        plug = FakePlug(is_on=False)
        controller = await self.make_controller(plug)
        result = await controller.power_cycle()
        self.assertEqual(result["result"], "restored_already_off_plug")
        self.assertEqual(plug.off_calls, 0)
        self.assertEqual(plug.on_calls, 1)


if __name__ == "__main__":
    unittest.main()
