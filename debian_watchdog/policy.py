"""Small, persistent recovery state machine for Cube J1."""

from dataclasses import dataclass, field


DAY = 24 * 60 * 60


@dataclass
class Policy:
    stale_after: int = 600
    bridge_grace: int = 420
    reboot_grace: int = 600
    cooldown: int = 3600
    max_bridge_per_day: int = 4
    max_reboot_per_day: int = 2
    phase: str = "normal"
    phase_at: float = 0
    cooldown_until: float = 0
    last_message_at: float = 0
    actions: list = field(default_factory=list)
    radio_deferrals: int = 0

    @classmethod
    def from_saved(cls, config, saved):
        obj = cls(**config)
        obj.phase = saved.get("phase", "normal")
        if obj.phase not in ("normal", "bridge_wait", "reboot_wait", "cooldown"):
            obj.phase = "normal"
        obj.phase_at = float(saved.get("phase_at", 0))
        obj.cooldown_until = float(saved.get("cooldown_until", 0))
        obj.last_message_at = float(saved.get("last_message_at", 0))
        obj.actions = [a for a in saved.get("actions", [])
                       if a.get("type") in ("bridge", "reboot") and isinstance(a.get("at"), (int, float))]
        obj.radio_deferrals = int(saved.get("radio_deferrals", 0))
        return obj

    def saved(self):
        return {"phase": self.phase, "phase_at": self.phase_at,
                "cooldown_until": self.cooldown_until,
                "last_message_at": self.last_message_at, "actions": self.actions,
                "radio_deferrals": self.radio_deferrals}

    def observe(self, now):
        previous = self.phase
        self.last_message_at = now
        self.phase = "normal"
        self.phase_at = now
        self.cooldown_until = 0
        self.radio_deferrals = 0
        return previous != "normal"

    def decide(self, now, broker_since, started_at):
        """Return (action, reason). Never act unless MQTT is currently connected."""
        if broker_since is None:
            return None, None
        self.actions = [a for a in self.actions if now - a["at"] < DAY]
        if self.phase == "cooldown":
            if now < self.cooldown_until:
                return None, None
            self.phase = "normal"
        if self.phase == "normal":
            anchor = max(self.last_message_at, broker_since, started_at)
            if now - anchor < self.stale_after:
                return None, None
            action, reason = "bridge", "no live power message for {}s".format(int(now - anchor))
        elif self.phase == "bridge_wait":
            if now - max(self.phase_at, broker_since) < self.bridge_grace:
                return None, None
            action, reason = "reboot", "no live power message after bridge restart"
        elif self.phase == "reboot_wait":
            if now - max(self.phase_at, broker_since) >= self.reboot_grace:
                self.phase = "cooldown"
                self.cooldown_until = now + self.cooldown
                return None, "reboot grace expired; cooling down"
            return None, None
        else:
            return None, None

        cap = self.max_bridge_per_day if action == "bridge" else self.max_reboot_per_day
        recent = [a["at"] for a in self.actions if a["type"] == action]
        if len(recent) >= cap:
            self.phase = "cooldown"
            self.cooldown_until = max(now + self.cooldown, min(recent) + DAY)
            return None, "daily {} limit reached; cooling down".format(action)
        return action, reason

    def start_action(self, action, now):
        self.phase = "bridge_wait" if action == "bridge" else "reboot_wait"
        self.phase_at = now

    def action_succeeded(self, action, now):
        """Only commands that reached Cube count against the daily limit."""
        self.actions.append({"type": action, "at": now})

    def action_failed(self, action, now):
        expected = "bridge_wait" if action == "bridge" else "reboot_wait"
        if self.phase == expected:
            self.phase = "cooldown"
            self.cooldown_until = now + min(self.cooldown, 900)

    def defer_for_radio_search(self, now):
        """Let a responsive Wi-SUN scan continue without restarting Cube."""
        self.radio_deferrals += 1
        self.phase = "cooldown"
        self.phase_at = now
        self.cooldown_until = now + min(self.cooldown, 900)

    def defer_for_cube_recovery(self, now):
        """Give Cube's own supervisor one short window before intervening."""
        self.phase = "cooldown"
        self.phase_at = now
        self.cooldown_until = now + min(self.cooldown, 900)
