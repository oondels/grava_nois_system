import sys
import uselect
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

button_one = Pin(2, Pin.IN, Pin.PULL_UP)   # NF
button_two = Pin(3, Pin.IN, Pin.PULL_UP)   # NF
button_docker = Pin(15, Pin.IN, Pin.PULL_UP)  # NA

led = Pin(LED_PIN, Pin.OUT)
led.value(1)  # inicia desligado

# LED só fica aceso continuamente após GRN_STARTED
grn_started = False

# Estados iniciais reais
last_state_one = button_one.value()
last_state_two = button_two.value()
last_state_docker = button_docker.value()

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

poll = uselect.poll()
poll.register(sys.stdin, uselect.POLLIN)


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


def restore_led_state():
    if grn_started:
        led_on()
    else:
        led_off()


def blink_led(times, on_ms=LED_BLINK_ON_MS, off_ms=LED_BLINK_OFF_MS):
    for _ in range(times):
        led_off()
        time.sleep_ms(off_ms)
        led_on()
        time.sleep_ms(on_ms)


def reset_started_state():
    global grn_started
    grn_started = False
    led_off()


def read_command():
    global grn_started

    events = poll.poll(0)
    if events:
        try:
            cmd = sys.stdin.readline().strip()

            if cmd == "GRN_STARTED":
                grn_started = True
                led_on()
                emit("ACK_GRN_STARTED")

        except Exception as e:
            emit("ERR_READ:" + str(e))


while True:
    now = time.ticks_ms()

    read_command()

    current_one = button_one.value()
    current_two = button_two.value()
    current_docker = button_docker.value()

    # BTN_1 (NF): 0 -> 1
    if current_one == 1 and last_state_one == 0:
        if time.ticks_diff(now, last_press_one) > debounce_ms:
            emit(BUTTON_ONE)
            last_press_one = now

    # BTN_2 (NF): 0 -> 1
    if current_two == 1 and last_state_two == 0:
        if time.ticks_diff(now, last_press_two) > debounce_ms:
            emit(BUTTON_TWO)
            last_press_two = now

    # GPIO 15 (NA): pressionou 1 -> 0
    if current_docker == 0 and last_state_docker == 1:
        if time.ticks_diff(now, last_press_docker) > debounce_ms:
            docker_press_start = now
            docker_hold_fired = False
            last_press_docker = now

    # SEGURANDO (NA): nível 0
    if current_docker == 0 and docker_press_start is not None and not docker_hold_fired:
        if time.ticks_diff(now, docker_press_start) >= hold_time_ms:
            blink_led(LED_BLINK_PULL_TIMES)
            emit(DOCKER_PULL)
            reset_started_state()
            docker_hold_fired = True
            docker_click_count = 0
            docker_first_click_time = 0

    # SOLTOU (NA): 0 -> 1
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
                    reset_started_state()
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