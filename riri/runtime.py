"""Whole-process wall clock bound, including trickling HTTP responses."""
from contextlib import contextmanager
import signal
import threading

from .errors import SafetyError, TimeBudgetExceeded


@contextmanager
def hard_deadline(seconds):
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise SafetyError("安全な実行期限を設定できない環境です")
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    if old_timer[0] or old_timer[1]:
        raise SafetyError("別の実行期限タイマーが動いています")
    def expired(signum, frame):
        raise TimeBudgetExceeded("見回りの実行時間の上限に達しました")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(240, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
