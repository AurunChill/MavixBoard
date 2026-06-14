# MavixBoard — Руководство оператора

Документ составлен в соответствии с ГОСТ 19.505-79 «Руководство
оператора. Требования к содержанию и оформлению».

---

## 1. Аннотация

Документ предназначен для оператора, осуществляющего установку и
эксплуатацию бортового программного обеспечения «MavixBoard» на дроне.
В качестве оператора, как правило, выступает пилот или техник,
готовящий БПЛА к полётам.

---

## 2. Назначение программы

«MavixBoard» — бортовое программное обеспечение, выполняющееся на
бортовом компьютере дрона (Raspberry Pi). Программа выполняет следующие
функции:

- получение видеоизображения с подключённых камер;
- передачу видеоизображения и команд управления через интернет на
  наземную станцию пилота;
- обмен данными с полётным контроллером (телеметрия, команды управления).

Программа работает в фоновом режиме как системная служба, без участия
оператора в процессе полёта.

---

## 3. Условия выполнения программы

### 3.1. Минимальный состав технических средств

- бортовой компьютер: Raspberry Pi 4 (4 ГБ ОЗУ) или аналогичный;
- USB-камера (одна или несколько; CSI-камеры не поддерживаются);
- полётный контроллер с поддержкой протокола MAVLink или CRSF;
- модуль связи с интернетом (LTE-модем, Wi-Fi);
- источник питания, обеспечивающий не менее 3 А при 5 В.

### 3.2. Минимальный состав программных средств

- операционная система: Raspberry Pi OS Bookworm или Ubuntu Server
  24.04;
- наличие активного интернет-соединения.

### 3.3. Подготовка к работе

Перед установкой убедиться, что:

1. Операционная система установлена и обновлена.
2. Имя дрона зарегистрировано в системе «Mavix» и получен установочный
   пакет (см. раздел 4.1).
3. Камера обнаруживается командой `v4l2-ctl --list-devices`.
4. Полётный контроллер подключён к одному из портов: `/dev/ttyACM0`,
   `/dev/ttyACM1`, `/dev/ttyUSB0`, `/dev/ttyUSB1`, `/dev/ttyAMA0`,
   `/dev/ttyAMA1`.

---

## 4. Выполнение программы

### 4.1. Получение установочного пакета

Архив `mavixboard-<id>.tar.gz` выдаётся сервером «MavixServer» по
запросу пилота, авторизованного в системе. Процедура получения описана
в руководстве оператора сервера. Краткая последовательность:

1. Пилот регистрируется в системе через наземную станцию «MavixDesktop».
2. Пилот регистрирует свой дрон, получая `drone_id`.
3. По адресу
   `https://<сервер>/api/v1/builds/board?drone_id=<идентификатор>`
   скачивается готовый установочный архив.

В пакете уже содержится привязка к учётной записи пилота — никаких
дополнительных настроек на дроне не требуется.

### 4.2. Установка программы

Архив `mavixboard-<id>.tar.gz` копируется на бортовой компьютер
(например, через `scp`), после чего выполняется:

```bash
tar -xzf mavixboard-<id>.tar.gz
cd mavixboard-<id>
sudo ./install.sh
sudo systemctl enable --now mavixboard
```

В процессе установки автоматически:

- устанавливаются системные зависимости (GStreamer, Python и т. д.);
- устанавливаются зависимости Python из локальных wheel-файлов
  (без обращения в интернет);
- регистрируется системная служба `mavixboard.service`.

### 4.3. Проверка работы

Состояние службы:

```bash
sudo systemctl status mavixboard
```

Журнал работы:

```bash
journalctl -u mavixboard -f
```

После успешного запуска в журнале наблюдаются записи:

```
[signal] connecting to ws://<server>/ws/drone ...
[signal] connected
```

Когда пилот выберет данный дрон в наземной станции, в журнале
появится:

```
[coord] connecting to GCS <id>
[manager] starting session with gcs=<id>
```

### 4.4. Остановка программы

```bash
sudo systemctl stop mavixboard
```

Чтобы отключить автоматический запуск при загрузке:

```bash
sudo systemctl disable mavixboard
```

### 4.5. Удаление программы

```bash
sudo systemctl disable --now mavixboard
sudo rm -rf /opt/mavixboard /etc/systemd/system/mavixboard.service
sudo systemctl daemon-reload
# Для полного удаления конфигурации:
sudo rm -rf /etc/mavixboard
```

### 4.6. Обновление программы

При выходе новой версии повторить процедуры п. 4.1–4.2: получить
новый архив, распаковать и запустить `install.sh` поверх существующего.
Служба будет перезапущена автоматически.

---

## 5. Сообщения оператору

### 5.1. Сообщения штатного функционирования

| Сообщение в журнале | Значение |
|---|---|
| `signal: connected` | Установлена связь с сервером сигнализации |
| `[manager] starting session with gcs=<id>` | Пилот подключился к дрону |
| `[fc-service] FC connected: mavlink — ardupilot` | Обнаружен полётный контроллер ArduPilot |
| `[fc-service] FC connected: crsf — TBS Crossfire` | Обнаружен полётный контроллер с CRSF |

### 5.2. Сообщения о нештатных ситуациях

| Сообщение | Причина | Действия оператора |
|---|---|---|
| `cameras not found` | Камера не обнаружена или нет прав доступа | Проверить кабель, исполнить `v4l2-ctl --list-devices`; добавить пользователя в группу `video` |
| `[signal] connect error: Name or service not known` | Сервер недоступен / нет интернета | Проверить интернет-соединение, доступность сервера, корректность `SIGNAL_SERVER_IP` в `/etc/mavixboard/preset.env` |
| `[signal] connect error: 401` | Просроченный или неверный `drone_token` | Получить новый установочный пакет в системе «Mavix» и переустановить |
| `[coord] pipeline error` | Сбой видеоконвейера | Перезапустить службу: `sudo systemctl restart mavixboard`; при повторе — проверить камеру другой утилитой, например `cheese` |
| `[fc-service] FC not found, retrying` | Полётный контроллер не отвечает | Проверить подключение по UART, корректность скорости передачи (для MAVLink — 115200) |
| `[enroll] нет ADMIN_ID/ENROLLMENT_TOKEN для саморегистрации` | В `preset.env`/`.env` нет данных владельца | Установить пакет, собранный в системе «Mavix» (`ADMIN_ID`/`ENROLLMENT_TOKEN` зашиваются автоматически) |
| `enroll отклонён сервером: 401` | Неверный `ENROLLMENT_TOKEN` | Скачать актуальный установочный пакет в системе «Mavix» и переустановить |
| `Reboot requested by receiver` | Пилот отправил команду перезагрузки | Программа перезапускается автоматически. Действия не требуются |

### 5.3. Где смотреть журнал в случае сбоя

| Источник | Команда |
|---|---|
| systemd journal | `journalctl -u mavixboard -n 200` |
| Файл программы | `cat /var/log/mavixboard/mavixboard_<дата>.log` |

При обращении в техническую поддержку приложить последние 200 строк
журнала.

---

## 6. Подготовка Raspberry Pi к работе

Настоящий раздел применим только при первичной подготовке нового
бортового компьютера. После завершения процедур одного раздела
необходимо переходить к следующему.

### 6.1. Установка операционной системы

Образ записывается на microSD-карту средствами «Raspberry Pi Imager».
При записи в дополнительных параметрах допустимо сразу задать имя
пользователя, пароль и параметры Wi-Fi.

Рекомендуемые версии: «Raspberry Pi OS» (Bookworm, 64-bit) или
«Ubuntu Server 24.04 LTS».

После первого запуска обновить систему:

```bash
sudo apt update && sudo apt upgrade -y
```

### 6.2. Настройка сети

#### 6.2.1. Wi-Fi через netplan

Файл `/etc/netplan/50-cloud-init.yaml`:

```yaml
network:
  version: 2
  renderer: networkd
  wifis:
    wlan0:
      dhcp4: true
      access-points:
        "SSID_СЕТИ":
          password: "ПАРОЛЬ"
```

Применить:

```bash
sudo netplan apply
```

#### 6.2.2. NetworkManager (рекомендуется при наличии нескольких сетей)

```bash
sudo apt install -y network-manager
sudo systemctl disable systemd-networkd
sudo systemctl enable --now NetworkManager
```

В `/etc/netplan/50-cloud-init.yaml` оставить:

```yaml
network:
  version: 2
  renderer: NetworkManager
```

Управление сетями — командой `sudo nmtui` (интерактивный интерфейс).

#### 6.2.3. mDNS — обращение по имени

```bash
sudo apt install -y avahi-daemon
sudo hostnamectl set-hostname mavixboard
sudo systemctl enable --now avahi-daemon
```

После этого RPi доступен с других машин в локальной сети по имени
`mavixboard.local`.

#### 6.2.4. Wi-Fi-точка доступа (необязательно)

Создать `~/hotspot.sh`:

```bash
#!/bin/bash
nmcli dev wifi hotspot ifname wlan0 con-name RPI4WIFI \
    ssid RPI4WIFI password Password1234
```

```bash
chmod +x ~/hotspot.sh
```

Файл `/etc/systemd/system/hotspot.service`:

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

#### 6.2.5. LTE-модем

Для LTE-модулей семейства Waveshare SIM7600/SIM8202 и аналогов:

1. Вставить SIM-карту **до** включения питания.
2. Проверить, что модем определился:

```bash
lsusb              # должен появиться Qualcomm / SIMCOM
ls /dev/ttyUSB*    # ttyUSB0..ttyUSB2
ip link show       # интерфейс usb0 / wwan0 / eth1
```

3. Установить ModemManager:

```bash
sudo apt install -y modemmanager
sudo systemctl enable --now ModemManager
mmcli -L                                  # модем найден
```

4. Создать GSM-соединение, указать APN оператора:

```bash
sudo nmcli connection add type gsm ifname '*' con-name lte apn internet
sudo nmcli connection modify lte connection.autoconnect yes
sudo nmcli connection up lte
```

APN операторов:

| Оператор | APN |
|---|---|
| Tele2 | `internet.tele2.ru` |
| МТС | `internet.mts.ru` |
| Билайн | `internet.beeline.ru` |
| Мегафон | `internet` |

5. При одновременном Wi-Fi и LTE задать приоритет LTE:

```bash
sudo nmcli connection modify lte ipv4.route-metric 50
sudo nmcli connection up lte
```

Диагностика:

```bash
mmcli -m 0                       # уровень сигнала, оператор
journalctl -u ModemManager -f
```

### 6.3. Подключение USB-камеры

Поддерживаются только USB-камеры (UVC). CSI-камеры (через `libcamera`/
`rpicam`) в текущей версии не поддерживаются.

```bash
lsusb                       # камера видна на USB
ls -l /dev/video*           # определить порт
```

Контрольный снимок:

```bash
sudo apt install -y fswebcam
fswebcam -d /dev/video0 ~/photo.jpg
```

### 6.4. Настройка UART для CRSF-полётников

Применимо только при использовании полётных контроллеров с протоколом
CRSF (Betaflight, iNav). Для MAVLink-контроллеров (ArduPilot, PX4),
подключаемых по USB, настройка UART не требуется.

#### 6.4.1. Конфигурация загрузчика

Добавить в файл `/boot/firmware/config.txt`:

```
enable_uart=1
dtoverlay=disable-bt
```

> Параметр `dtoverlay=disable-bt` обязателен — он отключает Bluetooth
> и переключает выводы GPIO14/15 с маломощного `mini-UART` на
> полноценный `PL011 UART` (`ttyAMA0`), способный передавать данные
> на скорости 420 000 бод, требуемых для CRSF.

#### 6.4.2. Отключение serial console

Командой:

```bash
sudo raspi-config
```

→ «Interface Options» → «Serial Port» → Login shell over serial: **No**;
Hardware enabled: **Yes**.

Альтернативно — вручную удалить `console=serial0,115200` из файла
`/boot/firmware/cmdline.txt`.

#### 6.4.3. Права доступа к serial-портам

```bash
sudo usermod -aG dialout rpi
sudo usermod -aG tty rpi
```

Постоянное правило `udev` — `/etc/udev/rules.d/99-serial.rules`:

```
KERNEL=="ttyAMA[0-9]*", GROUP="dialout", MODE="0660"
KERNEL=="ttyUSB[0-9]*", GROUP="dialout", MODE="0660"
KERNEL=="ttyACM[0-9]*", GROUP="dialout", MODE="0660"
```

Применить:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

#### 6.4.4. Освобождение порта `ttyAMA0` от systemd-getty

```bash
sudo systemctl stop serial-getty@ttyAMA0.service
sudo systemctl disable serial-getty@ttyAMA0.service
```

#### 6.4.5. Перезагрузка

После выполнения всех действий — обязательная перезагрузка:

```bash
sudo reboot
```

---

## 7. Настройка полётного контроллера

Содержание раздела применимо к подготовке нового полётного контроллера.
Если контроллер уже настроен и работает с системой «Mavix», раздел
можно пропустить.

### 7.1. Подключение проводов между Raspberry Pi и FC

Применимо только для CRSF-контроллеров. Для MAVLink — подключение
происходит по USB, провода UART не нужны.

Требуется **три провода** между GPIO Raspberry Pi и UART-падами на
полётном контроллере. Оба устройства работают на уровне 3,3 В —
преобразователь уровней не нужен.

```
Контакт RPi             Соединение           Контакт FC
─────────────           ──────────           ──────────
GPIO 14 (контакт 8)  ─── TX ────────►  RX UART-N (например R4)
GPIO 15 (контакт 10) ◄── RX ────────  TX UART-N (например T4)
GND     (контакт 6)  ─── GND ───────  GND
```

> Соединение TX↔RX **перекрёстное**: вывод TX одной стороны идёт на
> RX другой. Провод питания 5 В не требуется.

Соответствие номеров UART в CLI полётника:

| CLI `serial N` | UART на плате | Пады |
|---|---|---|
| `serial 0` | UART1 | T1 / R1 (часто USB) |
| `serial 1` | UART2 | T2 / R2 |
| `serial 2` | UART3 | T3 / R3 |
| `serial 3` | UART4 | T4 / R4 |

### 7.2. Выбор прошивки

Перед прошивкой убедиться в правильности целевой платформы (`target`).
Неверный таргет — самая частая причина того, что после прошивки
сенсоры (гироскоп, акселерометр, барометр) показывают `UNAVAILABLE`.

| Плата | Betaflight | iNav | ArduPilot |
|---|---|---|---|
| Skystars H743 HD | `SKYSTARSH7HD` | `SKYSTARSH743HD` | `SKYSTARSH7HD` |
| Matek H743 (WING/SLIM/MINI) | `MATEKH743` | `MATEKH743` | `MATEKH743` |
| Pixracer | — | — | `Pixracer` / `Pixracer-bdshot` |

> Skystars H743 и Matek H743 — **разные платы**, хотя обе построены
> на микроконтроллере STM32H743. Если прошить Skystars таргетом
> `MATEKH743` или наоборот, сенсоры не определятся.

### 7.3. Настройка Betaflight (CRSF)

Подключить полётный контроллер по USB, открыть Betaflight Configurator,
перейти на вкладку «CLI». Пример конфигурации для UART4
(`serial 3`):

```
serial 3 64 115200 57600 0 115200
set serialrx_provider = CRSF
set serialrx_inverted = OFF
feature TELEMETRY
map TAER1234
save
```

ARM-тумблер (AUX1, канал CH5, диапазон 1800–2100 мкс):

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

### 7.4. Настройка iNav (CRSF)

iNav, в отличие от Betaflight, требует явной установки
`receiver_type = SERIAL` и `serialrx_halfduplex = OFF`. Без этих
параметров приёмник не примет CRSF-кадры, даже если порт настроен.

#### 7.4.1. Настройка через графический интерфейс

1. **Ports**: на нужном UART (например UART4) включить «Serial RX».
   На остальных портах «Serial RX» оставить выключенным.
2. **Configuration → Receiver**: тип приёмника — «Serial»; провайдер —
   «CRSF».
3. **Configuration → Other Features**: включить «Telemetry output».
4. **Setup**: установить полётник ровно на столе, нажать «Calibrate
   Accelerometer».
5. **CLI**:

   ```
   set min_check = 1000
   set small_angle = 180
   save
   ```

   `min_check = 1000` исключает срабатывание защиты «throttle not
   zero» при значении газа 989 мкс, которое CRSF выдаёт в состоянии
   покоя стика. `small_angle = 180` снимает блокировку арма по углу
   наклона.

6. **Modes**: добавить режимы ARM (AUX1, диапазон 1800–2100) и ANGLE
   (AUX1, диапазон 900–2100, активен всегда — необходимо для
   стабилизации при управлении через интернет).
7. **Save and Reboot**.

#### 7.4.2. Альтернативная настройка через CLI

Полный одноразовый ввод (для UART4):

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

> Команду `calibrate acc` выполнять, когда полётник лежит горизонтально.

Failsafe:

```
set failsafe_procedure = DROP
save
```

#### 7.4.3. Проверка применённых параметров

```
serial
feature
get serialrx_provider
status
```

Признаком успешной настройки является отсутствие в строке
`Arming disabled flags` следующих флагов: `RXLOSS`, `THR`, `ANGLE`,
`CAL`. Если присутствует флаг `CLI` — выйти из режима командой
`exit`.

### 7.5. Настройка ArduPilot / PX4 (MAVLink)

Контроллеры с MAVLink подключаются к Raspberry Pi напрямую по USB —
обнаруживаются как `/dev/ttyACM0`. Настройки на стороне «MavixBoard»
не требуются: программа автоматически определит контроллер и направит
трафик на наземную станцию.

#### 7.5.1. Первоначальная прошивка ArduPilot

В QGroundControl открыть «Vehicle Setup → Firmware» → выбрать
«ArduPilot», таргет «Pixracer» (или «Pixracer-bdshot» для двустороннего
DShot).

#### 7.5.2. Бесконечная инициализация ArduPilot

Если в QGroundControl многократно появляется сообщение
`Initialising ArduPilot`, полётный контроллер завис на инициализации
SD-карты. Возможные действия:

1. Извлечь SD-карту, перезагрузить контроллер. Если запуск прошёл —
   проблема в карте: отформатировать её в FAT32 и установить обратно.
2. Отключить запись логов параметром `LOG_BACKEND_TYPE = 0`.

> Форматирование SD-карты безопасно — параметры контроллера хранятся
> во внутренней памяти, не на карте; удаляются только логи прошлых
> полётов.

#### 7.5.3. Кнопка безопасности

Контроллеры Pixracer и аналогичные по умолчанию требуют нажатия
физической кнопки безопасности перед армом. При её отсутствии:

```
BRD_SAFETY_DEFLT = Disabled
```

#### 7.5.4. Отключение проверок перед армом (только для наземных тестов)

```
ARMING_CHECK = 0
```

> Отключает все проверки (RC, компас, GPS, акселерометр и т. д.).
> Допустимо использовать только при наземных тестах с **снятыми
> пропеллерами**.

#### 7.5.5. Настройка приёмника FlySky FS-iA6B

По умолчанию FS-iA6B работает в режиме PWM (отдельный провод на
канал). ArduPilot ожидает PPM или iBUS (один провод):

- переключить FS-iA6B в режим PPM: при включении приёмника зажать
  кнопку B/D до моргания индикатора;
- в параметрах QGroundControl установить `RC_PROTOCOLS = 1` (PPM) или
  `RC_PROTOCOLS = 256` (iBUS).

---

## 8. Порядок включения дрона перед полётом

> **Перед всеми тестами в помещении обязательно снять пропеллеры.**

1. Отсоединить USB-кабель от полётного контроллера (если был
   подключён).
2. Подключить аккумулятор к дрону.
3. Подождать не менее 5 секунд для инициализации полётного
   контроллера и обнаружения его системой «MavixBoard».
4. Убедиться, что стик газа на пульте (или на джойстике в «MavixDesktop»)
   находится в крайнем нижнем положении.
5. Перевести тумблер «ARM» в положение «включено».

При успешном арме индикаторы полётного контроллера сменят режим
(двойное моргание / красный → синий, в зависимости от прошивки),
двигатели тихо запоют.

---

## 9. Устранение неполадок

### 9.1. Полётный контроллер не армируется

Подключить полётный контроллер по USB к компьютеру, открыть
конфигуратор (Betaflight / iNav Configurator), перейти в CLI и
выполнить команду `status`. В строке `Arming disabled flags`
перечислены причины:

#### Betaflight

| Флаг | Причина | Действие |
|---|---|---|
| `MSP` | Подключён USB | Отключить USB, питать только от аккумулятора |
| `RXLOSS` | Нет RC-сигнала | Проверить раздел 9.2 |
| `THROTTLE` | Газ не на нуле | Опустить стик газа полностью |
| `ANGLE` | Большой наклон | `set small_angle = 180` |
| `ARM_SWITCH` | Тумблер ARM включён при старте | Выключить, затем включить |

#### iNav

| Флаг | Причина | Действие |
|---|---|---|
| `CLI` | Открыт режим CLI | Ввести `exit` |
| `CAL` | Не откалиброван акселерометр | `calibrate acc` → `save` (FC ровно на столе) |
| `ACC` | Акселерометр не прошёл проверку | Калибровка + `set small_angle = 180` |
| `THR` | Газ выше порога | `set min_check = 1000`, опустить стик |
| `RXLOSS` | Нет RC-сигнала | См. 9.2 |
| `NAV UNSAFE` | Нет GPS/компаса | Отключить нав-режимы или подключить GPS |
| `HW` | Проблема с сенсорами | `status`; `set acc_hardware = AUTO`; убедиться в правильности таргета |

Универсальная последовательность для первого арма в iNav:

```
set small_angle = 180
set min_check = 1000
calibrate acc
save
exit
```

#### ArduPilot

| Сообщение | Причина | Действие |
|---|---|---|
| `PreArm: Hardware safety switch` | Не нажата кнопка безопасности | Нажать кнопку или `BRD_SAFETY_DEFLT = 0` |
| `Arm: System not initialised` | Контроллер ещё загружается | Подождать 15–30 секунд после подачи питания |
| `PreArm: RC not calibrated` | Не откалиброван пульт | QGroundControl → Radio Calibration |
| `PreArm: Compass not calibrated` | Не откалиброван компас | QGroundControl → Compass Calibration |

### 9.2. `RXLOSS` — полётный контроллер не получает RC-сигнал

1. Проверить перекрёстность проводов: TX Raspberry Pi → RX FC,
   а не TX → TX.
2. Убедиться, что в `/boot/firmware/config.txt` присутствует строка
   `dtoverlay=disable-bt`.
3. Убедиться, что serial console отключена (см. 6.4.2).
4. В CLI полётника убедиться, что `serialrx_provider = CRSF` и
   на выбранном UART включён `Serial RX`.

### 9.3. После прошивки сенсоры показывают `UNAVAILABLE`

Прошит неверный таргет. Перепрошить, выбрав правильный (см. 7.2),
обязательно отметив «Full chip erase». Если таргет правильный,
выполнить:

```
set acc_hardware = AUTO
set baro_hardware = AUTO
set gyro_hardware = AUTO
save
```

### 9.4. WebRTC-сессия не устанавливается

| Симптом в журнале | Действие |
|---|---|
| `[signal] connect error: …` | См. раздел 5.2 |
| `[coord] pipeline error` | См. раздел 5.2 |
| ICE-кандидаты не обмениваются | Проверить, что в `/etc/mavixboard/preset.env` указан корректный `SIGNAL_SERVER_IP` и доступен STUN-сервер |

### 9.5. Конфигуратор Betaflight / iNav не подключается к FC

1. Остановить службу: `sudo systemctl stop mavixboard`.
2. Отсоединить и снова подключить кабель USB полётного контроллера.
3. Подождать 5 секунд, открыть конфигуратор.
4. После завершения настройки FC — `sudo systemctl start mavixboard`.


### Запуск тест в симуляции:

**Mavlink**
Необходимо запустить в gazebo PX4-sitl симуляцию

```MAVLINK_URL=udpin:127.0.0.1:14540 python -m mavixboard```