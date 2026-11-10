"""Fixed local notices only; safe to import before the Python 3.11 check.

No caller text, exception content, or credentials are accepted as message data.
Keep this module free of modern syntax so the old-Python guard can import it.
"""
import platform
import subprocess

_MESSAGES = {
    "account_mismatch": "アカウント違いのため停止しました。設定を確認してください。",
    "python_too_old": "Python 3.11以上が必要なため停止しました。Pythonの設定を確認してください。",
    "unexpected_error": "予期しないエラーのため停止しました。ログと設定を確認してください。",
}


def notify(kind):
    """Send an allowlisted fixed notice; unknown kinds safely do nothing."""
    message = _MESSAGES.get(kind) if isinstance(kind, str) else None
    if message is None or platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'display notification "' + message + '" with title "りりの見回り"'],
            timeout=3, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def account_mismatch():
    return notify("account_mismatch")


def python_too_old():
    return notify("python_too_old")


def unexpected_error():
    return notify("unexpected_error")
