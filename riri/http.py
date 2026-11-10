"""Small injectable JSON transport with no redirects or ambiguous-write retries.

Socket timeouts limit idle I/O, not total elapsed time during trickle reads.
The CLI must additionally enforce its hard process deadline. We check the
monotonic deadline after reading too, rejecting late responses conservatively.
"""
import json
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from .errors import RemoteError, SafetyError, TimeBudgetExceeded

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Transport:
    def __init__(self, deadline=None, clock=time.monotonic, sleep=time.sleep, opener=None, before_request=None):
        self.deadline, self.clock, self.sleep = deadline, clock, sleep
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self._notion_at = None
        self.before_request = before_request

    def _budget(self, timeout):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - self.clock())
        if timeout <= 0:
            raise TimeBudgetExceeded('HTTP time budget exhausted')
        return timeout

    def _wait(self, seconds):
        self._budget(seconds + 0.001)
        if self.deadline is not None and self.clock() + seconds >= self.deadline:
            raise TimeBudgetExceeded('HTTP retry exceeds time budget')
        if seconds > 0:
            self.sleep(seconds)

    def request(self, method, url, headers=None, payload=None, timeout=20):
        parsed = urllib.parse.urlsplit(url)
        remote = parsed.scheme == 'https' and parsed.netloc in ('slack.com', 'api.notion.com')
        local = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if not (remote or local) or parsed.username or parsed.password or parsed.fragment:
            raise SafetyError('Untrusted HTTP origin')
        if local and any(k.lower() == 'authorization' for k in (headers or {})):
            raise SafetyError('Credentials cannot be sent to local HTTP')
        method = method.upper()
        write = method not in ('GET', 'HEAD') and not url.endswith('/auth.test') and not local
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = dict(headers or {})
        if data is not None:
            headers['Content-Type'] = 'application/json; charset=utf-8'
        for attempt in range(3):
            if parsed.netloc == 'api.notion.com' and self._notion_at is not None:
                self._wait(max(0, .35 - (self.clock() - self._notion_at)))
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            allowed_timeout = self._budget(timeout)
            # A STOP/deadline guard here covers every attempt, including retries and
            # the gap between a Notion reread and its subsequent PATCH.
            # Keep it outside I/O handling: no request was attempted if it raises.
            if self.before_request is not None:
                self.before_request()
            try:
                if parsed.netloc == 'api.notion.com':
                    self._notion_at = self.clock()
                with self.opener.open(request, timeout=allowed_timeout) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise ValueError('too large')
                    self._budget(timeout)
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError('not object')
                    return result
            except TimeBudgetExceeded:
                if write:
                    raise RemoteError('HTTP write deadline expired; outcome unknown', uncertain=True) from None
                raise
            except urllib.error.HTTPError as error:
                code = error.code
                retry = code == 429 or (code == 529 and parsed.netloc == 'api.notion.com')
                if retry and attempt < 2:
                    try:
                        delay = float(error.headers.get('Retry-After', '1'))
                        if not 0 <= delay <= 240:
                            raise ValueError()
                    except (ValueError, TypeError):
                        raise RemoteError('Invalid Retry-After', uncertain=False) from None
                    self._wait(delay)
                    continue
                raise RemoteError(f'HTTP request failed ({code})', uncertain=write and code >= 500 and code != 529) from None
            except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
                raise RemoteError('HTTP transport failed', uncertain=write) from None
        raise RemoteError('HTTP retry limit reached')

HTTPTransport = Transport
