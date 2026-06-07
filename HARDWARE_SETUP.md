# MavixBoard — настройка железа (Raspberry Pi + полётный контроллер)

Практическое руководство по подготовке борта: ОС на Raspberry Pi, сеть/LTE,
камеры, **UART и права на serial-порты**, прошивка и настройка полётного
контроллера (Betaflight / iNav / ArduPilot / PX4), порядок ARM и диагностика.

> Материал перенесён из исходного руководства проекта и сохраняет свою
> ценность для борта системы доставки: аппаратная часть (RPi + FC + UART) не
> изменилась. Прикладная часть (enrollment, .env, автозапуск) — см.
> [TECHNICAL.md](TECHNICAL.md) и [USER_GUIDE.md](USER_GUIDE.md).

---

## 1. Raspberry Pi

### 1.1 ОС — Ubuntu Server 24.04 LTS
Записать образ на microSD (Raspberry Pi Imager; можно сразу задать имя
пользователя, пароль, Wi-Fi). Обновить:
```bash
sudo apt update && sudo apt upgrade -y
```

### 1.2 Сеть
Wi-Fi через netplan (`/etc/netplan/50-cloud-init.yaml`):
```yaml
network:
  version: 2
  renderer: networkd
  wifis:
    wlan0:
      dhcp4: true
      access-points:
        "SSID": { password: "password" }
```
```bash
sudo netplan apply
```
Рекомендуется NetworkManager (несколько сетей, LTE):
```bash
sudo apt install network-manager
sudo systemctl disable systemd-networkd
sudo systemctl enable --now NetworkManager
sudo reboot
# затем renderer: NetworkManager в netplan; интерактивно — sudo nmtui
```
mDNS (доступ по имени `mavixboard.local`):
```bash
sudo apt install avahi-daemon
sudo hostnamectl set-hostname mavixboard
sudo systemctl enable --now avahi-daemon
```

### 1.3 LTE-модуль (мобильный интернет)
LTE-HAT (Waveshare SIM7600/SIM8202): SIM вставить **до** подачи питания.
```bash
lsusb; ls /dev/ttyUSB*; ip link show       # модуль определился
sudo apt install modemmanager
sudo systemctl enable --now ModemManager
mmcli -L
sudo nmcli connection add type gsm ifname '*' con-name lte apn internet
sudo nmcli connection modify lte connection.autoconnect yes
sudo nmcli connection up lte
```
APN: Tele2 `internet.tele2.ru`, МТС `internet.mts.ru`, Билайн
`internet.beeline.ru`, Мегафон `internet`. Приоритет LTE над Wi-Fi:
`sudo nmcli connection modify lte ipv4.route-metric 50`. Диагностика:
`mmcli -m 0`, `journalctl -u ModemManager -f`.

### 1.4 Камеры
USB: `lsusb`, `ls -l /dev/video*`, тест `fswebcam -d /dev/video0 photo.jpg`.
CSI (libcamera) — сборка из исходников:
```bash
sudo apt install -y git python3-pip meson cmake ninja-build build-essential \
    libboost-dev libgnutls28-dev libtiff5-dev pybind11-dev python3-yaml python3-ply \
    libglib2.0-dev libgstreamer-plugins-base1.0-dev libdrm-dev libexif-dev \
    libjpeg-dev libpng-dev v4l-utils
git clone https://github.com/raspberrypi/libcamera.git && cd libcamera
meson setup build --buildtype=release -Dpipelines=rpi/vc4,rpi/pisp \
    -Dipas=rpi/vc4,rpi/pisp -Dv4l2=true -Dgstreamer=enabled -Dtest=false \
    -Dlc-compliance=disabled -Dcam=disabled -Dqcam=disabled -Ddocumentation=disabled
ninja -C build && sudo ninja -C build install
echo "/usr/local/lib/aarch64-linux-gnu" | sudo tee /etc/ld.so.conf.d/rpicam.conf
sudo ldconfig
```

### 1.5 UART для CRSF-полётника — и ПРАВА НА ПОРТЫ ⚠️
В `/boot/firmware/config.txt`:
```
enable_uart=1
dtoverlay=disable-bt
```
> `dtoverlay=disable-bt` **обязателен** — отключает Bluetooth и переключает
> GPIO14/15 с mini-UART на полноценный PL011 (`ttyAMA0`), держащий 420000 бод.

Отключить serial-console: `sudo raspi-config` → Interface Options → Serial Port
→ Login shell: **No**, Hardware: **Yes** (или убрать `console=serial0,115200`
из `/boot/firmware/cmdline.txt`).

**Права на serial-порты (чтобы не требовался sudo при каждом старте):**
```bash
sudo usermod -aG dialout rpi
sudo usermod -aG tty rpi
```
Постоянное udev-правило `/etc/udev/rules.d/99-serial.rules`:
```
KERNEL=="ttyAMA[0-9]*", GROUP="dialout", MODE="0660"
KERNEL=="ttyUSB[0-9]*", GROUP="dialout", MODE="0660"
KERNEL=="ttyACM[0-9]*", GROUP="dialout", MODE="0660"
```
```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo systemctl stop serial-getty@ttyAMA0.service
sudo systemctl disable serial-getty@ttyAMA0.service
sudo reboot
```
После этого порт `/dev/ttyAMA0` доступен без `sudo chmod` при каждом старте.
**Перезагрузить RPi после всех изменений в config.txt.**

### 1.6 Зависимости GStreamer
```bash
sudo apt-get install -y python3-gi python3-gi-cairo \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    gstreamer1.0-plugins-rtp gstreamer1.0-x gstreamer1.0-nice \
    gstreamer1.0-libcamera libcamera-v4l2 libcairo2-dev libgirepository1.0-dev
```
> `gir1.2-gst-plugins-bad-1.0` даёт typelib `GstWebRTC` — без него запуск падает
> с `ValueError: Namespace GstWebRTC not available`.

Запуск/автозапуск борта (enrollment, `.env`, systemd) — см. [TECHNICAL.md](TECHNICAL.md).
Группы для systemd-юнита: `SupplementaryGroups=dialout video`.

---

## 2. Полётный контроллер

### 2.0 Прошивка и выбор таргета
Неверный таргет → сенсоры не определяются (GYRO/ACC=UNAVAILABLE), FC нерабочий.

| Плата | Betaflight | iNav | ArduPilot |
|---|---|---|---|
| Skystars H743 HD | `SKYSTARSH7HD` | `SKYSTARSH743HD` | `SKYSTARSH7HD` |
| Matek H743-WING/SLIM/MINI | `MATEKH743` | `MATEKH743` | `MATEKH743` |
| Pixracer | — | — | `Pixracer` / `Pixracer-bdshot` |

> Skystars H743 и Matek H743 — **разные платы** (хотя обе на STM32H743). При
> смене прошивки ставить галку **Full chip erase**. Проверка сенсоров в CLI:
> `status` → ожидаемо `GYRO=OK, ACC=OK, BARO=OK`. Если `UNAVAILABLE` —
> `set acc_hardware = AUTO` / `baro_hardware` / `gyro_hardware` → `save`.

ArduPilot ≥4.0 — только **ChibiOS** (NuttX убран). PX4 — NuttX.

### 2.1 Провода RPi → FC (CRSF, 3 провода, оба 3.3В)
```
RPi пин 8  (GPIO14, TX) ─► FC UART RX (напр. R4)
FC UART TX (напр. T4)   ─► RPi пин 10 (GPIO15, RX)
RPi пин 6  (GND)        ─► FC GND
```
> TX↔RX **перекрёстно**! 5V не нужен. `serial N` ↔ UART(N+1): serial 3 = UART4 (T4/R4).

### 2.2 Betaflight (CRSF), UART4
```
serial 3 64 115200 57600 0 115200
set serialrx_provider = CRSF
set serialrx_inverted = OFF
feature TELEMETRY
map TAER1234
aux 0 0 0 1800 2100 0 0        # ARM = AUX1 (CH5), 1800–2100
set failsafe_procedure = GPS-RESCUE
set failsafe_delay = 15
save
```
Linux, если Configurator не видит порт: `sudo usermod -a -G dialout $USER` (перелогиниться).

### 2.3 iNav (CRSF) — отличия
iNav требует `receiver_type = SERIAL` и `serialrx_halfduplex = OFF`, иначе CRSF
не принимается. Полный конфиг через CLI (UART4 = serial 3):
```
serial 3 64 115200 115200 0 115200
set receiver_type = SERIAL
set serialrx_provider = CRSF
set serialrx_inverted = OFF
set serialrx_halfduplex = OFF
set small_angle = 180          # убрать блокировку ARM по наклону
set min_check = 1000           # CRSF throttle в покое ≈989 → иначе флаг THR
feature TELEMETRY
map TAER
aux 0 0 0 1800 2100 0 0        # ARM
aux 1 3 0 900 2100 0 0         # ANGLE (стабилизация всегда вкл — без ACRO!)
calibrate acc                  # FC ровно на столе
save
```
> В iNav скорость CRSF жёстко 420000 бод — baudrate в `serial` игнорируется.
> Failsafe: `set failsafe_procedure = DROP`.

### 2.4 MAVLink (ArduPilot / PX4)
Борт автоопределяет MAVLink FC по USB (`/dev/ttyACM*`) и работает через
QGroundControl (UDP 14550), настройка не требуется. Полезные параметры ArduPilot
(QGC → Parameters):
- `BRD_SAFETY_DEFLT = Disabled` — отключить кнопку безопасности (если её нет);
- `ARMING_CHECK = 0` — отключить ВСЕ проверки (**только наземные тесты без пропеллеров!**);
- `LOG_BACKEND_TYPE = 0` — если бесконечный «Initialising ArduPilot» (зависание на SD; либо отформатировать SD в FAT32);
- `RC_PROTOCOLS = 1` (PPM) / `256` (iBUS) — для FlySky FS-iA6B и подобных.

### 2.5 Порядок ARM
1. Отключить USB. 2. Подключить батарею. 3. Подождать 5 c. 4. Газ вниз.
5. Щёлкнуть тумблер ARM. **Снять пропеллеры на тестах!**

---

## 3. Диагностика

### RXLOSS — FC не видит сигнал
1. Провода: RPi TX → FC RX (не наоборот). 2. `dtoverlay=disable-bt` в config.txt.
3. Serial-console отключена (raspi-config).

### FC не армируется — `status` → `Arming disable flags`
**Betaflight:** `MSP` (USB подключён → питать от батареи), `RXLOSS` (см. выше),
`THROTTLE` (газ вниз), `ANGLE` (`set small_angle = 180`), `ARM_SWITCH`
(выключить тумблер и включить).
**iNav:** `CLI` (`exit`), `CAL`/`ACC` (`calibrate acc` + `set small_angle = 180`),
`THR` (`set min_check = 1000`), `RXLOSS` (`receiver_type = SERIAL`), `NAV UNSAFE`
(нет GPS — отключить нав-режимы), `HW` (`set acc_hardware = AUTO`).
> Одноразовый фикс первого ARM (iNav): `set small_angle = 180` → `set min_check = 1000`
> → `calibrate acc` → `save` → `exit`.
**ArduPilot:** `Hardware safety switch` (нажать кнопку / `BRD_SAFETYENABLE = 0`),
`System not initialised` (подождать 15–30 c), `RC/Compass not calibrated`
(QGC → Radio/Compass Calibration).

### WebRTC не поднимается
См. подробный разбор TURN/ICE и костыли совместимости aiortc↔webrtcbin —
[WEBRTC_TURN_NOTES.md](WEBRTC_TURN_NOTES.md). Кратко: `Fatal SSL error` →
проверить `a=setup:passive` в answer; ICE gathering зависает → STUN; ICE
completed/DTLS stuck → `GLib.MainLoop` запущен в треде; нет offer → `latency=0`
на webrtcbin до `set_state(PLAYING)`.

---

## 4. Справка по протоколам

### CRSF (Crossfire Serial Protocol, TBS)
Физика: **420000 бод**. Фрейм `[addr][len][type][payload][crc8]`,
crc8 = CRC-8/DVB-S2 (0xD5) по `[type, payload]`. Каналы: `CH_MIN=172` (≈988 мкс),
`CH_CENTER=992` (≈1500), `CH_MAX=1811` (≈2012). Маппинг TAER: CH1 Throttle,
CH2 Roll, CH3 Pitch, CH4 Yaw, CH5 ARM (>1800 = armed). Кадры: `0x16`
RC_CHANNELS_PACKED, `0x14` LINK_STATISTICS, `0x08` BATTERY_SENSOR, `0x02` GPS,
`0x1E` ATTITUDE, `0x28/0x29` DEVICE_PING/INFO. Спецификация:
https://github.com/crsf-wg/crsf-spec

### MAVLink (ArduPilot / PX4)
USB (`/dev/ttyACM*`) или UART 115200. Борт фильтрует важные сообщения
(HEARTBEAT, SYS_STATUS, GLOBAL_POSITION_INT, BATTERY_STATUS, COMMAND_ACK и т.д.),
остальные прореживает. Идентификация FC по `MAV_AUTOPILOT` в HEARTBEAT
(3=ArduPilot, 12=PX4). Документация: https://mavlink.io/
