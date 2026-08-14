"""Firmware operacional V2 do Raspberry Pi Pico para o Grava Nois.

Copie este arquivo como ``main.py`` no Pico somente depois de validar o V1 atual.
O protocolo legado permanece compativel: BTN_1, BTN_2, RESTART_DOCKER,
PULL_DOCKER, GRN_STARTED e ACK_GRN_STARTED.
"""

CLICK_MAX_MS = 700
SEQUENCE_GAP_MS = 700
MAINTENANCE_HOLD_MIN_MS = 2000
MAINTENANCE_HOLD_MAX_MS = 3000
TRIGGER_HOLD_MIN_MS = 4000
TRIGGER_HOLD_MAX_MS = 5000
SHUTDOWN_HOLD_MIN_MS = 8000
SHUTDOWN_HOLD_MAX_MS = 10000
PULL_HOLD_MIN_MS = 12000
SHUTDOWN_CONFIRM_MS = 5000
WATCHDOG_MS = 10000
DEBOUNCE_MS = 40


def _elapsed(now_ms, then_ms):
    try:
        import time

        return time.ticks_diff(now_ms, then_ms)
    except AttributeError:
        return now_ms - then_ms


class AdminGesture:
    """Classifica gestos somente no release, sem bloquear o loop principal."""

    def __init__(self):
        self.pressed_at = None
        self.clicks = 0
        self.last_release_at = None
        self.shutdown_armed_at = None

    def update(self, pressed, now_ms):
        events = []
        if pressed and self.pressed_at is None:
            self.pressed_at = now_ms
        elif not pressed and self.pressed_at is not None:
            held_ms = _elapsed(now_ms, self.pressed_at)
            self.pressed_at = None
            if held_ms <= CLICK_MAX_MS:
                if (
                    self.shutdown_armed_at is not None
                    and _elapsed(now_ms, self.shutdown_armed_at) <= SHUTDOWN_CONFIRM_MS
                ):
                    self.shutdown_armed_at = None
                    self.clicks = 0
                    self.last_release_at = None
                    events.append("SHUTDOWN_HOST")
                else:
                    self.clicks += 1
                    self.last_release_at = now_ms
            else:
                self.clicks = 0
                self.last_release_at = None
                if MAINTENANCE_HOLD_MIN_MS <= held_ms <= MAINTENANCE_HOLD_MAX_MS:
                    events.append("RUN_SELF_TEST")
                elif TRIGGER_HOLD_MIN_MS <= held_ms <= TRIGGER_HOLD_MAX_MS:
                    events.append("TRIGGER_GLOBAL")
                elif SHUTDOWN_HOLD_MIN_MS <= held_ms <= SHUTDOWN_HOLD_MAX_MS:
                    self.shutdown_armed_at = now_ms
                    events.append("ARM_SHUTDOWN")
                elif held_ms >= PULL_HOLD_MIN_MS:
                    events.append("PULL_DOCKER")
                else:
                    events.append("GESTURE_CANCELLED")

        if (
            not pressed
            and self.clicks
            and self.last_release_at is not None
            and _elapsed(now_ms, self.last_release_at) >= SEQUENCE_GAP_MS
        ):
            actions = {
                2: "REQUEST_DIAGNOSTIC",
                3: "TOGGLE_MAINTENANCE",
                5: "RESTART_DOCKER",
            }
            events.append(actions.get(self.clicks, "GESTURE_CANCELLED"))
            self.clicks = 0
            self.last_release_at = None

        if (
            self.shutdown_armed_at is not None
            and _elapsed(now_ms, self.shutdown_armed_at) > SHUTDOWN_CONFIRM_MS
        ):
            self.shutdown_armed_at = None
            events.append("GESTURE_CANCELLED")
        return events


class DebouncedButton:
    def __init__(self, initial):
        self.stable = initial
        self.candidate = initial
        self.candidate_since = 0

    def update(self, raw, now_ms):
        if raw != self.candidate:
            self.candidate = raw
            self.candidate_since = now_ms
        elif (
            raw != self.stable
            and _elapsed(now_ms, self.candidate_since) >= DEBOUNCE_MS
        ):
            self.stable = raw
        return self.stable


class OperationalState:
    def __init__(self):
        self.camera = "STARTING"
        self.mqtt = "DISCONNECTED"
        self.upload = "IDLE"
        self.maintenance = "OFF"
        self.last_ping_at = None
        self.edge_started = False
        self.transient = None
        self.transient_started_at = 0
        self.transient_duration_ms = 0

    def set_transient(self, value, now_ms, duration_ms):
        self.transient = value
        self.transient_started_at = now_ms
        self.transient_duration_ms = duration_ms

    def apply_command(self, line, now_ms):
        if line == "GRN_STARTED":
            self.edge_started = True
            self.camera = "READY"
            return ["ACK_GRN_STARTED", "PICO_CAPS:2:DUAL_LED,HEARTBEAT,ACTIONS"]
        if line.startswith("PING:"):
            self.last_ping_at = now_ms
            return ["PONG:" + line.split(":", 1)[1]]
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "STATE":
            key, value = parts[1], parts[2]
            if key == "CAMERA":
                self.camera = value
            elif key == "MQTT":
                self.mqtt = value
            elif key == "UPLOAD":
                self.upload = value
            elif key == "MAINTENANCE":
                self.maintenance = value
            return []
        if len(parts) >= 3 and parts[0] == "FEEDBACK":
            category, result = parts[1], parts[2]
            if category == "DIAG" and result == "RUNNING":
                self.set_transient("DIAG_RUNNING", now_ms, 5000)
            elif result in ("OK", "ACCEPTED") and self.transient not in (
                "RESTART",
                "PULL",
            ):
                self.set_transient("SUCCESS", now_ms, 2000)
            elif category == "ACTION" and result == "ARMED":
                self.set_transient("ARMED", now_ms, SHUTDOWN_CONFIRM_MS)
            elif (
                result.startswith("FAIL")
                or result.startswith("REJECTED")
                or result.startswith("DENIED")
            ):
                self.set_transient("FAIL", now_ms, 5000)
        return []

    def leds(self, now_ms):
        if (
            self.edge_started
            and self.last_ping_at is not None
            and _elapsed(now_ms, self.last_ping_at) > WATCHDOG_MS
        ):
            phase = (now_ms // 125) % 2
            return phase == 0, phase == 1

        if (
            self.transient
            and _elapsed(now_ms, self.transient_started_at)
            <= self.transient_duration_ms
        ):
            transient_elapsed = _elapsed(now_ms, self.transient_started_at)
            if self.transient == "SUCCESS":
                return True, True
            if self.transient == "ARMED":
                on = (transient_elapsed // 150) % 2 == 0
                return on, on
            if self.transient == "DIAG_RUNNING":
                return (transient_elapsed // 200) % 2 == 0, False
            if self.transient == "FAIL":
                on = (transient_elapsed // 100) % 2 == 0
                return on, on
            if self.transient in ("RESTART", "PULL"):
                on = (transient_elapsed // 150) % 2 == 0
                return on, False
        elif self.transient:
            self.transient = None

        if self.maintenance == "ON":
            phase = (now_ms // 250) % 2
            return phase == 0, phase == 1

        cycle = now_ms % 2000
        if self.camera == "READY":
            system_on = True
        elif self.camera == "DEGRADED":
            system_on = cycle < 120 or 260 <= cycle < 380
        elif self.camera == "ERROR":
            system_on = cycle < 100 or 200 <= cycle < 300 or 400 <= cycle < 500
        else:
            system_on = cycle < 500

        if self.upload == "PENDING":
            activity_on = (now_ms // 250) % 2 == 0
        else:
            activity_on = self.mqtt == "CONNECTED"
        return system_on, activity_on


def run():
    import sys
    import time
    import uselect
    from machine import Pin

    led_system = Pin(14, Pin.OUT, value=1)
    led_activity = Pin(13, Pin.OUT, value=1)
    btn_1 = Pin(2, Pin.IN, Pin.PULL_UP)
    btn_2 = Pin(3, Pin.IN, Pin.PULL_UP)
    btn_admin = Pin(15, Pin.IN, Pin.PULL_UP)
    poll = uselect.poll()
    poll.register(sys.stdin, uselect.POLLIN)
    state = OperationalState()
    gesture = AdminGesture()
    dedicated_previous = [btn_1.value(), btn_2.value()]
    dedicated_last_emit = [-1000, -1000]
    admin_debounced = DebouncedButton(btn_admin.value() == 0)
    print("PICO_CAPS:2:DUAL_LED,HEARTBEAT,ACTIONS")

    while True:
        now_ms = time.ticks_ms()
        for index, button in enumerate((btn_1, btn_2)):
            current = button.value()
            if (
                dedicated_previous[index] == 0
                and current == 1
                and _elapsed(now_ms, dedicated_last_emit[index]) >= 200
            ):
                print("BTN_%d" % (index + 1))
                dedicated_last_emit[index] = now_ms
            dedicated_previous[index] = current

        admin_pressed = admin_debounced.update(btn_admin.value() == 0, now_ms)
        for event in gesture.update(admin_pressed, now_ms):
            if event == "GESTURE_CANCELLED":
                state.set_transient("FAIL", now_ms, 1000)
            else:
                if event == "RESTART_DOCKER":
                    state.set_transient("RESTART", now_ms, 1500)
                elif event == "PULL_DOCKER":
                    state.set_transient("PULL", now_ms, 600)
                print(event)

        for _event, _flags in poll.poll(0):
            line = sys.stdin.readline().strip().upper()
            for response in state.apply_command(line, now_ms):
                print(response)

        system_on, activity_on = state.leds(now_ms)
        led_system.value(0 if system_on else 1)
        led_activity.value(0 if activity_on else 1)
        time.sleep_ms(10)


if __name__ == "__main__":
    run()
