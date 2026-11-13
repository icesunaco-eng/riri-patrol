"""PR #4 再レビューの留保への追加修正：人間側 held 通知の遅れ（通信なし・偽の起動関数）。

必須2件（tests/test_local_noa_review_r2.py）とは別の変更で、単独で取り消せる。
"""
import unittest

from riri.noa import NoaUncertain
from tests.test_local_noa import FakePopen
from tests.test_local_noa_review_r import InstructionBase


class HeldNoticeIsNotDelayed(InstructionBase):
    """追加修正：人間側の held 通知は、生成指示の処理（送信・1時間の上限）より先に出す。"""

    def test_notice_is_queued_in_the_patrol_that_holds_the_old_sending(self):
        a = self.old_sending_human(7)
        self.source(1)
        popen = FakePopen()
        self.run_with(self.use_local(popen))
        self.assertEqual(a["status"], "held")
        self.assertEqual(len(self.human_notices()), 1)
        self.assertEqual(len(popen.calls), 1)  # the instruction is still sent, A is not

    def test_continuous_instructions_and_hourly_limit_do_not_hide_it(self):
        popen = FakePopen()
        for n in range(1, 13):  # an instruction on every patrol, past the 10/hour limit
            self.source(n)
            self.run_with(self.use_local(popen))
            self.clock.advance(60)
        a = self.old_sending_human(99)
        self.source(13)
        before = len(popen.calls)
        self.run_with(self.use_local(popen))
        self.assertEqual(len(popen.calls), before)  # limit reached: nothing sent
        self.assertEqual(a["status"], "held")
        self.assertEqual(len(self.human_notices()), 1)
        for n in range(14, 17):
            self.source(n)
            self.run_with(self.use_local(popen))
            self.clock.advance(60)
        self.assertEqual(len(self.human_notices()), 1)  # queued once, never duplicated

    def test_already_held_human_item_is_noticed_while_instructions_keep_coming(self):
        self.client.responses = [NoaUncertain()]
        self.message(3)
        self.run_with(self.use_routine())
        key = [k for k in self.p.state["outbox"] if k.startswith("noa-notice:")][0]
        del self.p.state["outbox"][key]; self.p.save()  # as if held before any notice existed
        self.clock.advance(1)
        self.source(1)
        self.run_with(self.use_routine())
        self.assertIn(key, self.p.state["outbox"])


if __name__ == "__main__":
    unittest.main()
