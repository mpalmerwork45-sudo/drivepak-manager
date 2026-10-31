from concurrent.futures import ThreadPoolExecutor
import threading

from drivepak_manager.image import Image


def test_shared_image_seek_and_read_are_one_operation(tmp_path):
    path = tmp_path / "tiny.img"
    path.write_bytes(b"a" * 512 + b"b" * 512)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    with Image(path) as image:
        actual = image.file
        class PausedSeek:
            def __getattr__(self, key): return getattr(actual, key)
            def seek(self, offset):
                actual.seek(offset)
                if offset == 0:
                    entered.set()
                    assert release.wait(5)
        image.file = PausedSeek()
        def second():
            second_started.set()
            return image.read(512, 512)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(image.read, 0, 512)
            try:
                assert entered.wait(5)
                other = pool.submit(second)
                assert second_started.wait(5)
                assert not other.done()
            finally:
                release.set()
            assert first.result(timeout=5) == b"a" * 512
            assert other.result(timeout=5) == b"b" * 512
