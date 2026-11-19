"""Run with:  python -m unittest"""

import unittest

from musicbot.audio import Track, parse_timestamp, format_duration, track_from_entry, youtube_id
from musicbot.musicqueue import LoopMode, MusicQueue, QueueFull


def make(name: str, duration: int = 100) -> Track:
    return Track(title=name, url=f"https://example.com/{name}", duration=duration)


def titles(q: MusicQueue) -> list[str]:
    return [t.title for t in q]


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.q = MusicQueue(max_size=5)
        self.q.add([make("a"), make("b"), make("c")])

    def test_add_front_and_limit(self) -> None:
        self.q.add([make("x")], front=True)
        self.assertEqual(titles(self.q), ["x", "a", "b", "c"])
        added = self.q.add([make("y"), make("z")])
        self.assertEqual(added, 1)  # only one slot left
        with self.assertRaises(QueueFull):
            self.q.add([make("w")])

    def test_remove_move_skip_to(self) -> None:
        self.assertEqual(self.q.remove(1).title, "b")
        self.assertEqual(titles(self.q), ["a", "c"])
        self.q.add([make("d")])
        self.q.move(2, 0)
        self.assertEqual(titles(self.q), ["d", "a", "c"])
        self.q.move(0, 99)  # clamps to the end
        self.assertEqual(titles(self.q), ["a", "c", "d"])
        self.assertEqual(self.q.skip_to(2, LoopMode.OFF).title, "d")
        self.assertEqual(titles(self.q), ["d"])
        with self.assertRaises(IndexError):
            self.q.remove(5)

    def test_skip_to_with_queue_loop_keeps_songs(self) -> None:
        self.q.skip_to(2, LoopMode.QUEUE)
        self.assertEqual(titles(self.q), ["c", "a", "b"])

    def test_loop_modes(self) -> None:
        cur = self.q.pop_next()
        self.q.finished(cur, LoopMode.TRACK, skipped=False)
        self.assertEqual(titles(self.q), ["a", "b", "c"])  # same song again
        self.assertEqual(len(self.q.history), 0)

        cur = self.q.pop_next()
        self.q.finished(cur, LoopMode.TRACK, skipped=True)  # skip beats song loop
        self.assertEqual(titles(self.q), ["b", "c"])
        self.assertEqual([t.title for t in self.q.history], ["a"])

        cur = self.q.pop_next()
        self.q.finished(cur, LoopMode.QUEUE, skipped=False)
        self.assertEqual(titles(self.q), ["c", "b"])

    def test_previous(self) -> None:
        self.q.finished(self.q.pop_next(), LoopMode.OFF, skipped=False)
        self.q.finished(self.q.pop_next(), LoopMode.OFF, skipped=False)
        self.assertEqual(self.q.take_previous().title, "b")
        self.assertEqual(self.q.take_previous().title, "a")
        self.assertIsNone(self.q.take_previous())

    def test_shuffle_keeps_items(self) -> None:
        self.q.shuffle()
        self.assertEqual(sorted(titles(self.q)), ["a", "b", "c"])
        self.assertEqual(self.q.total_duration, 300)

    def test_loop_mode_cycle(self) -> None:
        self.assertIs(LoopMode.OFF.next(), LoopMode.TRACK)
        self.assertIs(LoopMode.TRACK.next(), LoopMode.QUEUE)
        self.assertIs(LoopMode.QUEUE.next(), LoopMode.OFF)


class HelperTests(unittest.TestCase):
    def test_parse_timestamp(self) -> None:
        self.assertEqual(parse_timestamp("90"), 90)
        self.assertEqual(parse_timestamp("1:30"), 90)
        self.assertEqual(parse_timestamp("1:02:03"), 3723)
        self.assertEqual(parse_timestamp("1m30s"), 90)
        self.assertEqual(parse_timestamp("2h"), 7200)
        self.assertIsNone(parse_timestamp("abc"))
        self.assertIsNone(parse_timestamp(""))

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(5), "0:05")
        self.assertEqual(format_duration(3723), "1:02:03")
        self.assertEqual(format_duration(None), "LIVE")

    def test_youtube_id(self) -> None:
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=10",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVM",
            "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ",
        ):
            self.assertEqual(youtube_id(url), "dQw4w9WgXcQ", url)
        self.assertIsNone(youtube_id("https://soundcloud.com/a/b"))

    def test_track_from_flat_entry(self) -> None:
        entry = {
            "ie_key": "Youtube", "id": "dQw4w9WgXcQ", "title": "Song",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "duration": 213.0, "channel": "Rick",
        }
        t = track_from_entry(entry)
        self.assertEqual((t.title, t.duration, t.uploader, t.video_id), ("Song", 213, "Rick", "dQw4w9WgXcQ"))
        self.assertIsNone(t.stream_url)
        self.assertIsNone(track_from_entry({"ie_key": "Youtube", "id": "x" * 11, "title": "[Private video]"}))


if __name__ == "__main__":
    unittest.main()
