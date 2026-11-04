"""screen.shot_lock: the capture lock's timeout says what happened (D630: a fact, no 'try again')."""

import fcntl
import os
import tempfile
import unittest
from pathlib import Path

from bg3ai import screen


class ShotLock(unittest.TestCase):
    def test_a_held_lock_times_out_with_the_fact(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / screen.LOCK_NAME
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)  # another open file: flock conflicts in-process too
            self.addCleanup(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(TimeoutError) as cm, screen.shot_lock(path, timeout=0.1):
                pass
            self.assertEqual(
                str(cm.exception),
                "another process held the screen capture lock for 0.1s (waited the whole time; no frame was taken)",
            )

    def test_a_free_lock_is_taken(self):
        with tempfile.TemporaryDirectory() as d, screen.shot_lock(Path(d) / screen.LOCK_NAME, timeout=0.1):
            pass


if __name__ == "__main__":
    unittest.main()
