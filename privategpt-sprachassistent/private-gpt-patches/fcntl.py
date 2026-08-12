"""Minimal Windows stub for the Unix-only ``fcntl`` module.

PrivateGPT's model discovery imports ``fcntl`` unconditionally (used only for
file locking in the CLI downloader). On Windows this module does not exist,
which breaks tokenizer/model discovery. These are no-op implementations of the
few symbols that get imported or called.
"""

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd, operation):
    """No-op replacement for fcntl.flock (advisory locks are not supported)."""
    return None


def ioctl(fd, request, arg=0, mutate_flag=True):
    return 0


def fcntl(fd, cmd, arg=0):
    return 0
