"""Single validated write path: practice suppresses only remote mutations."""
from .eventlog import iso


class PracticeWriter:
    dry_run = True

    def __init__(self, slack, notion, logs, clock):
        self.slack, self.notion, self.logs, self.clock = slack, notion, logs, clock

    def slack_post(self, channel, text, key, kind="notice"):
        if hasattr(self.slack, "wants_report_user"):
            ts = self.slack.post(channel, text, dry_run=self.dry_run, kind=kind)
        else:  # Offline legacy adapters preserve the existing interface.
            ts = self.slack.post(channel, text, dry_run=self.dry_run)
        if not self.dry_run:
            return ts
        self.logs.practice({"at": iso(self.clock()), "operation": "slack_post", "channel": channel, "text": text, "key": key})
        return "practice-" + key

    def _audit(self, entry):
        self.logs.audit(dict(entry, at=iso(self.clock())))

    def set_status(self, req, status, *, practice_status=None):
        self.notion.set_status(req.block_id, req.text, status, dry_run=self.dry_run,
                               audit=self._audit,
                               expected_last_edited_time=getattr(req, 'last_edited_time', None),
                               practice_status=practice_status)
        if self.dry_run:
            self.logs.practice({"at": iso(self.clock()), "operation": "notion_status", "block_id": req.block_id,
                                "request_id": req.id, "status": status, "practice_status": practice_status})

    def append_log(self, page, table, header, events, rows):
        self.notion.append_rows(table, header, rows, dry_run=self.dry_run)
        if self.dry_run:
            self.logs.practice({"at": iso(self.clock()), "operation": "notion_log", "page_id": page,
                                "table_id": table, "header_id": header, "events": events, "rows": rows})


class LiveWriter(PracticeWriter):
    dry_run = False
