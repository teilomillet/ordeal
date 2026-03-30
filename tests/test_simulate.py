"""Tests for ordeal.simulate — no-mock simulation primitives."""
import time

import pytest

from ordeal.simulate import Clock, FileSystem


# ============================================================================
# Clock
# ============================================================================

class TestClock:
    def test_starts_at_zero(self):
        assert Clock().time() == 0.0

    def test_custom_start(self):
        assert Clock(start=100.0).time() == 100.0

    def test_advance(self):
        c = Clock()
        c.advance(10.5)
        assert c.time() == 10.5

    def test_advance_cumulative(self):
        c = Clock()
        c.advance(5.0)
        c.advance(3.0)
        assert c.time() == 8.0

    def test_sleep_advances(self):
        c = Clock()
        c.sleep(7.0)
        assert c.time() == 7.0

    def test_advance_negative_raises(self):
        c = Clock()
        with pytest.raises(ValueError, match="backwards"):
            c.advance(-1.0)

    def test_timer_fires_on_advance(self):
        c = Clock()
        fired: list[float] = []
        c.set_timer(5.0, lambda: fired.append(c.time()))
        c.advance(10.0)
        assert fired == [5.0]

    def test_timer_does_not_fire_early(self):
        c = Clock()
        fired: list[bool] = []
        c.set_timer(10.0, lambda: fired.append(True))
        c.advance(5.0)
        assert fired == []

    def test_multiple_timers_ordered(self):
        c = Clock()
        order: list[str] = []
        c.set_timer(3.0, lambda: order.append("A"))
        c.set_timer(1.0, lambda: order.append("B"))
        c.set_timer(2.0, lambda: order.append("C"))
        c.advance(5.0)
        assert order == ["B", "C", "A"]

    def test_pending_timers(self):
        c = Clock()
        c.set_timer(1.0, lambda: None)
        c.set_timer(2.0, lambda: None)
        assert c.pending_timers == 2
        c.advance(1.5)
        assert c.pending_timers == 1

    def test_patch_time(self):
        c = Clock()
        with c.patch():
            assert time.time() == 0.0
            time.sleep(5.0)
            assert time.time() == 5.0


# ============================================================================
# FileSystem
# ============================================================================

class TestFileSystem:
    def test_write_read_bytes(self):
        fs = FileSystem()
        fs.write("/a.bin", b"\x01\x02\x03")
        assert fs.read("/a.bin") == b"\x01\x02\x03"

    def test_write_read_str(self):
        fs = FileSystem()
        fs.write("/a.txt", "hello")
        assert fs.read("/a.txt") == b"hello"

    def test_read_text(self):
        fs = FileSystem()
        fs.write("/a.txt", "hello")
        assert fs.read_text("/a.txt") == "hello"

    def test_missing_file_raises(self):
        fs = FileSystem()
        with pytest.raises(FileNotFoundError):
            fs.read("/nope")

    def test_exists(self):
        fs = FileSystem()
        assert not fs.exists("/a")
        fs.write("/a", b"x")
        assert fs.exists("/a")

    def test_delete(self):
        fs = FileSystem()
        fs.write("/a", b"x")
        fs.delete("/a")
        assert not fs.exists("/a")

    def test_list_dir(self):
        fs = FileSystem()
        fs.write("/data/a.txt", b"a")
        fs.write("/data/b.txt", b"b")
        fs.write("/other/c.txt", b"c")
        assert fs.list_dir("/data/") == ["/data/a.txt", "/data/b.txt"]

    def test_reset(self):
        fs = FileSystem()
        fs.write("/a", b"x")
        fs.inject_fault("/b", "readonly")
        fs.reset()
        assert not fs.exists("/a")
        # Fault should also be cleared
        fs.write("/b", b"y")  # should not raise

    # -- Faults --

    def test_fault_readonly(self):
        fs = FileSystem()
        fs.inject_fault("/a", "readonly")
        with pytest.raises(PermissionError):
            fs.write("/a", b"x")

    def test_fault_full(self):
        fs = FileSystem()
        fs.inject_fault("/a", "full")
        with pytest.raises(OSError):
            fs.write("/a", b"x")

    def test_fault_missing(self):
        fs = FileSystem()
        fs.write("/a", b"real data")
        fs.inject_fault("/a", "missing")
        with pytest.raises(FileNotFoundError):
            fs.read("/a")
        assert not fs.exists("/a")  # exists also respects fault

    def test_fault_corrupt(self):
        fs = FileSystem()
        original = b"\x00" * 100
        fs.write("/a", original)
        fs.inject_fault("/a", "corrupt")
        data = fs.read("/a")
        assert len(data) == 100
        assert data != original  # overwhelmingly likely

    def test_clear_fault(self):
        fs = FileSystem()
        fs.write("/a", b"real")
        fs.inject_fault("/a", "missing")
        fs.clear_fault("/a")
        assert fs.read("/a") == b"real"  # back to normal
