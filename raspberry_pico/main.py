import sys
from machine import Pin
import time

TRIGGER_TOKEN = "BTN_REPLAY"
button = Pin(15, Pin.IN, Pin.PULL_UP)

last_state = 1
debounce_ms = 200
last_press = 0

while True:
    current = button.value()

    if current == 0 and last_state == 1:
        now = time.ticks_ms()
        if time.ticks_diff(now, last_press) > debounce_ms:
            sys.stdout.write(TRIGGER_TOKEN + "\n")
            try:
                sys.stdout.flush()
            except Exception:
                pass
            last_press = now

    last_state = current
    time.sleep_ms(10)
