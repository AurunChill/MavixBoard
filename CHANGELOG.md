# Changelog

Все значимые изменения бортового модуля **MavixBoard** документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [семантического версионирования](https://semver.org/lang/ru/).

## [1.0.0] - 2026-06-08

Первый стабильный релиз бортового модуля комплекса доставки грузов на базе БПЛА.
Модуль работает на одноплатном компьютере (Raspberry Pi), передаёт видео и
телеметрию оператору по WebRTC, исполняет команды управления и сброс груза.

### Added
- Полная ГОСТ-документация бортового модуля и диаграммы (структура, WebRTC-слой).
- Раздел «Принципы проектирования» (SOLID/DRY/KISS/YAGNI) с примерами из кода.
- Проприетарная лицензия (LICENSE) и история изменений (CHANGELOG).

## [0.8.0] - 2026-06-07

### Added
- Саморегистрация дрона при первом запуске (enroll-at-startup) — получение
  идентификатора и токена от сервера без ручной настройки.
- Выделенный канал телеметрии (GPS/heading) через отдельный data-channel.

### Fixed
- Телеметрия без GPS-фикса больше не пробрасывается (исключён «Null Island» 0,0).
- Отключён UPnP и изолирован teardown WebRTC — борт не зависает при выходе оператора.

## [0.7.0] - 2026-06-01

### Changed
- Рефакторинг ядра: инъекция камер, привязка калибровки к камере, чистка кода.
- CRSF-кодек разделён на кодирующую/декодирующую половины, именованные байтовые
  константы, удалён неиспользуемый декод.
- Единый стиль Python-кода, секции-баннеры, удаление мёртвого кода, починка тестов.

## [0.6.0] - 2026-05-28

### Added
- Поддержка TURN с авторизацией, keyframe 2/s, intra-refresh для стабильного видео.
- Флаг `FORCE_RELAY` (`ice-transport-policy=relay`) и подробная ICE-диагностика.

### Fixed
- Корректная регистрация TURN-сервера через сигнал `add-turn-server`.
- URL-кодирование учётных данных TURN, нормализация STUN/TURN URL.

## [0.5.0] - 2026-05-18

### Added
- CRSF-репитер 200 Гц + `LINK_STATISTICS` 10 Гц для надёжного канала управления.
- Поддержка SITL через `CRSF_URL` (pyserial `socket://`) без socat/pty.

## [0.4.0] - 2026-05-14

### Added
- Кэш калибровки камер с принудительным recalibrate и надёжным стартом.
- Горячая замена (hot-plug) полётного контроллера.

### Fixed
- Тихое завершение работы, неблокирующий event loop, watcher без ISP-шумов.

## [0.3.0] - 2026-05-13

### Added
- WebSocket-сигналинг (`SignalClient`) для связи дрона с сервером.
- Слой полётного контроллера: CRSF, MAVLink, взаимозаменяемые контроллеры, `FCService`.
- WebRTC: `PeerSession`, `WebRTCManager`, каналы данных.
- `CameraWatcher` и наблюдение за списком камер.
- Координатор сессии (`SessionCoordinator`), связывающий сигналинг, WebRTC и FC.
- Экспоненциальный backoff при переподключении, загрузка `preset.env`.
- Техническое описание и руководство оператора по ГОСТ.

## [0.2.0] - 2026-05-08

### Added
- Модуль GStreamer (захват и кодирование видео), конфигурация STUN/TURN.

## [0.1.0] - 2026-05-07

### Added
- Базовая структура проекта.
- Генерация и хранение токенов, логирование, dataclass настроек.
- Серверный API на aiohttp, вынос конфигурации в `core/`, тесты.

[1.0.0]: https://github.com/AurunChill/MavixBoard/releases/tag/v1.0.0
[0.8.0]: https://github.com/AurunChill/MavixBoard/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/AurunChill/MavixBoard/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/AurunChill/MavixBoard/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/AurunChill/MavixBoard/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/AurunChill/MavixBoard/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/AurunChill/MavixBoard/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/AurunChill/MavixBoard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AurunChill/MavixBoard/releases/tag/v0.1.0
