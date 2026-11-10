"""Safe error types: never retain remote exception messages or response bodies."""
import re

_SECRET = re.compile(r'(?:xox[a-zA-Z]*[-_][^\s<>"\'`]+|ntn_[^\s<>"\'`]+|secret_[^\s<>"\'`]+)', re.I)

def sanitize(text, secrets=()):
    text = str(text)
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    return _SECRET.sub('[REDACTED]', text)

class PatrolError(Exception):
    def __init__(self, message):
        super().__init__(sanitize(message))
class AccountMismatch(PatrolError): pass
class RequestIncomplete(PatrolError): pass
class DuplicateRequestId(PatrolError): pass
class StatusMissing(PatrolError): pass
class SafetyError(PatrolError): pass
class TimeBudgetExceeded(PatrolError): pass
class RemoteError(PatrolError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain
class UncertainPost(RemoteError):
    def __init__(self, message='Slack post outcome is unknown; automatic resend prohibited', uncertain=True):
        super().__init__(message, uncertain=True)

class NotionConflict(RemoteError):
    """A concurrent edit is never eligible for automatic repair or retry."""
    def __init__(self, message="Notionの同時編集を検出しました。上書きせず確認を待ちます"):
        super().__init__(message, uncertain=True)
