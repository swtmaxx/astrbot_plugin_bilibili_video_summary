from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules and "astrbot.api.event" in sys.modules:
        return

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")

    class StubStar:
        def __init__(self, context, config=None):
            self.context = context

    class StubMessageChain:
        def __init__(self):
            self.chain = []

        def message(self, message: str):
            self.chain.append(message)
            return self

    class StubFilter:
        @staticmethod
        def llm_tool(name=None):
            def decorator(function):
                function.llm_tool_name = name or function.__name__
                return function

            return decorator

    api_module.AstrBotConfig = dict
    api_module.logger = logging.getLogger("test-bilibili-video-summary")
    api_module.star = types.SimpleNamespace(Star=StubStar, Context=object)
    event_module.AstrMessageEvent = object
    event_module.MessageChain = StubMessageChain
    event_module.filter = StubFilter
    astrbot_module.api = api_module
    sys.modules.update(
        {
            "astrbot": astrbot_module,
            "astrbot.api": api_module,
            "astrbot.api.event": event_module,
        }
    )


_install_astrbot_stubs()
PLUGIN_PATH = Path(__file__).parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location(
    "bilibili_video_summary_test_module", PLUGIN_PATH
)
assert SPEC and SPEC.loader
main = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = main
SPEC.loader.exec_module(main)


def _make_main(config: dict | None = None, context=None):
    return main.Main(
        context or SimpleNamespace(),
        config or {"provider_id": "provider-1"},
    )


def _transcript(text: str = "字幕内容") -> main.VideoTranscript:
    return main.VideoTranscript(
        bvid="BV1xx411c7mD",
        aid=7,
        title="测试视频",
        pages=(
            main.SubtitlePage(
                page=1,
                cid=101,
                part="第一部分",
                tracks=(main.SubtitleTrack("中文", text),),
                duration_seconds=60,
            ),
        ),
        description="视频简介",
        published_at=1_700_000_000,
        duration_seconds=60,
    )


def test_parse_video_identifier_supports_common_inputs():
    assert main.parse_video_identifier("BV1xx411c7mD") == "BV1xx411c7mD"
    assert main.parse_video_identifier("av12345") == "av12345"
    assert (
        main.parse_video_identifier(
            "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
        )
        == "BV1xx411c7mD"
    )
    assert main.is_short_video_url("https://b23.tv/abc123") is True


def test_cookie_config_accepts_bare_sessdata_or_full_cookie():
    assert main.normalize_cookie_header("abc%2C123") == "SESSDATA=abc%2C123"
    full_cookie = "SESSDATA=abc%2C123; bili_jct=csrf; DedeUserID=42"
    assert main.normalize_cookie_header(full_cookie) == full_cookie


def test_prompt_contains_all_metadata_and_full_subtitles():
    plugin = _make_main()
    transcript = _transcript("第一句\n第二句")

    prompt = plugin._summary_prompt(transcript, "第一句\n第二句", "提取技术重点")

    assert "测试视频" in prompt
    assert "视频简介" in prompt
    assert "2023-11-15 06:13:20" in prompt
    assert "提取技术重点" in prompt
    assert "第一句\n第二句" in prompt
    assert "{{subtitles}}" not in prompt


def test_plain_request_keeps_the_default_generic_template():
    plugin = _make_main()
    transcript = _transcript()

    prompt = plugin._summary_prompt(transcript, "字幕内容", "重点关注技术结论")

    assert "请根据以下 Bilibili 视频信息和全部字幕生成总结" in prompt
    assert "重点关注技术结论" in prompt


def test_natural_language_template_overrides_default_and_renders_placeholders():
    plugin = _make_main()
    transcript = _transcript("完整字幕")

    prompt = plugin._summary_prompt(
        transcript,
        "完整字幕",
        "请使用以下模板总结：\n标题：{{video_title}}\n内容：{{subtitles}}",
    )

    assert prompt == "标题：测试视频\n内容：完整字幕"
    assert "请根据以下 Bilibili 视频信息和全部字幕生成总结" not in prompt


def test_explicit_prompt_template_argument_overrides_natural_language():
    plugin = _make_main()
    transcript = _transcript()

    prompt = plugin._summary_prompt(
        transcript,
        "字幕内容",
        "请重点分析技术细节",
        "只输出：{{video_title}} / {{part_count}}P / {{subtitles}}",
    )

    assert prompt == "只输出：测试视频 / 1P / 字幕内容"


@pytest.mark.asyncio
async def test_summarize_sends_all_subtitles_in_one_llm_request():
    class FakeContext:
        def __init__(self):
            self.calls = []

        async def llm_generate(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(completion_text="总结")

    context = FakeContext()
    plugin = _make_main(context=context)
    transcript = _transcript("x" * 30_000)

    result = await plugin._summarize(transcript, "只保留关键结论")

    assert result == "总结"
    assert len(context.calls) == 1
    assert "x" * 30_000 in context.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_transcript_keeps_pages_without_subtitles_and_reports_progress():
    client = main.BilibiliClient("", request_interval=0)
    client._request_payload = AsyncMock(
        side_effect=[
            {
                "aid": 7,
                "bvid": "BVmulti",
                "title": "多P视频",
                "desc": "简介",
                "pubdate": 1_700_000_000,
                "duration": 180,
                "pages": [
                    {"page": 1, "cid": 101, "part": "有字幕", "duration": 60},
                    {"page": 2, "cid": 102, "part": "无字幕", "duration": 120},
                ],
            },
            {"subtitle": {"subtitles": [{"lan_doc": "中文", "subtitle_url": "//s1"}]}},
            {"body": [{"content": "第一P字幕"}]},
            {"subtitle": {"subtitles": []}},
        ]
    )
    progress = []

    async def record_progress(current, total):
        progress.append((current, total))

    transcript = await client.get_video_transcript(
        "BVmulti",
        progress_callback=record_progress,
    )

    assert len(transcript.pages) == 2
    assert transcript.subtitle_count == 1
    assert transcript.pages[0].tracks[0].text == "第一P字幕"
    assert transcript.pages[1].subtitle_error
    assert progress == [(1, 2), (2, 2)]


@pytest.mark.asyncio
async def test_job_sends_final_summary_to_current_session():
    send_message = AsyncMock(return_value=True)
    context = SimpleNamespace(send_message=send_message)
    plugin = _make_main(context=context)
    plugin._get_client = AsyncMock()
    plugin._get_client.return_value.get_video_transcript = AsyncMock(
        return_value=_transcript()
    )
    plugin._summarize = AsyncMock(return_value="最终总结")
    plugin._send_progress = AsyncMock()

    await plugin._run_job("job", "platform:group", "BV1xx411c7mD", "")

    assert send_message.await_args.args[0] == "platform:group"
    assert "最终总结" in send_message.await_args.args[1].chain[0]
    assert "job" not in plugin._jobs


def test_friendly_error_explains_model_gateway_timeout():
    message = main.Main._friendly_error(RuntimeError("504 Gateway Time-out"))
    assert "模型上下文或模型网关超时" in message


def test_no_chat_command_is_registered():
    assert not hasattr(main.Main, "bili_summary_video")
    assert getattr(main.Main.summarize_bilibili_video, "llm_tool_name") == (
        "summarize_bilibili_video"
    )
