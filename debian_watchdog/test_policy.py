import unittest

from policy import Policy


class PolicyTest(unittest.TestCase):
    def test_stages_and_recovery(self):
        p = Policy(stale_after=100, bridge_grace=40, reboot_grace=60)
        self.assertEqual(p.decide(99, broker_since=0, started_at=0)[0], None)
        self.assertEqual(p.decide(100, broker_since=0, started_at=0)[0], "bridge")
        p.start_action("bridge", 100)
        self.assertEqual(p.decide(139, broker_since=0, started_at=0)[0], None)
        self.assertEqual(p.decide(140, broker_since=0, started_at=0)[0], "reboot")
        p.start_action("reboot", 140)
        self.assertEqual(p.decide(200, broker_since=0, started_at=0)[1],
                         "reboot grace expired; cooling down")
        self.assertTrue(p.observe(201))
        self.assertEqual(p.phase, "normal")
        self.assertEqual(p.decide(250, broker_since=0, started_at=0)[0], None)

    def test_broker_outage_does_not_trigger_action(self):
        p = Policy(stale_after=100, bridge_grace=40)
        self.assertEqual(p.decide(1000, broker_since=None, started_at=0)[0], None)
        self.assertEqual(p.decide(1099, broker_since=1000, started_at=0)[0], None)
        self.assertEqual(p.decide(1100, broker_since=1000, started_at=0)[0], "bridge")
        p.start_action("bridge", 1100)
        self.assertEqual(p.decide(2000, broker_since=1990, started_at=0)[0], None)

    def test_daily_limit_survives_reload(self):
        p = Policy(stale_after=1, max_bridge_per_day=1)
        p.start_action("bridge", 2)
        p.action_succeeded("bridge", 2)
        p.observe(3)
        p = Policy.from_saved({"stale_after": 1, "max_bridge_per_day": 1}, p.saved())
        self.assertIsNone(p.decide(10, broker_since=0, started_at=0)[0])
        self.assertEqual(p.phase, "cooldown")
        self.assertGreaterEqual(p.cooldown_until, 2 + 24 * 3600)

    def test_responsive_radio_scan_defers_restarts(self):
        p = Policy(stale_after=100, cooldown=3600)
        self.assertEqual(p.decide(100, broker_since=0, started_at=0)[0], "bridge")
        p.defer_for_radio_search(100)
        self.assertEqual(p.decide(500, broker_since=0, started_at=0)[0], None)
        self.assertEqual(p.decide(1000, broker_since=0, started_at=0)[0], "bridge")
        self.assertEqual(p.actions, [])
        p.observe(1001)
        self.assertEqual(p.phase, "normal")

    def test_failed_action_does_not_use_daily_allowance(self):
        p = Policy(stale_after=1, max_bridge_per_day=1)
        p.start_action("bridge", 2)
        p.action_failed("bridge", 3)
        self.assertEqual(p.actions, [])
        self.assertEqual(p.decide(904, broker_since=0, started_at=0)[0], "bridge")

    def test_successful_action_uses_daily_allowance(self):
        p = Policy(stale_after=1, max_bridge_per_day=1)
        p.start_action("bridge", 2)
        p.action_succeeded("bridge", 2)
        p.observe(3)
        self.assertIsNone(p.decide(4, broker_since=0, started_at=0)[0])


if __name__ == "__main__":
    unittest.main()
