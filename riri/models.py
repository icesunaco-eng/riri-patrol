from dataclasses import dataclass, field

RECIPIENTS = ("あいす", "のあ", "かれん", "みに", "うさ", "りり", "まむ")
REPORT_PAGES = {
    "あいす": "https://app.notion.com/p/413f1dff94dc819996a8fd6674e258ae",
    "のあ": "https://app.notion.com/p/413f1dff94dc8120bfa6d9ba516c56bb",
    "みに": "https://app.notion.com/p/413f1dff94dc810dbec6c97de7653f30",
    "かれん": "https://app.notion.com/p/413f1dff94dc811e9d1fc7808d2cea3f",
    "うさ": "https://app.notion.com/p/413f1dff94dc81c492f6ddc54aa45dd5",
    "まむ": "https://app.notion.com/p/417f1dff94dc81f7b471dcf8a05e9c43",
}


@dataclass
class Request:
    id: str
    requester: str
    recipients: list[str]
    recipients_source: str
    status: str
    block_id: str
    text: str
    fields: dict[str, str] = field(default_factory=dict)
    block_type: str = "code"


@dataclass
class Report:
    id: str
    reporter: str
    result: str
    summary: str
    block_id: str
    text: str
    digest: str
    work_url: str = ''
    work_url_status: str = 'missing'
    work_url_reason: str = 'missing'
    work_url_legacy: bool = False
    work_url_superseded: bool = False
