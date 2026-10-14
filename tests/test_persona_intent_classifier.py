"""破坏性人设分类必须使用精确状态值。"""

import asyncio

from agent.handler import MessageHandler


class _FakeHandler:
    def __init__(self, response):
        self.response = response

    async def _call_llm_light(self, **_kwargs):
        return self.response


def _classify(response):
    return asyncio.run(MessageHandler._classify_persona_intent(
        _FakeHandler(response), "原消息", "候选人设"
    ))


def test_persona_intent_accepts_only_exact_protocol_values():
    assert _classify("A") == "permanent"
    assert _classify("B") == "roleplay"
    assert _classify("C") == "joke"


def test_verbose_classifier_output_fails_closed():
    assert _classify("Answer: C") == "joke"
    assert _classify("analysis says B") == "joke"
