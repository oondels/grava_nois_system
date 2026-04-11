import sys
from machine import Pin
import time

BUTTON_ONE = "BTN_1"
BUTTON_TWO = "BTN_2"

DOCKER_PULL = "PULL_DOCKER"
DOCKER_RESTART = "RESTART_DOCKER"

# =========================
# CONFIGURAÇÕES DE LED
# =========================
LED_PIN = 14

LED_BLINK_PULL_TIMES = 2
LED_BLINK_RESTART_TIMES = 5

LED_BLINK_ON_MS = 150
LED_BLINK_OFF_MS = 150

# =========================

button_one = Pin(2, Pin.IN, Pin.PULL_UP)
button_two = Pin(3, Pin.IN, Pin.PULL_UP)
button_docker = Pin(15, Pin.IN, Pin.PULL_UP)

led = Pin(LED_PIN, Pin.OUT)
led.value(0)  # sempre aceso

last_state_one = 1
last_state_two = 1
last_state_docker = 1

last_press_one = 0
last_press_two = 0
last_press_docker = 0

debounce_ms = 200

hold_time_ms = 5000
multi_click_window_ms = 1500

docker_press_start = None
docker_hold_fired = False
docker_click_count = 0
docker_first_click_time = 0


def emit(trigger_name):
    sys.stdout.write(trigger_name + "\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def led_on():
    led.value(0)


def led_off():
    led.value(1)


def blink_led(times, on_ms=LED_BLINK_ON_MS, off_ms=LED_BLINK_OFF_MS):
    for _ in range(times):
        led_off()
        time.sleep_ms(off_ms)
        led_on()
        time.sleep_ms(on_ms)


while True:
    now = time.ticks_ms()

    current_one = button_one.value()
    current_two = button_two.value()
    current_docker = button_docker.value()

    # BTN_1
    if current_one == 0 and last_state_one == 1:
        if time.ticks_diff(now, last_press_one) > debounce_ms:
            emit(BUTTON_ONE)
            last_press_one = now

    # BTN_2
    if current_two == 0 and last_state_two == 1:
        if time.ticks_diff(now, last_press_two) > debounce_ms:
            emit(BUTTON_TWO)
            last_press_two = now

    # BOTÃO DA PORTA 15 - pressionou
    if current_docker == 0 and last_state_docker == 1:
        if time.ticks_diff(now, last_press_docker) > debounce_ms:
            docker_press_start = now
            docker_hold_fired = False
            last_press_docker = now

    # SEGURANDO (PULL_DOCKER)
    if current_docker == 0 and docker_press_start is not None and not docker_hold_fired:
        if time.ticks_diff(now, docker_press_start) >= hold_time_ms:
            blink_led(LED_BLINK_PULL_TIMES)
            emit(DOCKER_PULL)
            docker_hold_fired = True
            docker_click_count = 0
            docker_first_click_time = 0

    # SOLTOU
    if current_docker == 1 and last_state_docker == 0:
        if docker_press_start is not None:
            press_duration = time.ticks_diff(now, docker_press_start)

            if not docker_hold_fired and press_duration < hold_time_ms:
                if docker_click_count == 0:
                    docker_first_click_time = now
                    docker_click_count = 1
                else:
                    if time.ticks_diff(now, docker_first_click_time) <= multi_click_window_ms:
                        docker_click_count += 1
                    else:
                        docker_first_click_time = now
                        docker_click_count = 1

                if docker_click_count >= 5:
                    blink_led(LED_BLINK_RESTART_TIMES)
                    emit(DOCKER_RESTART)
                    docker_click_count = 0
                    docker_first_click_time = 0

            docker_press_start = None

    # Expiração multi-clique
    if docker_click_count > 0:
        if time.ticks_diff(now, docker_first_click_time) > multi_click_window_ms:
            docker_click_count = 0
            docker_first_click_time = 0

    last_state_one = current_one
    last_state_two = current_two
    last_state_docker = current_docker

    time.sleep_ms(10)
