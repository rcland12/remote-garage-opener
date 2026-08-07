# Flashing and deploying

One-time MicroPython install, then a two-second deploy loop for firmware
changes.

## Host setup

    source ~/.global_venv/bin/activate
    pip install esptool mpremote

On Linux your user needs serial access:

    sudo usermod -aG dialout $USER      # log out and back in

### On Windows

Install Python from python.org with "Add Python to PATH" ticked, then
`pip install esptool mpremote` as above. Ports are `COM3`, `COM5` and so on;
there is no group to join. If PowerShell refuses to activate a venv, that is
the execution policy: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Use `tools/deploy.py` and `tools/smoke_test.py` rather than the `.sh`
versions. They are standard library only and take the same arguments.

Do not flash from inside WSL. WSL2 cannot see a USB serial device without
`usbipd-win` and an elevated re-attach on every replug, and this board gets
unplugged a lot during bring-up. Run the tools in native Windows.

Note that in Windows PowerShell 5.1 `curl` is an alias for `Invoke-WebRequest`
and takes entirely different arguments. Any `curl` line in these docs needs to
be `curl.exe`, or use the Python tools instead.

## Which USB port

Dev boards with two USB-C ports label them `COM` and `USB`. They are not
interchangeable:

- `COM` goes to the USB-to-UART bridge chip. Its DTR and RTS lines drive GPIO 0
  and EN, which is what lets esptool enter the ROM bootloader on its own. It
  stays enumerated no matter what the firmware does.
- `USB` goes to the S3's native USB peripheral on GPIO 19 and 20. It can flash,
  but there is no auto-reset circuit, so a wedged firmware means holding BOOT
  and tapping RESET by hand. It also drops off the host whenever the running
  firmware reconfigures USB.

Use `COM`. This firmware arms a watchdog and reboots itself, so the port that
survives reboots is the one you want.

## Find the port

Plug the board in and check:

    ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null

Do not identify the port by its device name. Whether a port appears as
`ttyACM*` or `ttyUSB*` depends on the bridge chip's USB class, not on which
of the two ports it is:

- CH343 and other CDC-class bridges bind to `cdc_acm` and appear as
  `/dev/ttyACM0`, the same name native USB uses.
- CH340 and CP210x need vendor drivers and appear as `/dev/ttyUSB0`.

Use the USB vendor ID to tell them apart:

    lsusb

    1a86:...    QinHeng/WCH bridge chip  -> this is the COM port
    10c4:...    Silicon Labs CP210x      -> this is the COM port
    303a:...    Espressif                -> this is the native USB port

If nothing appears at all, the cable is charge-only; swap it for a data cable.

The board this repo was built against is a CH343 bridge, `1a86:55d3`, and its
COM port enumerates as `/dev/ttyACM0`.

If the board is already running something that grabs the USB peripheral, hold
BOOT, tap RESET, then release BOOT to force the ROM bootloader.

## Install MicroPython

Download the current stable `.bin` for the `ESP32_GENERIC_S3` board from
<https://micropython.org/download/ESP32_GENERIC_S3/>. If your board has 8 MB
of octal SPIRAM, take the SPIRAM_OCT variant; the plain build is the safe
default and this firmware needs almost no RAM either way.

Ask the chip which one it is before downloading:

    esptool --chip esp32s3 --port /dev/ttyACM0 flash-id

Read the `Features` and `Detected flash size` lines. 8 MB of embedded PSRAM
means an octal part, so take SPIRAM_OCT; the 2 MB quad-PSRAM modules and the
no-PSRAM boards take the plain build.

The board this repo was built against reports 16 MB flash and `Embedded PSRAM
8MB`, an N16R8 module, running `ESP32_GENERIC_S3-SPIRAM_OCT` v1.28.0. After
boot it reports roughly 8.3 MB of free heap, which confirms PSRAM came up.

    esptool --chip esp32s3 --port /dev/ttyACM0 erase-flash
    esptool --chip esp32s3 --port /dev/ttyACM0 --baud 460800 \
        write-flash -z 0 ESP32_GENERIC_S3-<version>.bin

Note the flash offset is `0`, not the `0x1000` used by the original ESP32.

esptool v4 and older spell the subcommands with underscores (`erase_flash`,
`write_flash`) and install as `esptool.py`. Both spellings work on v5.

Confirm you have a REPL:

    mpremote connect /dev/ttyACM0 repl

You should get a `>>>` prompt. Ctrl-] exits.

## Configure

    cp firmware/secrets.example.py firmware/secrets.py
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"

Put the WiFi SSID, WiFi password and that generated token into
`firmware/secrets.py`. It is gitignored; keep it that way.

Leave `DRY_RUN = True` in `firmware/config.py` until the wiring has been
verified through step 4 of `docs/hardware.md`.

## Deploy

    ./tools/deploy.sh                    # auto-detect port
    ./tools/deploy.sh /dev/ttyACM0       # explicit
    ./tools/deploy.sh --reset            # deploy, reboot, open the console

Or the cross-platform equivalent, which additionally applies the vendor-id
rule above rather than making you run `lsusb` yourself:

    python tools/deploy.py --list             # candidate ports, bridge first
    python tools/deploy.py --port /dev/ttyACM0
    python tools/deploy.py --port COM5 --reset

The script refuses to run if `secrets.py` is missing or still holds the
placeholder token, and reminds you when `DRY_RUN` is on.

Equivalent by hand:

    mpremote connect /dev/ttyACM0 fs cp firmware/config.py :config.py
    mpremote connect /dev/ttyACM0 fs cp firmware/garage.py :garage.py
    mpremote connect /dev/ttyACM0 fs cp firmware/main.py :main.py
    mpremote connect /dev/ttyACM0 fs cp firmware/secrets.py :secrets.py
    mpremote connect /dev/ttyACM0 reset

`main.py` runs automatically at boot. The serial console prints every event
that `GET /log` returns, which is how you read the device's IP address the
first time:

    mpremote connect /dev/ttyACM0 repl

    [     0s] boot reset=power-on dry_run=True
    [     0s] wifi_connect your-ssid
    [     2s] wifi_up 192.168.1.50
    [     2s] http_up port 80

## Stopping the firmware to get a clean REPL

The main loop runs forever and arms a watchdog, so a plain Ctrl-C at the REPL
gets interrupted by the next reboot. To work interactively, either set
`WDT_ENABLED = False` in `config.py` and redeploy, or interrupt and
immediately remove `main.py`:

    mpremote connect /dev/ttyACM0 fs rm :main.py

The watchdog cannot be disabled at runtime once started; that is a hardware
property of the ESP32, and it is the behaviour we want in service.

## Testing without hardware

`tools/simulate.py` runs the real `firmware/main.py` on your desktop against
stub `machine` and `network` modules. Useful for changing the API without
touching the board:

    ./tools/simulate.py --port 8099

    # in another shell
    GARAGE_HOST=127.0.0.1:8099 GARAGE_TOKEN=simulator-token \
        ./tools/smoke_test.sh --pulse

The simulator prints every relay pin transition with a timestamp, so you can
confirm pulse width without a scope. A healthy run shows the pin going to its
active level and back roughly 500 ms later, and nothing else touching a pin.
