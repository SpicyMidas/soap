"""Queue + loop + history logic. Pure Python so it is easy to test."""

from __future__ import annotations

import enum
import random
from collections import deque
from typing import Iterable, Iterator

from .audio import Track


class LoopMode(enum.IntEnum):
    OFF = 0
    TRACK = 1
    QUEUE = 2

    @property
    def label(self) -> str:
        return {LoopMode.OFF: "Off", LoopMode.TRACK: "Song", LoopMode.QUEUE: "Queue"}[self]

    @property
    def emoji(self) -> str:
        return {LoopMode.OFF: "➡️", LoopMode.TRACK: "🔂", LoopMode.QUEUE: "🔁"}[self]

    def next(self) -> LoopMode:
        return LoopMode((self.value + 1) % 3)


class QueueFull(Exception):
    pass


class MusicQueue:
    HISTORY_SIZE = 50

    def __init__(self, max_size: int = 500) -> None:
        self.max_size = max_size
        self.tracks: list[Track] = []
        self.history: deque[Track] = deque(maxlen=self.HISTORY_SIZE)

    # -- basics ----------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.tracks)

    def __bool__(self) -> bool:
        return bool(self.tracks)

    def __iter__(self) -> Iterator[Track]:
        return iter(self.tracks)

    def __getitem__(self, index: int) -> Track:
        return self.tracks[index]

    @property
    def space_left(self) -> int:
        return max(0, self.max_size - len(self.tracks))

    @property
    def total_duration(self) -> int:
        return sum(t.duration or 0 for t in self.tracks)

    @property
    def has_live(self) -> bool:
        return any(t.duration is None for t in self.tracks)

    # -- adding / removing -----------------------------------------------------
    def add(self, tracks: Iterable[Track], *, front: bool = False) -> int:
        """Add tracks, returns how many were added (stops when the queue is full)."""
        new = list(tracks)[: self.space_left]
        if not new and not self.space_left:
            raise QueueFull(f"The queue is full ({self.max_size} songs).")
        if front:
            self.tracks[0:0] = new
        else:
            self.tracks.extend(new)
        return len(new)

    def pop_next(self) -> Track | None:
        return self.tracks.pop(0) if self.tracks else None

    def remove(self, index: int) -> Track:
        """0-based index."""
        self._check(index)
        return self.tracks.pop(index)

    def move(self, src: int, dst: int) -> Track:
        """0-based indexes; dst is clamped into range."""
        self._check(src)
        track = self.tracks.pop(src)
        dst = max(0, min(dst, len(self.tracks)))
        self.tracks.insert(dst, track)
        return track

    def shuffle(self) -> None:
        random.shuffle(self.tracks)

    def clear(self) -> int:
        count = len(self.tracks)
        self.tracks.clear()
        return count

    def skip_to(self, index: int, loop: LoopMode) -> Track:
        """Drop everything before `index` (0-based) so it plays next."""
        self._check(index)
        dropped = self.tracks[:index]
        del self.tracks[:index]
        if loop is LoopMode.QUEUE:
            self.tracks.extend(dropped)
        return self.tracks[0]

    def _check(self, index: int) -> None:
        if not 0 <= index < len(self.tracks):
            raise IndexError(f"Position must be between 1 and {len(self.tracks)}.")

    # -- playback bookkeeping --------------------------------------------------
    def finished(self, track: Track, loop: LoopMode, *, skipped: bool) -> None:
        """Called when a song stops playing to apply loop mode + history."""
        if loop is LoopMode.TRACK and not skipped:
            self.tracks.insert(0, track)  # play it again
            return
        self.history.append(track)
        if loop is LoopMode.QUEUE:
            self.tracks.append(track.copy())

    def take_previous(self) -> Track | None:
        return self.history.pop() if self.history else None

    def recent_ids(self, extra: Iterable[Track] = ()) -> set[str]:
        ids = {t.video_id for t in self.history if t.video_id}
        ids |= {t.video_id for t in self.tracks if t.video_id}
        ids |= {t.video_id for t in extra if t and t.video_id}
        return ids
