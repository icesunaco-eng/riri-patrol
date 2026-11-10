import unittest
from riri.compose import instruction
from riri.errors import SafetyError
from riri.llm import DraftService, validate_draft
from riri.parse import parse_requests
from tests.test_parse import REQUEST, block

class Fake:
    def __init__(self, values): self.values, self.calls = list(values), []
    def request(self, *args, **kwargs):
        self.calls.append((args,kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception): raise value
        return {'message': {'content':value}}

class LLMTests(unittest.TestCase):
    def setUp(self):
        self.req = parse_requests([block(REQUEST)])[0]
        self.names = self.req.recipients
        self.good = instruction(self.req, self.names)
    def test_each_mandatory_line_missing_fails(self):
        self.assertTrue(validate_draft(self.good,self.req,self.names))
        lines = self.good.splitlines()
        for i in range(len(lines)-1):
            self.assertFalse(validate_draft('\n'.join(lines[:i]+lines[i+1:]), self.req,self.names))
    def test_changed_values_append_injection_and_new_information_fail(self):
        for bad in [self.good.replace('今日中','今日中ではなく明日'), self.good+'\nhttps://evil.example', self.good.replace('確認する','削除する'), self.good.replace('💛20261111-1','💛20261111-2'), self.good+'\n<@U123>', self.good+' xoxb-secret', self.good.replace('@あいす @かれん','@あいす @かれん @みに')]:
            self.assertFalse(validate_draft(bad,self.req,self.names))
    def test_ollama_parameters_and_wrapping(self):
        fake = Fake(['<think>thinking</think>\n```text\n'+self.good+'\n```'])
        self.assertEqual(DraftService(fake).draft(self.req,self.names),(self.good,'LLM'))
        args, kwargs = fake.calls[0]
        self.assertEqual(args,('POST','http://127.0.0.1:11434/api/chat'))
        self.assertEqual(kwargs['timeout'],90)
        self.assertFalse(kwargs['payload']['stream'])
        self.assertFalse(kwargs['payload']['think'])
        self.assertNotIn('tools',kwargs['payload'])
        self.assertEqual(kwargs['payload']['options']['num_ctx'],32768)
    def test_retry_limit_and_network_immediate_fallback(self):
        for values, count in [(['bad','bad'],2),([OSError('secret remote body')],1)]:
            fake = Fake(values)
            self.assertEqual(DraftService(fake).draft(self.req,self.names),(self.good,'型'))
            self.assertEqual(len(fake.calls),count)
    def test_known_secret_input_never_transmitted(self):
        fake = Fake([])
        self.req.fields['手順'] = 'private-value'
        with self.assertRaises(SafetyError): DraftService(fake,secrets=['private-value']).draft(self.req,self.names)
        self.assertEqual(fake.calls,[])
    def test_disabled_and_remote_endpoint_rejected(self):
        fake = Fake([])
        self.assertEqual(DraftService(fake,enabled=False).draft(self.req,self.names),(self.good,'型'))
        for url in ['https://evil.example','http://127.0.0.1.evil.example:11434','http://user@127.0.0.1:11434','http://127.0.0.1:11434/evil']:
            with self.assertRaises(SafetyError): DraftService(fake,endpoint=url)
if __name__ == '__main__': unittest.main()
