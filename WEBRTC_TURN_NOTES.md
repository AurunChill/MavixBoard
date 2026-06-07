# WebRTC ↔ TURN: разбор совместимости aiortc и webrtcbin

Историческая, но актуальная заметка: почему не поднималось WebRTC-соединение
борт (GStreamer `webrtcbin`) ↔ оператор (aiortc) через TURN, и какие костыли
совместимости из-за этого появились. Эти решения живут в коде до сих пор
(`relay_patch.py`, `FORCE_RELAY`, парсинг trickle-кандидатов), поэтому держим
разбор рядом с бортом.

## Симптом
Борт (`webrtcbin`) ↔ оператор (aiortc) не соединялись за NAT (должны были идти
через TURN). В логах: `ICE connection state -> checking → failed`. Локально (одна
сеть) иногда поднималось напрямую; через интернет/relay — стабильно `failed`.

## Главная причина
**Desktop неправильно парсил trickle-ICE-кандидаты от дрона.** libnice шлёт
кандидаты по одному (trickle). В `add_remote_ice` вытаскивался только **тип**, а
`ip`/`port`/`foundation`/`priority` оставались пустыми:
```python
ice = RTCIceCandidate(component=1, foundation='', ip='', port=0, priority=0, ...)
ice.candidate = cand_str        # ← aiortc это игнорирует
```
aiortc 1.14 строит кандидата **из полей объекта**, а не из строки `.candidate` →
поля пустые → `aioice`: `Remote candidate "" is not valid` → у desktop НЕТ ни
одного удалённого кандидата → проверять не с чем → `ICE failed`. Видео-relay ни
при чём — соединение умирало на обмене кандидатами.

**Фикс** — парсить всю строку штатным парсером aiortc:
```python
from aiortc.sdp import candidate_from_sdp
sdp_str = cand_str[len('candidate:'):] if cand_str.startswith('candidate:') else cand_str
ice = candidate_from_sdp(sdp_str)   # заполняет ip/port/foundation/priority/type
ice.sdpMid = sdp_mid
ice.sdpMLineIndex = sdp_mline_index
await self._pc.addIceCandidate(ice)
```
Коммит `6f57384` (MavixDesktop-UI).

## Сопутствующие костыли совместимости
1. **Desktop игнорировал локальный TURN-конфиг.** `coordinator.py` всегда брал
   ICE-серверы из API и затирал `config.py`. Фикс: `_local_ice_servers()` —
   приоритет локального STUN/TURN, на сервер идём только если локально пусто.
2. **«Ненастоящий» force_relay на desktop.** aiortc 1.14 не отдаёт
   `iceTransportPolicy` через `RTCConfiguration`; вырезание не-relay строк из SDP
   не помогало — ICE-агент всё равно работал `transport_policy=ALL`, собирал
   host/srflx и слал проверки, которые coturn дропал (нет permission на чужой
   relay). Фикс: `relay_patch.py` — monkeypatch подменяет `aioice.Connection` на
   подкласс с `transport_policy=RELAY` (нативный relay-only), гейтится живым
   `settings.force_relay` + наличием TURN. Исходники библиотеки не трогали.
   Коммит `50da147`.
3. **Board не умел force_relay.** Добавлен `FORCE_RELAY` → в `webrtcbin`
   подставляется `ice-transport-policy=relay` (`core/config.py`,
   `gstreamer/pipeline.py`). Коммит `8fb00f5`.

## Что НЕ было причиной (исключено, сэкономило бы время)
Сервер/сеть исправны: relay-порты 49152–65535 открыты; coturn relay↔relay даёт
40/40 без потерь (UDP/3478 и TLS/443); TURNS:443 работает; ACL coturn
default-allow (CreatePermission на приватные IP проходит). **Вывод: наличие
relay-кандидата ≠ relay работает; `ICE failed` при наличии relay-кандидата чаще
означает проблему обмена/применения кандидатов, а не TURN.**

## Диагностика (инструменты)
1. `turnutils_uclient -y -u <user> -w <pass> -p <port> -n 10 <host>` (+`-S` для
   TLS) — встроенный в coturn тест relay↔relay; изолирует сервер от приложения.
2. `ICE_DEBUG=1 python3 -m mavixdesktop` — DEBUG для `aioice`/`aiortc`: видно
   каждую аллокацию/кандидата/проверку. Именно он показал `Remote candidate "" is
   not valid` — финальную улику.
3. Лог coturn (`journalctl -u coturn`): `peer usage: rp/sp` — принято/отправлено
   через relay (`rp=0` при `sp>0` = проверки уходят, ответы не возвращаются);
   `CREATE_PERMISSION ... success` — кому разрешён relay.

## Заметки по серверу (на будущее)
- `user-quota` в `/etc/turnserver.conf` может забиваться при утечке аллокаций
  (board/desktop не всегда закрывают TURN при teardown) — поднять квоту или
  выдавать временные креды.
- Для realtime-видео отдавать клиентам и UDP-TURN (`turn:host:3478`), а не только
  `turns:host:443` (TCP/TLS = лишняя задержка и head-of-line blocking).

> Дополнительно к этому: при teardown борта финальный unref `webrtcbin` может
> зависнуть в gupnp-igd (UPnP) — отключаем UPnP и изолируем teardown. Подробности
> — в [TECHNICAL.md](TECHNICAL.md), раздел «Сложности и принятые решения».
