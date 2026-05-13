from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExponentialBackoff:
    initial: float = 1.0
    multiplier: float = 2.0
    cap: float = 30.0
    _current: float = 0.0

    def __post_init__(self) -> None:
        if self.initial <= 0:
            raise ValueError('initial must be > 0')
        if self.multiplier < 1.0:
            raise ValueError('multiplier must be >= 1')
        if self.cap < self.initial:
            raise ValueError('cap must be >= initial')
        self._current = self.initial

    @property
    def current(self) -> float:
        return self._current

    def next_delay(self) -> float:
        delay = self._current
        self._current = min(self.cap, self._current * self.multiplier)
        return delay

    def reset(self) -> None:
        self._current = self.initial
