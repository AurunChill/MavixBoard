# MavixBoard — Техническое описание

Документ составлен в соответствии с ГОСТ 19.402-78 «Описание программы» и
ГОСТ 19.503-79 «Руководство системного программиста».

---

## 1. Аннотация

Документ содержит техническое описание бортового программного обеспечения
«MavixBoard» — компонента системы дистанционного управления БПЛА «Mavix»,
устанавливаемого на бортовом компьютере (как правило — Raspberry Pi 4).
Предназначен для разработчиков и инженеров, осуществляющих сборку,
установку, настройку и сопровождение программы.

---

## 2. Общие сведения

### 2.1. Наименование

Полное наименование: «Бортовое ПО Mavix» (MavixBoard).
Условное обозначение: `mavixboard`.
Версия: 0.1.0.

### 2.2. Программное обеспечение

| Компонент | Версия |
|---|---|
| Язык программирования | Python ≥ 3.11 |
| Среда выполнения | GNU/Linux (Raspberry Pi OS / Ubuntu 24.04) |
| GStreamer + GstWebRTC | 1.22+ |
| PyGObject | 3.44+ |
| aiohttp | 3.9+ |
| websockets | 12.0+ |
| pyserial / pyserial-asyncio | 3.5+ / 0.6+ |
| pymavlink | 2.4+ |
| ASGI / асинхронность | asyncio |

### 2.3. Назначение

Программа обеспечивает:

- захват видео с подключённых USB- или CSI-камер;
- кодирование видеопотоков в формат H.264;
- передачу видеопотоков по технологии WebRTC наземной станции;
- двунаправленный обмен пакетами с полётным контроллером по
  протоколам MAVLink или CRSF;
- автоматическое подключение к серверу сигнализации и поддержание
  соединения с экспоненциальной задержкой при разрывах.

---

## 3. Функциональное назначение

### 3.1. Класс решаемых задач

Программа реализует роль предложителя (offerer) в WebRTC-сессии:
формирует медиа-канал и три data-канала (`packet-channel`, `ping-channel`,
`config-channel`), направляет SDP-оффер наземной станции через сервер
сигнализации, ожидает ответ.

### 3.2. Сведения о функциональных ограничениях

- Одновременно поддерживается одна активная сессия с одной наземной
  станцией.
- Программа предполагает наличие сетевого подключения к серверу
  сигнализации.
- При обнаружении ошибки GStreamer-конвейера сессия принудительно
  завершается; повторное подключение инициируется наземной станцией.

---

## 4. Описание логической структуры

### 4.1. Структура каталогов

```
src/mavixboard/
├── core/
│   ├── config.py        # Загрузка преcет‑окружения, параметры
│   ├── logger.py        # Журналирование (консоль + файл)
│   └── backoff.py       # Экспоненциальная задержка переподключения
├── server/
│   ├── api.py           # HTTP-клиент: /health, /drones/register
│   └── signal_client.py # WebSocket-клиент к серверу сигнализации
├── webrtc/
│   ├── peer.py          # PeerSession — управление одной WebRTC-сессией
│   ├── manager.py       # WebRTCManager — оркестрация PeerSession и каналов
│   └── channels.py      # PacketChannel, PingChannel, ConfigChannel
├── gstreamer/
│   ├── pipeline.py      # Построение GStreamer-конвейера
│   ├── gstreamer.py     # GStreamerPipe — обёртка над gst_parse_launch
│   ├── camera.py        # Перечисление и описание камер
│   └── watcher.py       # Слежение за подключёнными камерами
├── fc/
│   ├── crsf.py          # Реализация протокола CRSF
│   ├── mavlink.py       # Парсинг сообщений MAVLink
│   ├── controllers.py   # Контроллеры FC (Mavlink, CRSF)
│   ├── detect.py        # Автоопределение типа FC
│   └── service.py       # FCService — единая точка управления FC
├── token/
│   ├── generator.py     # Генерация криптографических токенов
│   └── storage.py       # Хранение токена в файле
├── coordinator.py       # SessionCoordinator — связь signaling/webrtc/fc
└── __main__.py          # Точка входа
```

### 4.2. Описание основных модулей

#### 4.2.1. Модуль конфигурации (`core/config.py`)

Загружает значения из:

1. `/etc/mavixboard/preset.env` — конфигурация, «впаянная» при сборке
   `.deb`-пакета (содержит `USER_ID`, `DRONE_TOKEN`, `SIGNAL_SERVER_IP`).
   Загружается без переопределения уже установленных переменных.
2. Локального `.env` в корне проекта — для разработки.
   Загружается с переопределением.

Доступ к параметрам — через объект `settings`.

#### 4.2.2. Модуль координатора (`coordinator.py`)

Реализует основной цикл работы:

1. Соединение с сервером сигнализации с экспоненциальной задержкой
   (`ExponentialBackoff`: 1, 2, 4, 8, 16, 30 секунд).
2. Прослушивание сообщений и их диспетчеризация по типу.
3. При получении `connect` от сервера: создание `GStreamerPipe`,
   `WebRTCManager`, запуск негоциации.
4. При ошибке конвейера или потере соединения — `teardown` сессии,
   отправка `disconnect_session` и `error` через `config-channel`.

#### 4.2.3. Модуль WebRTC-сессии (`webrtc/`)

`PeerSession` оборачивает GStreamer-элемент `webrtcbin`:

- регистрирует обработчики сигналов `on-negotiation-needed`,
  `on-ice-candidate`;
- асинхронно создаёт SDP-оффер;
- применяет SDP-ответ;
- передаёт ICE-кандидаты в очередь, откуда они забираются
  «насосами» (pump_ice, pump_offer) и отправляются в сигнализацию.

`WebRTCManager` управляет одной активной сессией. Создаёт
`DataChannelHub`, привязывает FC-канал к `FCService`, отправляет
информацию о FC и список камер в `config-channel` при его открытии.

#### 4.2.4. Модуль связи с полётным контроллером (`fc/`)

`FCService.start()` запускает асинхронный цикл поиска FC по перечню
последовательных портов. Применяются последовательно:

1. `MavlinkController` — попытка получить heartbeat по протоколу
   MAVLink (через `pymavlink`).
2. `CrsfController` — попытка получить кадр CRSF.

При успехе сервис устанавливает обнаруженный `FlightController` и
начинает приём/передачу пакетов. При обрыве — повторно сканирует
порты.

Передача пакетов:

- от FC к наземной станции: `FCService.on_packet_to_gcs` →
  `PacketChannel.send_bytes`;
- от наземной станции к FC: `PacketChannel.on_packet` →
  `FCService.send`.

#### 4.2.5. Модуль GStreamer-конвейера (`gstreamer/`)

`PipelineBuilder` формирует строку конвейера вида:

```
webrtcbin name=webrtc bundle-policy=max-bundle stun-server=<...> [turn-server=<...>]
  v4l2src device=/dev/video0 ! ... ! x264enc ! rtph264pay ! webrtc.sink_0
  [v4l2src device=/dev/video1 ! ...]
```

`CameraManager.get_cameras()` возвращает список доступных камер с
их параметрами; `CameraWatcher` периодически сканирует устройства
и при изменении уведомляет координатор.

### 4.3. Протокол data-каналов

Программа открывает три канала:

| Канал | Параметры | Назначение |
|---|---|---|
| `packet-channel` | `ordered=true`, `max-retransmits=2`, `bitrate=6000000` | Передача пакетов FC |
| `ping-channel` | `ordered=true`, `max-retransmits=2` | Эхо-ответы для измерения задержки |
| `config-channel` | `ordered=true` | Управляющие JSON-сообщения |

В `config-channel` циркулируют сообщения:

| Тип | Направление | Содержимое |
|---|---|---|
| `fc` | дрон → GCS | `kind`, `name` |
| `cameras` | дрон → GCS | список объектов с `device_index`, `name`, `bitrate_kbs`, `params`, `param_index` |
| `cameras_changed` | дрон → GCS | `device_indices` |
| `error` | дрон → GCS | `message` |
| `bitrate` | GCS → дрон | `updates: [{device_index, bitrate_kbs}]` |
| `reboot` | GCS → дрон | — |

---

## 5. Используемые технические средства

### 5.1. Минимальные требования

| Параметр | Значение |
|---|---|
| Бортовой компьютер | Raspberry Pi 4 (4 ГБ) или сопоставимый ARM/x86_64 |
| ОЗУ | 1 ГБ свободной |
| ОС | Raspberry Pi OS (Bookworm) или Ubuntu 24.04 |
| Камера | USB-вебкамера с поддержкой H.264 или YUYV/MJPEG ≥ 320×240 |
| Полётный контроллер | ArduPilot, PX4 (MAVLink) или TBS/RX (CRSF) |
| Соединение | UART через `/dev/ttyACM*`, `/dev/ttyUSB*`, `/dev/ttyAMA*` |
| Сеть | Wi-Fi, LTE-модем или Ethernet |

### 5.2. Зависимости системного уровня

```
python3 ≥ 3.11, python3-venv, python3-pip
python3-gi
gir1.2-gst-plugins-bad-1.0
gstreamer1.0-tools / -plugins-base / -plugins-good / -plugins-bad / -plugins-ugly / -libav
gstreamer1.0-nice
v4l-utils
```

Полный перечень содержится в поле `Depends:` файла `DEBIAN/control`
устанавливаемого `.deb`-пакета.

---

## 6. Установка и настройка

### 6.1. Установка через `.deb`-пакет

Рекомендуемый способ. Файл получается от сервера «MavixServer»
обращением:

```
GET https://<server>/api/v1/builds/board.deb?drone_id=<id>
Authorization: Bearer <access_token>
```

Установка на дроне:

```bash
sudo apt update
sudo apt install ./mavixboard.deb
sudo systemctl enable --now mavixboard.service
```

В процессе установки:

- создаётся виртуальное окружение `/opt/mavixboard/.venv` с доступом
  к системным пакетам (`--system-site-packages`);
- из локального wheel и из PyPI устанавливаются зависимости;
- регистрируется и запускается systemd-служба `mavixboard.service`.

### 6.2. Установка для разработки

```bash
git clone https://github.com/AurunChill/MavixBoard.git
cd MavixBoard
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env-example .env
# Отредактировать .env
.venv/bin/python -m mavixboard
```

### 6.3. Параметры конфигурации

| Переменная | Назначение | Значение по умолчанию |
|---|---|---|
| `SIGNAL_SERVER_IP` | URL сервера сигнализации | `http://localhost` |
| `SIGNAL_WS_URL` | Полный URL WebSocket (если иной путь) | пусто |
| `USER_ID` | Идентификатор владельца (32 символа) | пусто |
| `STUN_SERVER` | URL STUN-сервера | `stun://localhost:3478` |
| `TURN_SERVER` | URL TURN-сервера | пусто |

При установке через `.deb` параметры `USER_ID`, `DRONE_TOKEN`,
`SIGNAL_SERVER_IP` записываются в `/etc/mavixboard/preset.env` сервером
автоматически.

---

## 7. Проверка работы

### 7.1. Автоматизированные тесты

```bash
.venv/bin/pytest
# Ожидаемый результат: 303 passed
```

### 7.2. Тестирование без аппаратного обеспечения

При запуске без камер и без полётного контроллера программа:

- успешно подключается к серверу сигнализации;
- получает запрос `connect`;
- сообщает об отсутствии камер в журнале и не открывает сессию;
- продолжает ожидание новых запросов.

### 7.3. Состояние службы

```bash
sudo systemctl status mavixboard
journalctl -u mavixboard -f
```

---

## 8. Сообщения системному программисту

| Сообщение | Причина | Действия |
|---|---|---|
| `cameras not found` | Не обнаружены устройства `/dev/video*` | Проверить подключение камер, права на устройства (`v4l2-ctl --list-devices`) |
| `[signal] connect error: ...` | Невозможно соединиться с сервером | Проверить сетевую доступность, корректность `SIGNAL_SERVER_IP` |
| `[coord] pipeline error` | Сбой GStreamer-конвейера | Просмотреть журнал на наличие диагностики GStreamer; убедиться в исправности камер |
| `[fc-service] FC disconnected` | Полётный контроллер не отвечает | Проверить кабель UART, права на `/dev/ttyACM*` |
| `[manager] session already active` | Получен повторный `connect` | Штатная ситуация при перезаключении; сессия будет пересоздана |
| `[coord] reboot requested via config channel` | Получена команда `reboot` от наземной станции | Программа перезапускается через `os.execv` |

---

## 9. Журналирование

Журнал ведётся в стандартный поток вывода (виден через `journalctl`)
и в файл `~/.config/mavixboard/logs/mavixboard_<дата>.log`.

Уровни сообщений: `INFO`, `WARNING`, `ERROR`. Уровень `INFO`
используется для отслеживания фаз работы (подключение, открытие
канала, получение FC-пакета).
