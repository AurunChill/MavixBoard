# Mavix — FPV управление дроном через интернет

Система для удалённого пилотирования дрона через WebRTC: видео с камер в реальном времени, управление через джойстик, телеметрия.

```
┌─────────────────────┐        Signal Server        ┌──────────────────────┐
│   PC / GCS          │  ◄── WebRTC (video + data) ──►│   Raspberry Pi       │
│   Receiver          │                              │   Transmitter        │
│   (aiortc)          │                              │   (GStreamer)        │
└─────────────────────┘                              └──────────┬───────────┘
                                                                │ UART / USB
                                                     ┌──────────▼───────────┐
                                                     │  Flight Controller   │
                                                     │  Betaflight / iNav / │
                                                     │  ArduPilot           │
                                                     └──────────────────────┘
```

**Трансмиттер** (RPi) — стримит видео с USB-камер через WebRTC, принимает CRSF-пакеты по datachannel и пробрасывает на полётник по UART.

**Ресивер** (PC) — отображает видео, читает джойстик, формирует CRSF-пакеты и отправляет на дрон.

---

## Содержание

1. [Установка на Raspberry Pi (трансмиттер)](#1-установка-на-raspberry-pi-трансмиттер)
2. [Установка на PC (ресивер)](#2-установка-на-pc-ресивер)
3. [Настройка полётного контроллера](#3-настройка-полётного-контроллера)
4. [Запуск и управление](#4-запуск-и-управление)
5. [Техническое описание](#5-техническое-описание)
6. [Устранение неполадок](#6-устранение-неполадок)
7. [Заметки](#7-заметки)

---

## 1. Установка на Raspberry Pi (трансмиттер)

### 1.1 ОС — Ubuntu Server 24.04.3 LTS

Записать образ на MicroSD через Raspberry Pi Imager. При записи можно сразу задать имя пользователя, пароль и Wi-Fi.

Обновить систему:
```bash
sudo apt update && sudo apt upgrade -y
```

### 1.2 Сеть

**Базовая настройка Wi-Fi** (если не настроено в Imager):
```bash
sudo nano /etc/netplan/50-cloud-init.yaml
```
```yaml
network:
  version: 2
  renderer: networkd
  wifis:
    wlan0:
      dhcp4: true
      access-points:
        "SSID":
          password: "password"
```
```bash
sudo netplan apply
```

**Расширенная настройка через NetworkManager** (рекомендуется — поддерживает несколько сетей, LTE):
```bash
sudo apt install network-manager
sudo systemctl disable systemd-networkd
sudo systemctl enable --now NetworkManager
sudo reboot
```

После перезагрузки:
```bash
sudo nano /etc/netplan/50-cloud-init.yaml
```
```yaml
network:
  version: 2
  renderer: NetworkManager
```

Интерактивное управление подключениями:
```bash
sudo nmtui
```

**mDNS — доступ по имени вместо IP:**
```bash
sudo apt install avahi-daemon
sudo hostnamectl set-hostname mavixboard
sudo systemctl enable --now avahi-daemon
```
После этого RPi доступен как `mavixboard.local`.

**Точка доступа Wi-Fi:**
```bash
sudo nano ~/hotspot.sh
```
```bash
#!/bin/bash
nmcli dev wifi hotspot ifname wlan0 con-name RPI4WIFI ssid RPI4WIFI password Password1234
```
```bash
sudo chmod +x ~/hotspot.sh
sudo nano /etc/systemd/system/hotspot.service
```
```ini
[Unit]
Description=Wi-Fi Hotspot
After=network-manager.service

[Service]
Type=simple
ExecStart=/home/rpi/hotspot.sh
Restart=always
RestartSec=5s
User=root

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hotspot.service
```

### 1.3 LTE-модуль (мобильный интернет)

RPi-совместимые LTE-HAT (Waveshare SIM7600, SIM8202 и аналоги) подключаются физически через 40-pin GPIO, а для данных используют встроенный USB-интерфейс. Вставьте SIM-карту **до** включения питания.

Проверить что модуль определился:
```bash
lsusb                    # новый USB-девайс (Qualcomm, SIMCOM и т.п.)
ls /dev/ttyUSB*          # /dev/ttyUSB0..ttyUSB2
ip link show             # интерфейс usb0, wwan0 или eth1
```

Установить ModemManager и настроить соединение:
```bash
sudo apt install modemmanager
sudo systemctl enable --now ModemManager
mmcli -L                 # убедиться что модем найден
```

Создать GSM-соединение (заменить `internet` на APN вашего оператора):
```bash
sudo nmcli connection add type gsm ifname '*' con-name lte apn internet
sudo nmcli connection modify lte connection.autoconnect yes
sudo nmcli connection up lte
```

> APN операторов: Tele2 — `internet.tele2.ru`, МТС — `internet.mts.ru`, Билайн — `internet.beeline.ru`, Мегафон — `internet`

Если одновременно подключены Wi-Fi и LTE, установить приоритет LTE:
```bash
sudo nmcli connection modify lte ipv4.route-metric 50   # меньше = выше приоритет
sudo nmcli connection up lte
```

Диагностика:
```bash
mmcli -m 0               # уровень сигнала, оператор, тип сети
journalctl -u ModemManager -f
```

### 1.4 Камеры

**USB-камера:**
```bash
lsusb                    # камера видна на шине
ls -l /dev/video*        # определить порт
```

Тест снимка:
```bash
sudo apt install fswebcam
fswebcam -d /dev/video0 photo.jpg
scp rpi@mavixboard.local:~/photo.jpg ~/photo.jpg
```

**CSI-камера (rpicam / libcamera):**

Сборка libcamera:
```bash
sudo apt install -y git python3-pip meson cmake ninja-build build-essential \
    libboost-dev libgnutls28-dev libtiff5-dev pybind11-dev \
    python3-yaml python3-ply libglib2.0-dev libgstreamer-plugins-base1.0-dev \
    libdrm-dev libexif-dev libjpeg-dev libpng-dev v4l-utils

git clone https://github.com/raspberrypi/libcamera.git && cd libcamera
meson setup build --buildtype=release \
    -Dpipelines=rpi/vc4,rpi/pisp -Dipas=rpi/vc4,rpi/pisp \
    -Dv4l2=true -Dgstreamer=enabled -Dtest=false \
    -Dlc-compliance=disabled -Dcam=disabled -Dqcam=disabled -Ddocumentation=disabled
ninja -C build && sudo ninja -C build install
echo "/usr/local/lib/aarch64-linux-gnu" | sudo tee /etc/ld.so.conf.d/rpicam.conf
sudo ldconfig
```

Сборка rpicam-apps:
```bash
git clone https://github.com/raspberrypi/rpicam-apps.git && cd rpicam-apps
meson setup build -Denable_libav=disabled -Denable_drm=enabled -Denable_egl=disabled
ninja -C build && sudo ninja -C build install && sudo ldconfig
```

### 1.5 UART для CRSF-полётника

Добавить в `/boot/firmware/config.txt`:
```
enable_uart=1
dtoverlay=disable-bt
```

> `dtoverlay=disable-bt` **обязателен** — отключает Bluetooth и переключает GPIO14/15 с mini-UART на полноценный PL011 UART (ttyAMA0), который держит 420000 бод.

Отключить serial console:
```bash
sudo raspi-config
```
→ Interface Options → Serial Port → Login shell: **No** / Hardware: **Yes**

Или вручную убрать `console=serial0,115200` из `/boot/firmware/cmdline.txt`.

**Права на serial-порты** (чтобы не требовался sudo):
```bash
sudo usermod -aG dialout rpi
sudo usermod -aG tty rpi
```

Создать постоянное udev-правило:
```bash
sudo nano /etc/udev/rules.d/99-serial.rules
```
```
KERNEL=="ttyAMA[0-9]*", GROUP="dialout", MODE="0660"
KERNEL=="ttyUSB[0-9]*", GROUP="dialout", MODE="0660"
KERNEL=="ttyACM[0-9]*", GROUP="dialout", MODE="0660"
```
```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Настройка порта ttyAMA0:
```bash
(.venv) rpi@rpi4:~/CRSF/src$ sudo systemctl stop serial-getty@ttyAMA0.service
(.venv) rpi@rpi4:~/CRSF/src$ sudo systemctl disable serial-getty@ttyAMA0.service
(.venv) rpi@rpi4:~/CRSF/src$ sudo nano /boot/firmware/cmdline.txt 
(.venv) rpi@rpi4:~/CRSF/src$ sudo reboot now
```

После этого порты работают без `sudo chmod` при каждом старте.

**Перезагрузить RPi после всех изменений в config.txt.**

### 1.6 Зависимости трансмиттера

GStreamer (системные пакеты):
```bash
sudo apt-get install -y \
    python3-gi python3-gi-cairo \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    gstreamer1.0-plugins-rtp gstreamer1.0-x \
    gstreamer1.0-nice gstreamer1.0-libcamera libcamera-v4l2 \
    libcairo2-dev libgirepository1.0-dev
```

> `gir1.2-gst-plugins-bad-1.0` предоставляет typelib для `GstWebRTC` — без него запуск упадёт с `ValueError: Namespace GstWebRTC not available`.

Python-пакеты:
```bash
cd WebRTC/board
pip install -r requirements.txt
pip install PyGObject
```

### 1.7 Копирование на RPi и запуск

Скопировать трансмиттер на RPi:
```bash
scp -r WebRTC/board/src/* rpi@mavixboard.local:/home/rpi/Mavix/src/
```

Запустить вручную:
```bash
cd /home/rpi/Mavix/src
python3 main.py
```

Настройки через `.env` (создаётся при первом запуске):
- `SIGNAL_URL` — WebSocket-адрес сигнального сервера
- `CONNECTION_TOKEN` — токен авторизации
- `TAG` — тег устройства (должен быть `drone`)

### 1.8 Автозапуск через systemd

```bash
sudo nano /etc/systemd/system/mavix.service
```
```ini
[Unit]
Description=Mavix WebRTC Transmitter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/rpi/Mavix/src
ExecStart=/home/rpi/Mavix/.venv/bin/python3 /home/rpi/Mavix/src/main.py
Restart=on-failure
RestartSec=5s
User=rpi
SupplementaryGroups=dialout video
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

> `ExecStart` использует Python из venv напрямую — активировать venv отдельно не нужно.

```bash
sudo systemctl daemon-reload
sudo systemctl enable mavix.service
sudo systemctl start mavix.service
```

Управление сервисом:
```bash
sudo systemctl status mavix.service
sudo journalctl -u mavix.service -f        # логи в реальном времени
sudo journalctl -u mavix.service -n 100    # последние 100 строк
sudo systemctl restart mavix.service
sudo systemctl stop mavix.service
```

---

## 2. Установка на PC (ресивер)

### 2.1 Зависимости

```bash
cd WebRTC/qgs
pip install -r requirements.txt
```

### 2.2 Запуск

```bash
cd WebRTC/qgs/src
python3 main.py
```

1. Введите токен подключения
2. Выберите дрон из списка
3. Настройте джойстик
4. Взлёт

### 2.3 Калибровка джойстика (Radiomaster Pocket, Mode 2 / EdgeTX)

| Pygame ось | Стик | Канал | Функция |
|---|---|---|---|
| A0 | Правый X | CH2 | Крен (Roll) |
| A1 | Правый Y | CH3 | Тангаж (Pitch) |
| A2 | Левый Y | CH1 | Тяга (Throttle) |
| A3 | Левый X | CH4 | Рыскание (Yaw) |
| A4+ | Тумблеры | CH5+ | ARM и т.д. |

> Тумблеры на Pocket экспортируются как **оси**, не кнопки — ресивер определяет тип автоматически при калибровке.

Файл калибровки сохраняется в `qgs/src/_data/<имя_джойстика>.json`.
SDL-строка для QGroundControl доступна в UI кнопкой «Копировать SDL строку».

---

## 3. Настройка полётного контроллера

### 3.0 Прошивка полётного контроллера

#### Выбор таргета

Правильный таргет — критически важен. Неверный таргет приводит к тому что сенсоры (гироскоп, акселерометр, баро) не определяются и FC неработоспособен.

| Плата | Betaflight | iNav | ArduPilot |
|---|---|---|---|
| Skystars H743 HD | `SKYSTARSH7HD` | `SKYSTARSH743HD` | `SKYSTARSH7HD` |
| Matek H743-WING/SLIM/MINI | `MATEKH743` | `MATEKH743` | `MATEKH743` |
| Pixracer | — | — | `Pixracer` / `Pixracer-bdshot` |

> Skystars H743 и Matek H743 — **разные платы** с разными таргетами, хотя оба на STM32H743. Если прошить Skystars таргетом MATEKH743, сенсоры не определятся (GYRO=UNAVAILABLE, ACC=UNAVAILABLE).

#### Betaflight → iNav (смена прошивки)

1. Открыть iNav Configurator → вкладка **Firmware Flasher**
2. Выбрать правильный таргет (см. таблицу выше)
3. Поставить галочку **Full chip erase** — обязательно при смене прошивки
4. Нажать **Load Firmware [Online]** → **Flash Firmware**

После прошивки проверить сенсоры в CLI:
```
status
```
Ожидаемый результат (пример для Skystars H743):
```
GYRO=BMI270, ACC=BMI270, BARO=BMP280
Sensor status: GYRO=OK, ACC=OK, BARO=OK
```

Если сенсоры показывают `UNAVAILABLE` — скорее всего неверный таргет или в конфиге вручную прописан неверный сенсор:
```
set acc_hardware = AUTO
set baro_hardware = AUTO
set gyro_hardware = AUTO
save
```

#### iNav — проверка конфига после прошивки

Минимальный рабочий конфиг для CRSF через UART4:
```
serial 3 64 115200 115200 0 115200
set receiver_type = SERIAL
set serialrx_provider = CRSF
set serialrx_inverted = OFF
set serialrx_halfduplex = OFF
feature TELEMETRY
map TAER
set small_angle = 180
set min_check = 1000
aux 0 0 0 1800 2100 0 0
aux 1 3 0 900 2100 0 0
calibrate acc
save
```

> Перед `calibrate acc` положите FC ровно на стол. Подробности по каждой команде — в разделе 3.3.

Проверить что всё применилось:
```
serial
feature
get serialrx_provider
status
```

#### RTOS: ChibiOS vs NuttX

ArduPilot начиная с версии 4.0 **полностью перешёл на ChibiOS** и убрал поддержку NuttX. Для всех современных плат (Pixracer, Matek H743, Skystars H743) нужно использовать **ChibiOS** — это единственный доступный вариант в актуальных прошивках ArduPilot.

PX4 по-прежнему использует NuttX как основную RTOS.

---

### 3.1 Подключение проводов (RPi → FC, CRSF)

Нужны **3 провода**: RPi GPIO ↔ UART-пады на полётнике. Оба устройства 3.3В — преобразователь уровней не нужен.

```
RPi Пин 8  (GPIO14, TX)        ──────►  FC UART RX  (например R4)
FC UART TX (например T4)       ──────►  RPi Пин 10 (GPIO15, RX)
RPi Пин 6  (GND)               ──────►  FC GND
```

> TX идёт на RX и наоборот — **перекрёстное** соединение! Провод 5V не нужен.

Нумерация UART на плате: T1/R1 = UART1, T2/R2 = UART2 и т.д. Любой свободный UART подойдёт.

| CLI `serial N` | UART на плате | Пады |
|---|---|---|
| serial 0 | UART1 | T1/R1 (обычно USB) |
| serial 1 | UART2 | T2/R2 |
| serial 2 | UART3 | T3/R3 |
| serial 3 | UART4 | T4/R4 |

### 3.2 Betaflight (CRSF)

Подключить FC по USB, открыть Betaflight Configurator → вкладка **CLI**.

На Linux, если Configurator не видит порт:
```bash
sudo usermod -a -G dialout $USER   # перелогиниться после
```

Пример настройки для UART4 (T4/R4):
```
serial 3 64 115200 57600 0 115200
set serialrx_provider = CRSF
set serialrx_inverted = OFF
feature TELEMETRY
map TAER1234
save
```

- `serial 3 64 ...` — UART4, режим Serial RX (64)
- `feature TELEMETRY` — телеметрия батареи/GPS обратно по UART

ARM-тумблер (AUX1, CH5, диапазон 1800–2100 мкс):
```
aux 0 0 0 1800 2100 0 0
save
```

Рекомендуемый failsafe:
```
set failsafe_procedure = GPS-RESCUE
set failsafe_delay = 15
save
```

### 3.3 iNav (CRSF)

> **Отличия от Betaflight:** iNav требует явной установки `receiver_type = SERIAL` и `serialrx_halfduplex = OFF`. Без этого FC не будет принимать CRSF-фреймы, даже если порт настроен правильно.

#### 1. Порты

**Ports** → на нужном UART (например UART4) включить **Serial RX**. Остальные UART — Serial RX выключен (только один порт!)

#### 2. Приёмник

**Configuration** → Receiver → Receiver type: **Serial**, Serial Receiver Provider: **CRSF**

#### 3. Телеметрия

**Configuration** → Other Features → **Telemetry output**: включить

#### 4. Калибровка акселерометра

**Setup** → положить дрон ровно на стол → нажать **Calibrate Accelerometer**

#### 5. Порог газа

**CLI** → `set min_check = 1000` → `save`

Без этого FC считает что газ не на нуле (CRSF Throttle в покое ≈ 989) и не даёт армиться (флаг `THR`).

#### 6. Угол наклона для арма

**CLI** → `set small_angle = 180` → `save`

По умолчанию iNav блокирует арм если дрон наклонён больше 25°. Значение 180 убирает это ограничение.

#### 7. Режимы (Modes)

**Modes** → добавить:

| Режим | AUX-канал | Диапазон | Зачем |
|---|---|---|---|
| ARM | AUX1 (CH5) | 1800–2100 | Включение моторов |
| ANGLE | AUX1 (CH5) | 900–2100 | Стабилизация (всегда активен). Без него дрон летит в ACRO — опасно через интернет |

#### 8. Save and Reboot

#### Альтернатива — всё через CLI (UART4 = serial 3)

```
serial 3 64 115200 115200 0 115200
set receiver_type = SERIAL
set serialrx_provider = CRSF
set serialrx_inverted = OFF
set serialrx_halfduplex = OFF
set small_angle = 180
set min_check = 1000
feature TELEMETRY
map TAER
aux 0 0 0 1800 2100 0 0
aux 1 3 0 900 2100 0 0
calibrate acc
save
```

> Перед `calibrate acc` положите FC ровно на стол. В iNav скорость CRSF жёстко прошита как 420000 бод — цифры baudrate в команде `serial` игнорируются.

Failsafe (iNav):
```
set failsafe_procedure = DROP
save
```

### 3.4 MAVLink (ArduPilot / PX4)

Трансмиттер автоматически определяет MAVLink FC по USB (`/dev/ttyACM*`) при запуске. Настройка не требуется — FC подключается и работает через QGroundControl (UDP 14550).

#### ArduPilot — первоначальная настройка (Pixracer)

Прошивка: QGroundControl → Vehicle Setup → Firmware → выбрать **ArduPilot**, таргет **Pixracer** (или **Pixracer-bdshot** для двунаправленного DShot).

**Проблема: бесконечный "Initialising ArduPilot"**

Если в QGroundControl видно бесконечный цикл `Info: Initialising ArduPilot` — FC завис на инициализации SD карты. Решение:

1. Вытащить SD карту из FC и перезагрузить — если помогло, проблема в карте
2. Отформатировать SD карту в **FAT32** и вставить обратно
3. Или отключить логгирование через параметр: `LOG_BACKEND_TYPE = 0`

> Форматирование SD карты безопасно — параметры FC хранятся во внутренней памяти, не на карте. Удалятся только логи полётов.

**Отключение кнопки безопасности (safety switch)**

Pixracer требует нажатия физической кнопки безопасности перед армом. Если кнопки нет или она не используется:

QGroundControl → Parameters:
```
BRD_SAFETY_DEFLT = Disabled
```

**Отключение проверок перед армом (только для тестов!)**

```
ARMING_CHECK = 0
```

> Отключает ВСЕ проверки (RC, компас, GPS, акселерометр и т.д.). Использовать только для наземных тестов без пропеллеров.

**Настройка приёмника FlySky FS-iA6B (PPM)**

FS-iA6B по умолчанию работает в режиме PWM (отдельный провод на каждый канал). ArduPilot ожидает PPM или iBUS — один провод, все каналы.

Переключение FS-iA6B в PPM режим: зажать кнопку B/D на приёмнике при включении питания до моргания светодиода.

Параметры в QGroundControl:
```
RC_PROTOCOLS = 1     # PPM
```

Или для iBUS:
```
RC_PROTOCOLS = 256   # iBUS
```

### 3.5 Порядок ARM

1. Отключите USB от полётника
2. Подключите батарею
3. Подождите 5 секунд
4. Газ полностью вниз
5. Щёлкните тумблер арма

> **Снимите пропеллеры перед тестом!**

---

## 4. Запуск и управление

### Ручной запуск (разработка)

**Трансмиттер (RPi):**
```bash
cd /home/rpi/Mavix/src && python3 main.py
```

**Ресивер (PC):**
```bash
cd WebRTC/qgs/src && python3 main.py
```

**Деплой изменений на RPi:**
```bash
scp -r WebRTC/board/src/* rpi@mavixboard.local:/home/rpi/Mavix/src/
```

### Диагностика FC

Проверить подключение полётника, получение пакетов:
```bash
cd /home/rpi/Mavix/src && python3 -m fc
```
Выводит каждую секунду: тип FC, имя, количество пакетов/сек, статус handler.

---

## 5. Техническое описание

### 5.1 Архитектура WebRTC

| Сторона | Библиотека | WebRTC роль | DTLS роль |
|---|---|---|---|
| Трансмиттер (RPi) | GStreamer `webrtcbin` | Offerer | Active (DTLS client) |
| Ресивер (PC) | aiortc | Answerer | Passive (DTLS server) |

> GStreamer всегда хочет быть DTLS-клиентом. Поэтому в answer SDP принудительно заменяется `a=setup:active` → `a=setup:passive`.

**Handshake:**
```
Трансмиттер (GStreamer)         Signal Server           Ресивер (aiortc)
        |                              |                        |
        |── connect (tag=drone) ──────►|                        |
  [pipeline: PLAYING]                 |                        |
  [create-offer]                      |◄── connect (tag=GCS) ──|
        |◄── new_connection ──────────|                        |
        |── offer SDP + ICE ─────────►|── offer SDP + ICE ────►|
        |                             |         [setRemoteDescription]
        |                             |         [createAnswer → patch setup:passive]
        |◄── answer SDP ─────────────|◄── answer SDP ─────────|
  [set-remote-description]            |                        |
        ├──────────────── ICE checks ─────────────────────────┤
        ├──────────────── DTLS handshake ─────────────────────┤
        │  GStreamer ──[H264/SRTP]──────────────────► aiortc  │
        │  aiortc ────[CRSF datachannel]────────────► GStreamer│
        └─────────────────────────────────────────────────────┘
```

**Динамическое управление параметрами:**

| Параметр | Без перезапуска |
|---|---|
| Битрейт | ✓ (`x264enc set_property('bitrate', ...)`) |
| Разрешение / FPS / формат | ✗ (требует перезапуска pipeline) |

При переключении камер неактивным автоматически снижается битрейт до 200 кбит/с — канал не перегружается, поток не прерывается.

### 5.2 CRSF-протокол

CRSF (Crossfire Serial Protocol) — бинарный протокол TBS для RC-команд и телеметрии.
Спецификация: https://github.com/crsf-wg/crsf-spec

**Физический уровень:** 420000 бод (нестандартный, но поддерживается большинством UART-чипов).

**Формат фрейма:**
```
[addr 1B][len 1B][type 1B][payload 0..60B][crc8 1B]
```
- `addr` — адрес получателя: `0xC8` = FC, `0xEE` = TX-модуль, `0xEC` = RX, `0x00` = broadcast
- `len` — байт после поля len: `len(type + payload + crc)`
- `crc8` — полином CRC-8/DVB-S2 (0xD5) по `[type, payload]`

**Типы фреймов:**

| Тип | Константа | Описание |
|---|---|---|
| `0x16` | RC_CHANNELS_PACKED | 16 каналов × 11 бит, 22 байта |
| `0x14` | LINK_STATISTICS | RSSI, Link Quality, SNR |
| `0x28` | DEVICE_PING | Запрос имени устройства |
| `0x29` | DEVICE_INFO | Имя + версия прошивки |
| `0x08` | BATTERY_SENSOR | Напряжение, ток, ёмкость, % заряда |
| `0x02` | GPS | Координаты, высота, спутники |
| `0x1E` | ATTITUDE | Pitch/Roll/Yaw (радианы × 10000) |
| `0x21` | FLIGHT_MODE | ASCII-строка режима полёта |

**RC_CHANNELS_PACKED (0x16):** 16 каналов упакованы в 176 бит = 22 байта (11 бит/канал, little-endian).

**Значения каналов:**

| Константа | Значение | PWM |
|---|---|---|
| CH_MIN | 172 | ≈ 988 мкс |
| CH_CENTER | 992 | ≈ 1500 мкс |
| CH_MAX | 1811 | ≈ 2012 мкс |

**Маппинг каналов (TAER1234):**

| Канал | Функция |
|---|---|
| CH1 | Throttle |
| CH2 | Roll |
| CH3 | Pitch |
| CH4 | Yaw |
| CH5 | ARM (>1800 мкс = armed) |

**CRSFHandler (трансмиттер)** — два потока на `/dev/ttyAMA0` @ 420000 бод:
- **Write thread (50 Гц):** RC-пакеты от datachannel → UART; failsafe CENTER если нет пакетов >0.5 сек; LINK_STATISTICS каждые 0.5 сек
- **Read thread:** телеметрия UART → парсинг фреймов → очередь для datachannel

**Обнаружение FC:** при запуске трансмиттер отправляет DEVICE_PING (0x28), ждёт DEVICE_INFO (0x29). Нет ответа → пробует MAVLink.

### 5.3 MAVLink-протокол

MAVLink — протокол для ArduPilot / PX4. Документация: https://mavlink.io/

| | CRSF | MAVLink |
|---|---|---|
| Интерфейс | UART (420000 бод) | USB (`/dev/ttyACM*`) или UART (115200) |
| Назначение | RC-каналы + лёгкая телеметрия | Полный контроль: параметры, команды, waypoints |
| FC | Betaflight / iNav | ArduPilot / PX4 |

**Схема при MAVLink FC:**
```
Ресивер ──[CRSF datachannel]──► Трансмиттер ──[UDP 14550]──► QGroundControl ──[MAVLink]──► FC
```

**Фильтрация пакетов** — пропускаются только важные сообщения (снижает трафик ~10×):
```
IMPORTANT_MSGS = {0, 1, 22, 77, 253, 33, 74, 147}
# HEARTBEAT, SYS_STATUS, PARAM_VALUE, COMMAND_ACK,
# STATUSTEXT, GLOBAL_POSITION_INT, VFR_HUD, BATTERY_STATUS
```
Остальные — каждый 20-й пакет.

**Идентификация FC по HEARTBEAT:**
```python
MAV_AUTOPILOT = {0: 'Generic', 3: 'ArduPilot', 12: 'PX4'}
```

---

## 6. Устранение неполадок

### RXLOSS — FC не получает сигнал

1. Проверьте провода: RPi TX → FC RX (не наоборот!)
2. Проверьте `dtoverlay=disable-bt` в `/boot/firmware/config.txt`
3. Убедитесь что serial console отключена (raspi-config → Serial → Login shell: No)

### FC не армируется

В CLI любого FC: `status` → строка `Arming disabled flags:` (или `Arming disable flags:`)

#### Betaflight

| Флаг | Причина | Решение |
|---|---|---|
| MSP | USB подключён | Отключите USB, питайте от батареи |
| RXLOSS | Нет RC-сигнала | см. выше |
| THROTTLE | Газ не на нуле | Стик газа полностью вниз |
| ANGLE | Дрон не ровно | `set small_angle = 180` в CLI |
| ARM_SWITCH | Тумблер арма включён при старте | Выключить, потом включить |

#### iNav

| Флаг | Причина | Решение |
|---|---|---|
| CLI | Открыт режим CLI | Написать `exit` |
| CAL | Акселерометр не откалиброван | `calibrate acc` → `save` (FC ровно на столе) |
| ACC | Акселерометр не прошёл проверку | Калибровка + `set small_angle = 180` |
| THR | Газ выше порога min_check | `set min_check = 1000` → `save`, стик газа полностью вниз |
| RXLOSS | Нет RC-сигнала | Проверить UART, провода, `receiver_type = SERIAL` |
| NAV UNSAFE | Нет GPS/компаса для навигации | Отключить навигационные режимы или подключить GPS |
| HW | Проблема с сенсорами | `status` → проверить Sensor status, `set acc_hardware = AUTO` |

> Полный одноразовый фикс для первого арма в iNav: `set small_angle = 180` → `set min_check = 1000` → `calibrate acc` → `save` → `exit`

### WebRTC не подключается

| Симптом | Решение |
|---|---|
| `Fatal SSL error` в GStreamer | Проверить патч `a=setup:passive` в answer SDP |
| ICE gathering зависает | Проверить STUN-сервер |
| ICE completed, DTLS stuck | Убедиться что `GLib.MainLoop` запущен в треде |
| Нет offer от трансмиттера | Проверить что `latency=0` ставится на webrtcbin до `set_state(PLAYING)` |

### Betaflight / iNav Configurator не подключается

1. Остановить трансмиттер
2. Отключить батарею
3. Переподключить USB, подождать 5 секунд

### ArduPilot — FC не армируется

| Сообщение | Причина | Решение |
|---|---|---|
| `PreArm: Hardware safety switch` | Не нажата кнопка безопасности | Нажать кнопку или `BRD_SAFETYENABLE = 0` |
| `Arm: System not initialised` | FC ещё загружается | Подождать 15–30 секунд после включения |
| `PreArm: RC not calibrated` | Не откалиброван пульт | QGC → Radio Calibration |
| `PreArm: Compass not calibrated` | Не откалиброван компас | QGC → Compass Calibration |

### ArduPilot — бесконечный "Initialising ArduPilot"

FC завис на инициализации SD карты:
1. Вытащить SD карту и перезагрузить FC
2. Если помогло — отформатировать карту в **FAT32**
3. Или отключить логгирование: параметр `LOG_BACKEND_TYPE = 0`

---

## 7. Заметки

### Стиль кода

1. Комментарии только там, где без них совсем непонятно (на английском)
2. Private-методы — двойное подчёркивание `__`
3. Отступы по PEP-8: между логическими блоками модуля — два пустых, между методами класса — один, внутри функции — без дополнительных
4. Простота важнее архитектуры, никакого overhead без необходимости

### TODO