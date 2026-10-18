"""Advisory file locks for macOS/Linux and Windows, using only the stdlib."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time


@contextmanager
def locked_file(path, blocking=True, busy_error=None):
    path = Path(path)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'r+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'\0'); handle.flush()
            while True:
                try:
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1); break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):raise
                    if not blocking:raise busy_error or BlockingIOError('File is locked.') from error
                    time.sleep(0.05)
            try:yield handle
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as error:
                if busy_error:raise busy_error from error
                raise
            try:yield handle
            finally:fcntl.flock(handle, fcntl.LOCK_UN)
