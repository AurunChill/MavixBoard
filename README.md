# MavixBoard

Бортовая часть системы автоматизированной доставки малогабаритных грузов дронами
**Mavix**. Работает на Raspberry Pi: стримит видео по WebRTC, пробрасывает команды
на полётный контроллер (CRSF/MAVLink), отдаёт телеметрию, исполняет сброс груза и
сам регистрируется на сервере (enrollment).

## Стек
Python 3.12 · GStreamer (`webrtcbin`, libnice) · PyGObject · asyncio · Raspberry Pi (Ubuntu).

## Быстрый старт
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # + системные GStreamer-пакеты (см. HARDWARE_SETUP §1.6)
# задать ADMIN_ID и ENROLLMENT_TOKEN в env (из кабинета администратора)
python -m mavixboard        # при первом запуске борт зарегистрируется (enrollment)
```

## Тесты
```bash
python -m pytest -q         # полный набор — зелёный
```

## Документация
- [HARDWARE_SETUP.md](HARDWARE_SETUP.md) — **подготовка железа**: Raspberry Pi
  (сеть, LTE, камеры), UART и права на serial-порты, прошивка и настройка FC
  (Betaflight/iNav/ArduPilot/PX4), порядок ARM, диагностика.
- [WEBRTC_TURN_NOTES.md](WEBRTC_TURN_NOTES.md) — совместимость aiortc ↔ webrtcbin,
  TURN/ICE, костыли (trickle-кандидаты, relay_patch, force_relay).
- [TECHNICAL.md](TECHNICAL.md) — техническое описание (ГОСТ 19.402): структура
  WebRTC-слоя, диаграммы, «Сложности и принятые решения».
- [USER_GUIDE.md](USER_GUIDE.md) — руководство по запуску борта.
- Обзор всей системы — корневой [README.md](../README.md).
