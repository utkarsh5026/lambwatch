from __future__ import annotations

import json
import os
from pathlib import Path

from lambda_watcher import heartbeat
from lambda_watcher.heartbeat import Heartbeat


def _beat(**overrides) -> Heartbeat:
    fields = {"pid": os.getpid(), "started_at": "2026-09-22T08:00:00+00:00",
              "last_beat_at": "2026-09-22T08:00:00+00:00"}
    fields.update(overrides)
    return Heartbeat(**fields)


def test_a_heartbeat_reads_back_what_was_written(tmp_path: Path):
    path = tmp_path / "state" / "watcher.json"
    heartbeat.write(path, _beat(dirs=["/mnt/c/Users/Sam/Downloads"], observer="polling", seen=3))
    back = heartbeat.read(path)
    assert back is not None
    assert back.dirs == ["/mnt/c/Users/Sam/Downloads"]
    assert back.observer == "polling" and back.seen == 3


def test_no_heartbeat_is_an_ordinary_answer_not_an_error(tmp_path: Path):
    assert heartbeat.read(tmp_path / "nothing-here.json") is None


def test_a_half_written_heartbeat_is_treated_as_none(tmp_path: Path):
    path = tmp_path / "watcher.json"
    path.write_text('{"pid": 12, "started_at": ', encoding="utf-8")
    assert heartbeat.read(path) is None


def test_a_heartbeat_from_a_newer_release_still_reads(tmp_path: Path):
    # A field this release has never heard of must not make the file unreadable.
    path = tmp_path / "watcher.json"
    data = {"pid": 1, "started_at": "2026-09-22T08:00:00+00:00",
            "last_beat_at": "2026-09-22T08:00:00+00:00", "from_the_future": True}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert heartbeat.read(path) is not None


def test_a_recent_beat_is_fresh():
    assert not _beat().is_stale(now="2026-09-22T08:01:00+00:00")


def test_a_beat_long_past_is_stale():
    later = "2026-09-22T09:00:00+00:00"   # an hour, well past five missed beats
    assert _beat().is_stale(now=later)


def test_a_beat_that_cannot_be_dated_counts_as_stale():
    assert _beat(last_beat_at="not a time").is_stale()


def test_a_cleanly_stopped_watcher_is_not_running():
    assert not _beat(stopped_at="2026-09-22T08:05:00+00:00").is_running()


def test_this_process_counts_as_running():
    assert _beat(pid=os.getpid()).is_running()
