from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.utils.session_waiter import (
    FILTERS,
    DefaultSessionFilter,
    SessionWaiter,
)


VIDEO_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
VIDEO_PAGE_LIST_API = "https://api.bilibili.com/x/player/pagelist"
VIDEO_PLAYER_API = "https://api.bilibili.com/x/player/wbi/v2"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
DEFAULT_REQUEST_INTERVAL = 1.5
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_MAX_CONCURRENT_JOBS = 1
MESSAGE_CHUNK_SIZE = 3500
CHINA_TIMEZONE = timezone(timedelta(hours=8))

DEFAULT_SUMMARY_PROMPT = (
    "请根据以下 Bilibili 视频信息和全部字幕生成总结。\n\n"
    "用户额外要求：{{user_request}}\n\n"
    "视频信息：\n{{video_metadata}}\n\n"
    "视频分P：\n{{video_parts}}\n\n"
    "全部字幕：\n{{subtitles}}\n\n"
    "请用中文输出视频主题、主要内容、重要结论和待办事项，使用清晰的小标题。"
    "必须以字幕和视频信息为依据，不要补写没有依据的事实，也不要逐句复述字幕。"
)
PROMPT_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z][a-zA-Z0-9_]*)\}\}")
PROMPT_TEMPLATE_MARKER_RE = re.compile(
    r"(?:请\s*)?(?:使用|按照|根据|采用|按|用)\s*"
    r"(?:以下|下面|这个|该)?\s*(?:自定义)?"
    r"(?:提示词模板|总结模板|模板)\s*(?:来|进行)?\s*"
    r"(?:总结)?\s*[：:]\s*(?P<template>.+)",
    re.IGNORECASE | re.DOTALL,
)
PROMPT_TEMPLATE_SIMPLE_MARKER_RE = re.compile(
    r"(?:^|[\n。；])\s*(?:提示词模板|总结模板|模板)\s*"
    r"(?:是|为|如下)?\s*[：:]\s*(?P<template>.+)",
    re.IGNORECASE | re.DOTALL,
)
PROMPT_TEMPLATE_FENCE_RE = re.compile(r"```(?:[^\n`]*)\n?(?P<template>.*?)```", re.DOTALL)
MAX_PROMPT_TEMPLATE_LENGTH = 8000
MAX_CONFIGURED_PROMPT_TEMPLATES = 30
MAX_PROMPT_TEMPLATE_NAME_LENGTH = 100
FOLLOWUP_TIMEOUT_SECONDS = 30 * 60
FOLLOWUP_EXIT_PHRASES = frozenset(
    {
        "结束追问",
        "结束提问",
        "清除视频上下文",
        "忘记这个视频",
        "退出追问",
    }
)

# Bilibili's documented WBI mixin-key permutation.
WBI_MIXIN_KEY_TABLE = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class BilibiliError(RuntimeError):
    """Raised when a Bilibili request or response cannot be processed."""


class BilibiliApiError(BilibiliError):
    """Raised when a Bilibili API returns a non-zero response code."""


class SubtitleExtractionError(BilibiliError):
    """Raised when a video has no usable subtitle track."""


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_timestamp(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    try:
        parsed_date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        return int(parsed_date.timestamp())
    except ValueError:
        return None


def clean_title(value: Any) -> str:
    title = str(value or "未知标题")
    return re.sub(r"<[^>]*>", "", title).strip() or "未知标题"


def clean_description(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def format_timestamp(value: Any) -> str:
    timestamp = parse_timestamp(value)
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=CHINA_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S (UTC+08:00)"
    )


def format_duration(value: Any) -> str:
    seconds = _to_int(value)
    if seconds <= 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}小时")
    if minutes or hours:
        parts.append(f"{minutes}分")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def parse_video_identifier(value: Any) -> str | None:
    """Extract a BVID or normalized AID from common Bilibili inputs."""
    text = str(value or "").strip()
    if not text:
        return None
    direct_match = re.fullmatch(r"(BV[a-zA-Z0-9]{10})", text)
    if direct_match:
        return direct_match.group(1)
    aid_match = re.fullmatch(r"(?:av)?(\d+)", text, re.IGNORECASE)
    if aid_match:
        return f"av{aid_match.group(1)}"

    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host.endswith("bilibili.com"):
        path_match = re.search(
            r"/video/(BV[a-zA-Z0-9]{10}|av\d+)", parsed.path, re.IGNORECASE
        )
        if path_match:
            identifier = path_match.group(1)
            return identifier if identifier.upper().startswith("BV") else identifier.lower()
        query = parse_qs(parsed.query)
        bvid = query.get("bvid", [""])[0]
        if re.fullmatch(r"BV[a-zA-Z0-9]{10}", bvid):
            return bvid
        aid = query.get("aid", [""])[0]
        if aid.isdigit():
            return f"av{aid}"

    search_match = re.search(r"(BV[a-zA-Z0-9]{10}|av\d+)", text, re.IGNORECASE)
    if search_match:
        identifier = search_match.group(1)
        return identifier if identifier.upper().startswith("BV") else identifier.lower()
    return None


def is_short_video_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    return (parsed.hostname or "").lower() in {"b23.tv", "www.b23.tv"}


def normalize_cookie_header(value: Any) -> str:
    """Accept either a bare SESSDATA value or a complete Cookie string."""
    text = str(value or "").replace("\r", "").replace("\n", "").strip()
    if not text:
        return ""
    parts = [part.strip() for part in text.split(";") if part.strip()]
    cookie_parts = [part for part in parts if "=" in part]
    has_sessdata = any(
        part.split("=", 1)[0].strip().lower() == "sessdata"
        for part in cookie_parts
    )
    if has_sessdata:
        return "; ".join(cookie_parts)
    return f"SESSDATA={parts[0]}"


def subtitle_body_to_text(body: Any) -> str:
    if not isinstance(body, list):
        return ""
    lines: list[str] = []
    previous = ""
    for item in body:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").replace("\r", "").strip()
        if not content or content == previous:
            continue
        lines.append(content)
        previous = content
    return "\n".join(lines)


def split_message(text: str, limit: int = MESSAGE_CHUNK_SIZE) -> list[str]:
    """Split outgoing messages without losing text."""
    text = str(text or "").strip()
    if not text:
        return []
    if limit <= 0:
        raise ValueError("message limit must be positive")
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit + 1)
        if split_at <= limit // 2:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


@dataclass(frozen=True)
class SubtitleTrack:
    language: str
    text: str


@dataclass(frozen=True)
class SubtitlePage:
    page: int
    cid: int
    part: str
    tracks: tuple[SubtitleTrack, ...]
    duration_seconds: int | None = None
    subtitle_error: str = ""


@dataclass(frozen=True)
class VideoTranscript:
    bvid: str
    aid: int
    title: str
    pages: tuple[SubtitlePage, ...]
    description: str = ""
    published_at: int | None = None
    duration_seconds: int | None = None

    @property
    def subtitle_count(self) -> int:
        return sum(len(page.tracks) for page in self.pages)


@dataclass(frozen=True)
class FollowupContext:
    transcript: VideoTranscript
    summary: str


ProgressCallback = Callable[[int, int], Awaitable[None]]


class BilibiliClient:
    """Asynchronous client for Bilibili video metadata and subtitles."""

    def __init__(
        self,
        sessdata: str,
        request_interval: float = DEFAULT_REQUEST_INTERVAL,
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.cookie_header = normalize_cookie_header(sessdata)
        self.request_interval = max(0.0, request_interval)
        self.timeout_seconds = max(1, timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._request_lock: asyncio.Lock | None = None
        self._last_request_at = 0.0
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_keys_fetched_at = 0.0

    async def open(self) -> None:
        if self._session and not self._session.closed:
            return
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bilibili.com/",
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _wait_for_request_slot(self) -> None:
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
        async with self._request_lock:
            delay = self._last_request_at + self.request_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()

    async def _request_payload(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        envelope: bool = True,
    ) -> Any:
        await self.open()
        request_params = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        if signed:
            request_params = await self._signed_params(request_params)
        await self._wait_for_request_slot()
        assert self._session is not None
        try:
            async with self._session.get(url, params=request_params) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise BilibiliError(f"请求 Bilibili 失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise BilibiliError("Bilibili 返回格式无效")
        if not envelope:
            return payload
        if "code" not in payload:
            return payload
        if payload.get("code") != 0:
            message = str(payload.get("message") or payload.get("msg") or "未知错误")
            raise BilibiliApiError(
                f"Bilibili API 错误：{payload.get('code')}：{message}"
            )
        return payload.get("data") or {}

    async def _get_wbi_keys(self) -> tuple[str, str] | None:
        if self._wbi_keys and time.monotonic() - self._wbi_keys_fetched_at < 3600:
            return self._wbi_keys
        try:
            payload = await self._request_payload(NAV_API, envelope=False)
        except BilibiliError:
            self._wbi_keys = None
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        wbi_img = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi_img, dict):
            return None
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        if not img_key or not sub_key:
            return None
        self._wbi_keys = (img_key, sub_key)
        self._wbi_keys_fetched_at = time.monotonic()
        return self._wbi_keys

    async def _signed_params(self, params: dict[str, Any]) -> dict[str, Any]:
        keys = await self._get_wbi_keys()
        if not keys:
            return params
        mixin_source = keys[0] + keys[1]
        mixin_key = "".join(
            mixin_source[index]
            for index in WBI_MIXIN_KEY_TABLE
            if index < len(mixin_source)
        )[:32]
        signed = dict(params)
        signed["wts"] = int(time.time())
        query_params = {
            key: re.sub(r"[!'()*]", "", str(value))
            for key, value in sorted(signed.items())
        }
        query = urlencode(query_params)
        signed["w_rid"] = hashlib.md5(
            (query + mixin_key).encode("utf-8")
        ).hexdigest()
        return signed

    @staticmethod
    def _parse_pages(raw_pages: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_pages, list):
            return []
        pages: list[dict[str, Any]] = []
        for index, raw_page in enumerate(raw_pages, start=1):
            if not isinstance(raw_page, dict):
                continue
            page = _to_int(raw_page.get("page"), index)
            cid = _to_int(raw_page.get("cid"))
            if cid:
                pages.append(
                    {
                        "page": page,
                        "cid": cid,
                        "part": clean_title(raw_page.get("part") or f"第 {page} P"),
                        "duration_seconds": _to_int(raw_page.get("duration")) or None,
                    }
                )
        return pages

    @staticmethod
    def _subtitle_url(value: Any) -> str:
        url = str(value or "").strip()
        if url.startswith("//"):
            return "https:" + url
        if url and not url.startswith(("http://", "https://")):
            return "https://" + url.lstrip("/")
        return url

    async def _get_subtitle_tracks(
        self,
        aid: int,
        page: dict[str, Any],
    ) -> tuple[SubtitleTrack, ...]:
        player_data = await self._request_payload(
            VIDEO_PLAYER_API,
            {"aid": aid, "cid": page["cid"]},
            signed=True,
        )
        if not isinstance(player_data, dict):
            raise SubtitleExtractionError(f"第 {page['page']} P 字幕响应格式无效")
        subtitle_info = player_data.get("subtitle") or {}
        subtitles = subtitle_info.get("subtitles", [])
        if not isinstance(subtitles, list) or not subtitles:
            raise SubtitleExtractionError(f"第 {page['page']} P 没有可用字幕")

        tracks: list[SubtitleTrack] = []
        seen_texts: set[str] = set()
        for index, raw_subtitle in enumerate(subtitles, start=1):
            if not isinstance(raw_subtitle, dict):
                continue
            subtitle_url = self._subtitle_url(raw_subtitle.get("subtitle_url"))
            if not subtitle_url:
                continue
            payload = await self._request_payload(subtitle_url, envelope=False)
            text = subtitle_body_to_text(payload.get("body"))
            if not text or text in seen_texts:
                continue
            seen_texts.add(text)
            language = str(
                raw_subtitle.get("lan_doc")
                or raw_subtitle.get("lan")
                or f"字幕 {index}"
            ).strip()
            tracks.append(SubtitleTrack(language=language, text=text))
        if not tracks:
            raise SubtitleExtractionError(f"第 {page['page']} P 字幕内容为空")
        return tuple(tracks)

    async def resolve_short_url(self, short_url: str) -> str | None:
        await self.open()
        await self._wait_for_request_slot()
        assert self._session is not None
        try:
            async with self._session.get(short_url, allow_redirects=True) as response:
                response.raise_for_status()
                return parse_video_identifier(str(response.url))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BilibiliError(f"解析 Bilibili 短链接失败：{exc}") from exc

    async def get_video_transcript(
        self,
        video_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> VideoTranscript:
        """Fetch metadata and every available subtitle track for every page."""
        normalized_id = parse_video_identifier(video_id) or str(video_id).strip()
        is_aid = bool(re.fullmatch(r"av\d+", normalized_id, re.IGNORECASE))
        view_data = await self._request_payload(
            VIDEO_VIEW_API,
            {"aid": normalized_id[2:]} if is_aid else {"bvid": normalized_id},
        )
        if not isinstance(view_data, dict):
            raise BilibiliError("视频详情响应格式无效")
        aid = _to_int(view_data.get("aid"))
        if not aid:
            raise BilibiliError(f"视频 {normalized_id} 缺少 AID")
        bvid = str(view_data.get("bvid") or "").strip()
        if not bvid and not is_aid:
            bvid = normalized_id
        if not bvid:
            raise BilibiliError(f"视频 {normalized_id} 缺少 BVID")

        pages = self._parse_pages(view_data.get("pages"))
        if not pages:
            page_data = await self._request_payload(
                VIDEO_PAGE_LIST_API,
                {"bvid": bvid},
            )
            pages = self._parse_pages(page_data)
        if not pages:
            raise BilibiliError(f"视频 {bvid} 没有可用分P")

        transcript_pages: list[SubtitlePage] = []
        for index, page in enumerate(pages, start=1):
            tracks: tuple[SubtitleTrack, ...] = ()
            subtitle_error = ""
            try:
                tracks = await self._get_subtitle_tracks(aid, page)
            except SubtitleExtractionError as exc:
                subtitle_error = str(exc)
                logger.info("Bilibili 视频 %s：%s", bvid, subtitle_error)
            transcript_pages.append(
                SubtitlePage(
                    page=page["page"],
                    cid=page["cid"],
                    part=page["part"],
                    tracks=tracks,
                    duration_seconds=page["duration_seconds"],
                    subtitle_error=subtitle_error,
                )
            )
            if progress_callback:
                await progress_callback(index, len(pages))

        if not any(page.tracks for page in transcript_pages):
            raise SubtitleExtractionError(f"视频 {bvid} 没有可用字幕")
        page_duration = sum(page["duration_seconds"] or 0 for page in pages)
        view_duration = _to_int(view_data.get("duration"))
        return VideoTranscript(
            bvid=bvid,
            aid=aid,
            title=clean_title(view_data.get("title")),
            pages=tuple(transcript_pages),
            description=clean_description(view_data.get("desc")),
            published_at=parse_timestamp(
                view_data.get("pubdate") or view_data.get("ctime")
            ),
            duration_seconds=max(page_duration, view_duration) or None,
        )


class Main(star.Star):
    """Provide a natural-language Bilibili video summarization tool."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._client: BilibiliClient | None = None
        self._client_signature: tuple[str, float, int] | None = None
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._jobs_lock = asyncio.Lock()
        self._followup_tasks: dict[str, asyncio.Task[None]] = {}
        self._followup_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._migrate_prompt_template_config()
        errors = self._configuration_errors()
        if errors:
            logger.warning(
                "Bilibili 视频总结插件配置待完善：%s", "；".join(errors)
            )
        else:
            logger.info("Bilibili 视频总结自然语言工具已加载。")

    async def _migrate_prompt_template_config(self) -> None:
        """Convert legacy JSON templates to AstrBot's native template_list shape."""
        raw = self._config_value("prompt_templates", None)
        if raw in (None, "", [], {}):
            return
        if isinstance(raw, list) and all(
            isinstance(item, dict)
            and item.get("__template_key") == "summary"
            and str(item.get("name") or "").strip()
            and str(item.get("prompt") or "").strip()
            for item in raw
        ):
            return

        entries = self._configured_prompt_template_entries()
        if not entries:
            return
        normalized = [
            {
                "__template_key": "summary",
                "name": entry["name"],
                "prompt": entry["prompt"],
            }
            for entry in entries
        ]
        self.config["prompt_templates"] = normalized
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                await asyncio.to_thread(save_config)
                logger.info("Bilibili 视频总结旧版模板配置已迁移到 template_list。")
            except Exception as exc:
                logger.warning("迁移 Bilibili 视频总结模板配置失败：%s", exc)

    async def terminate(self) -> None:
        async with self._jobs_lock:
            tasks = list(self._jobs.values())
            self._jobs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._followup_lock:
            followup_tasks = list(self._followup_tasks.values())
            self._followup_tasks.clear()
        for task in followup_tasks:
            task.cancel()
        if followup_tasks:
            await asyncio.gather(*followup_tasks, return_exceptions=True)
        if self._client:
            await self._client.close()
        self._client = None
        self._client_signature = None

    def _config_value(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def _config_int(self, key: str, default: int) -> int:
        return _to_int(self._config_value(key, default), default)

    def _config_float(self, key: str, default: float) -> float:
        try:
            return float(self._config_value(key, default))
        except (TypeError, ValueError):
            return default

    def _configuration_errors(self) -> list[str]:
        errors: list[str] = []
        if not str(self._config_value("provider_id", "")).strip():
            errors.append("provider_id 总结模型未配置")
        request_interval = self._config_float(
            "request_interval_seconds", DEFAULT_REQUEST_INTERVAL
        )
        if not 0 <= request_interval <= 60:
            errors.append("请求间隔必须在 0 到 60 秒之间")
        request_timeout = self._config_int(
            "request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT
        )
        if not 1 <= request_timeout <= 300:
            errors.append("请求超时必须在 1 到 300 秒之间")
        max_jobs = self._config_int(
            "max_concurrent_jobs", DEFAULT_MAX_CONCURRENT_JOBS
        )
        if not 1 <= max_jobs <= 3:
            errors.append("最大并发任务数必须在 1 到 3 之间")
        return errors

    async def _get_client(self) -> BilibiliClient:
        signature = (
            str(self._config_value("sessdata", "")).strip(),
            self._config_float("request_interval_seconds", DEFAULT_REQUEST_INTERVAL),
            self._config_int("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT),
        )
        if self._client and self._client_signature == signature:
            return self._client
        if self._client:
            await self._client.close()
        self._client = BilibiliClient(*signature)
        self._client_signature = signature
        return self._client

    async def _resolve_video_input(self, video_input: str) -> str | None:
        identifier = parse_video_identifier(video_input)
        if identifier:
            return identifier
        if is_short_video_url(video_input):
            return await (await self._get_client()).resolve_short_url(video_input)
        return None

    @staticmethod
    def _video_metadata_text(transcript: VideoTranscript) -> str:
        return "\n".join(
            (
                f"BVID：{transcript.bvid}",
                f"AID：{transcript.aid}",
                f"视频链接：https://www.bilibili.com/video/{transcript.bvid}/",
                f"视频标题：{transcript.title}",
                f"视频简介：{transcript.description or '（未提供）'}",
                f"视频发布时间：{format_timestamp(transcript.published_at) or '（未知）'}",
                f"视频总时长：{format_duration(transcript.duration_seconds) or '（未知）'}",
            )
        )

    @staticmethod
    def _video_parts_text(transcript: VideoTranscript) -> str:
        parts: list[str] = []
        for page in transcript.pages:
            status = f"字幕轨道：{len(page.tracks)}"
            if page.subtitle_error:
                status += f"；{page.subtitle_error}"
            parts.append(
                f"第 {page.page} P：{page.part or '未命名'}；"
                f"CID：{page.cid}；"
                f"时长：{format_duration(page.duration_seconds) or '未知'}；{status}"
            )
        return "\n".join(parts) or "（未提供）"

    @staticmethod
    def _subtitle_text(transcript: VideoTranscript) -> str:
        parts: list[str] = []
        for page in transcript.pages:
            for track in page.tracks:
                parts.append(
                    f"[第 {page.page} P｜{page.part}｜{track.language}]\n{track.text}"
                )
        return "\n\n".join(parts)

    @staticmethod
    def _render_prompt(template: str, values: dict[str, str]) -> str:
        return PROMPT_PLACEHOLDER_RE.sub(
            lambda match: values.get(match.group(1), match.group(0)),
            template,
        )

    @staticmethod
    def _clean_prompt_template(value: Any) -> str:
        template = str(value or "").strip()
        if not template:
            return ""
        fenced = PROMPT_TEMPLATE_FENCE_RE.search(template)
        if fenced:
            template = fenced.group("template").strip()
        return template[:MAX_PROMPT_TEMPLATE_LENGTH]

    def _configured_prompt_template_entries(self) -> list[dict[str, str]]:
        """Read native template_list entries and legacy JSON templates."""
        raw = self._config_value("prompt_templates", "")
        if isinstance(raw, (dict, list)):
            decoded: Any = raw
        else:
            text = str(raw or "").strip()
            if not text:
                return []
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Bilibili 视频总结模板配置不是有效 JSON：%s", exc)
                return []

        candidates: list[tuple[Any, Any]] = []
        if isinstance(decoded, dict):
            candidates.extend(decoded.items())
        elif isinstance(decoded, list):
            for item in decoded:
                if not isinstance(item, dict):
                    continue
                candidates.append(
                    (
                        item.get("name") or item.get("title"),
                        item.get("prompt") or item.get("template"),
                    )
                )
        else:
            logger.warning("Bilibili 视频总结模板配置必须是 JSON 对象或数组")
            return []

        entries: list[dict[str, str]] = []
        for raw_name, raw_prompt in candidates[:MAX_CONFIGURED_PROMPT_TEMPLATES]:
            name = str(raw_name or "").strip()[:MAX_PROMPT_TEMPLATE_NAME_LENGTH]
            prompt = self._clean_prompt_template(raw_prompt)
            if name and prompt:
                entries.append({"name": name, "prompt": prompt})
        return entries

    def _configured_prompt_templates(self) -> dict[str, str]:
        return {
            entry["name"]: entry["prompt"]
            for entry in self._configured_prompt_template_entries()
        }

    async def _save_named_prompt_template(self, name: str, prompt: str) -> str:
        name = str(name or "").strip()[:MAX_PROMPT_TEMPLATE_NAME_LENGTH]
        prompt = self._clean_prompt_template(prompt)
        if not name:
            return "模板名称不能为空。"
        if not prompt:
            return "模板提示词不能为空。"

        entries = self._configured_prompt_template_entries()
        replaced = False
        for entry in entries:
            if entry["name"].casefold() == name.casefold():
                entry["name"] = name
                entry["prompt"] = prompt
                replaced = True
                break
        if not replaced:
            if len(entries) >= MAX_CONFIGURED_PROMPT_TEMPLATES:
                return f"模板数量已达到上限（{MAX_CONFIGURED_PROMPT_TEMPLATES} 个）。"
            entries.append({"name": name, "prompt": prompt})

        saved_entries = [
            {
                "__template_key": "summary",
                "name": entry["name"],
                "prompt": entry["prompt"],
            }
            for entry in entries
        ]
        self.config["prompt_templates"] = saved_entries
        save_config = getattr(self.config, "save_config", None)
        if not callable(save_config):
            return "当前配置对象不支持持久化，请在 WebUI 中手动保存模板。"
        try:
            await asyncio.to_thread(save_config)
        except Exception as exc:
            logger.warning("保存 Bilibili 视频总结模板失败：%s", exc)
            return "模板已写入内存，但保存到配置文件失败，请检查 AstrBot 配置权限。"
        action = "更新" if replaced else "新增"
        return f"已{action}总结模板“{name}”。"

    def _named_prompt_template(
        self,
        request: str,
        templates: dict[str, str],
    ) -> str:
        if not request or not templates:
            return ""
        lowered_request = request.casefold()
        if lowered_request.strip() in {name.casefold() for name in templates}:
            for name, prompt in templates.items():
                if name.casefold() == lowered_request.strip():
                    return prompt
        has_template_word = "模板" in request or "prompt" in lowered_request
        if not has_template_word:
            return ""
        for name in sorted(templates, key=len, reverse=True):
            if name.casefold() in lowered_request:
                return templates[name]
        return ""

    def _resolve_prompt_template(
        self,
        user_request: str,
        explicit_template: str = "",
    ) -> str:
        templates = self._configured_prompt_templates()
        explicit = str(explicit_template or "").strip()
        if explicit:
            named = self._named_prompt_template(explicit, templates)
            return named or self._clean_prompt_template(explicit)

        named = self._named_prompt_template(user_request, templates)
        if named:
            return named
        return self._extract_prompt_template(user_request)

    @classmethod
    def _extract_prompt_template(
        cls,
        user_request: str,
        explicit_template: str = "",
    ) -> str:
        """Find a per-request template without treating every extra request as one."""
        direct = cls._clean_prompt_template(explicit_template)
        if direct:
            return direct

        request = str(user_request or "").strip()
        if not request:
            return ""

        fenced = PROMPT_TEMPLATE_FENCE_RE.search(request)
        if fenced and ("{{" in fenced.group("template") or "模板" in request):
            return cls._clean_prompt_template(fenced.group("template"))

        match = PROMPT_TEMPLATE_MARKER_RE.search(request)
        if not match:
            match = PROMPT_TEMPLATE_SIMPLE_MARKER_RE.search(request)
        if match:
            return cls._clean_prompt_template(match.group("template"))

        # A request containing supported placeholders is already a usable template.
        if "{{subtitles}}" in request or "{{video_" in request:
            return cls._clean_prompt_template(request)
        return ""

    def _summary_prompt(
        self,
        transcript: VideoTranscript,
        subtitles: str,
        user_request: str,
        prompt_template: str = "",
    ) -> str:
        template = self._resolve_prompt_template(user_request, prompt_template)
        if not template:
            template = str(
                self._config_value("summary_prompt", DEFAULT_SUMMARY_PROMPT)
            ).strip() or DEFAULT_SUMMARY_PROMPT
        values = {
            "video_bvid": transcript.bvid,
            "video_aid": str(transcript.aid),
            "video_url": f"https://www.bilibili.com/video/{transcript.bvid}/",
            "video_title": transcript.title,
            "video_description": transcript.description or "（未提供）",
            "video_published_at": format_timestamp(transcript.published_at) or "（未知）",
            "video_duration": format_duration(transcript.duration_seconds) or "（未知）",
            "video_metadata": self._video_metadata_text(transcript),
            "video_parts": self._video_parts_text(transcript),
            "part_count": str(len(transcript.pages)),
            "subtitle_count": str(transcript.subtitle_count),
            "subtitles": subtitles,
            "user_request": user_request.strip() or "（无额外要求）",
            "material_type": "全部原始字幕",
            "chunk_index": "1",
            "chunk_total": "1",
        }
        return self._render_prompt(template, values)

    async def _call_llm(self, prompt: str) -> str:
        response = await self.context.llm_generate(
            chat_provider_id=str(self._config_value("provider_id", "")).strip(),
            prompt=prompt,
        )
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            raise BilibiliError("模型未返回总结内容")
        return text

    async def _summarize(
        self,
        transcript: VideoTranscript,
        user_request: str = "",
        prompt_template: str = "",
    ) -> str:
        """Send every available subtitle to exactly one model request."""
        subtitles = self._subtitle_text(transcript)
        if not subtitles:
            raise SubtitleExtractionError("视频没有可用字幕")
        return await self._call_llm(
            self._summary_prompt(
                transcript,
                subtitles,
                user_request,
                prompt_template,
            )
        )

    def _followup_prompt(
        self,
        followup_context: FollowupContext,
        question: str,
    ) -> str:
        transcript = followup_context.transcript
        return (
            "你正在回答用户对刚刚总结的 Bilibili 视频的追问。\n"
            "请只依据视频信息、分P信息、全部字幕和已有总结回答。\n"
            "如果资料中没有明确答案，请直接说明“视频资料中没有明确说明”，不要编造。\n"
            "回答要直接、清晰，并针对用户的问题，不要重新输出整篇视频总结。\n\n"
            f"【视频信息】\n{self._video_metadata_text(transcript)}\n\n"
            f"【分P信息】\n{self._video_parts_text(transcript)}\n\n"
            f"【已有总结】\n{followup_context.summary}\n\n"
            f"【全部字幕】\n{self._subtitle_text(transcript)}\n\n"
            f"【用户追问】\n{question.strip()}"
        )

    @staticmethod
    def _is_followup_exit(message: str) -> bool:
        return message.strip().casefold() in {
            phrase.casefold() for phrase in FOLLOWUP_EXIT_PHRASES
        }

    @staticmethod
    def _is_new_summary_request(message: str) -> bool:
        text = str(message or "").strip()
        if not (parse_video_identifier(text) or is_short_video_url(text)):
            return False
        return any(
            keyword in text
            for keyword in ("总结", "概括", "分析", "提炼", "复盘", "字幕")
        )

    async def _send_event_text(self, event: AstrMessageEvent, text: str) -> None:
        sender = getattr(event, "send", None)
        plain_result = getattr(event, "plain_result", None)
        if callable(sender) and callable(plain_result):
            await sender(plain_result(text))
            return
        target = str(getattr(event, "unified_msg_origin", "") or "").strip()
        await self._send_text(target, text)

    def _requeue_event(self, event: AstrMessageEvent) -> bool:
        get_event_queue = getattr(self.context, "get_event_queue", None)
        if not callable(get_event_queue):
            return False
        queue = get_event_queue()
        put_nowait = getattr(queue, "put_nowait", None)
        if not callable(put_nowait):
            return False
        put_nowait(copy.copy(event))
        stop_event = getattr(event, "stop_event", None)
        if callable(stop_event):
            stop_event()
        return True

    async def _handle_followup_event(
        self,
        controller: Any,
        event: AstrMessageEvent,
        followup_context: FollowupContext,
    ) -> None:
        question = str(getattr(event, "message_str", "") or "").strip()
        if not question:
            return
        if self._is_followup_exit(question):
            await self._send_event_text(event, "已结束当前视频追问，上下文已清除。")
            controller.stop()
            return

        if self._is_new_summary_request(question):
            controller.stop()
            if not self._requeue_event(event):
                await self._send_event_text(
                    event,
                    "已结束当前视频追问。请重新发送新视频总结请求。",
                )
            return

        try:
            await self._send_event_text(event, "正在根据刚才的视频内容回答你的追问。")
            answer = await self._call_llm(
                self._followup_prompt(followup_context, question)
            )
            await self._send_event_text(event, f"【Bilibili 视频追问】\n{answer}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Bilibili 视频追问失败：%s", exc)
            await self._send_event_text(
                event,
                f"【Bilibili 视频追问】\n回答失败：{self._friendly_error(exc)}",
            )
        controller.keep(FOLLOWUP_TIMEOUT_SECONDS, reset_timeout=True)

    async def _run_followup_waiter(
        self,
        target: str,
        followup_context: FollowupContext,
    ) -> None:
        session_filter = DefaultSessionFilter()
        FILTERS.append(session_filter)
        waiter = SessionWaiter(session_filter, target, record_history_chains=False)

        async def handle_event(controller: Any, event: AstrMessageEvent) -> None:
            await self._handle_followup_event(
                controller,
                event,
                followup_context,
            )

        try:
            await waiter.register_wait(
                handle_event,
                timeout=FOLLOWUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.info("Bilibili 视频追问上下文已过期：%s", target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Bilibili 视频追问会话失败：%s", exc)
        finally:
            with contextlib.suppress(ValueError):
                FILTERS.remove(session_filter)
            current = asyncio.current_task()
            async with self._followup_lock:
                if self._followup_tasks.get(target) is current:
                    self._followup_tasks.pop(target, None)

    async def _replace_followup_context(
        self,
        target: str,
        transcript: VideoTranscript,
        summary: str,
    ) -> None:
        async with self._followup_lock:
            previous = self._followup_tasks.pop(target, None)
        if previous and not previous.done():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)

        task = asyncio.create_task(
            self._run_followup_waiter(
                target,
                FollowupContext(transcript=transcript, summary=summary),
            ),
            name=f"bilibili-video-followup-{target}",
        )
        async with self._followup_lock:
            self._followup_tasks[target] = task
        # Register the SessionWaiter before the final message reaches the user.
        await asyncio.sleep(0)

    async def _send_text(self, target: str, text: str) -> None:
        destination = str(target or "").strip()
        if not destination:
            raise BilibiliError("无法确定当前会话")
        chunks = split_message(text)
        if not chunks:
            raise BilibiliError("不能发送空消息")
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"[{index}/{len(chunks)}]\n" if len(chunks) > 1 else ""
            sent = await self.context.send_message(
                destination,
                MessageChain().message(prefix + chunk),
            )
            if sent is False:
                raise BilibiliError("AstrBot 未找到目标会话对应的平台")

    async def _send_progress(self, target: str, message: str) -> None:
        try:
            await self._send_text(target, f"【Bilibili 视频总结】\n{message}")
        except Exception as exc:
            logger.warning("发送 Bilibili 视频总结进度失败：%s", exc)

    @staticmethod
    def _friendly_error(error: BaseException) -> str:
        text = str(error).strip()
        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "context length",
                "maximum context",
                "too many tokens",
                "504 gateway",
                "gateway time-out",
            )
        ):
            return "全部字幕一次性发送后超过模型上下文或模型网关超时，请更换更大上下文的模型或调整模型服务超时。"
        if "-352" in text or "-412" in text:
            return "Bilibili 触发风控，请在 WebUI 配置有效的 SESSDATA 后重试。"
        return text[:500] or "未知错误"

    async def _run_job(
        self,
        job_key: str,
        target: str,
        video_input: str,
        user_request: str,
        prompt_template: str = "",
    ) -> None:
        try:
            await self._send_progress(target, "已接受请求，正在解析视频链接。")
            video_id = await self._resolve_video_input(video_input)
            if not video_id:
                raise BilibiliError("无法识别 Bilibili 视频链接，请提供 BV、AV 或 b23.tv 视频链接。")
            await self._send_progress(target, f"已识别视频 {video_id}，正在读取所有分P字幕。")

            async def report_progress(current: int, total: int) -> None:
                await self._send_progress(target, f"字幕读取进度：第 {current}/{total} 个分P。")

            transcript = await (await self._get_client()).get_video_transcript(
                video_id,
                progress_callback=report_progress,
            )
            await self._send_progress(
                target,
                f"字幕读取完成：{len(transcript.pages)} 个分P、{transcript.subtitle_count} 条字幕轨道，正在生成总结。",
            )
            summary = await self._summarize(
                transcript,
                user_request,
                prompt_template,
            )
            published = format_timestamp(transcript.published_at)
            header = (
                "【Bilibili 视频总结】\n"
                f"标题：{transcript.title}\n"
                f"BVID：{transcript.bvid}\n"
                f"链接：https://www.bilibili.com/video/{transcript.bvid}/\n"
                f"发布时间：{published or '未知'}\n"
                f"总时长：{format_duration(transcript.duration_seconds) or '未知'}\n"
                f"分P：{len(transcript.pages)}，字幕轨道：{transcript.subtitle_count}\n\n"
            )
            await self._send_text(
                target,
                header
                + summary
                + "\n\n你可以继续直接追问这个视频，追问上下文保留 30 分钟。"
                + "发送“结束追问”即可清除上下文。",
            )
            await self._replace_followup_context(target, transcript, summary)
            logger.info("Bilibili 视频总结已发送：%s", transcript.bvid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Bilibili 视频总结失败：%s", exc)
            await self._send_progress(target, f"处理失败：{self._friendly_error(exc)}")
        finally:
            current = asyncio.current_task()
            async with self._jobs_lock:
                if self._jobs.get(job_key) is current:
                    self._jobs.pop(job_key, None)

    async def _start_job(
        self,
        target: str,
        video_input: str,
        user_request: str,
        prompt_template: str = "",
    ) -> str:
        normalized = parse_video_identifier(video_input) or video_input.strip().lower()
        job_key = f"{target}\0{normalized}"
        async with self._jobs_lock:
            existing = self._jobs.get(job_key)
            if existing and not existing.done():
                return "这个视频正在当前会话中处理，请等待已有任务完成。"
            active_count = sum(not task.done() for task in self._jobs.values())
            max_jobs = self._config_int(
                "max_concurrent_jobs", DEFAULT_MAX_CONCURRENT_JOBS
            )
            if active_count >= max_jobs:
                return "当前视频总结任务已达到并发上限，请稍后再试。"
            self._jobs[job_key] = asyncio.create_task(
                self._run_job(
                    job_key,
                    target,
                    video_input,
                    user_request,
                    prompt_template,
                ),
                name=f"bilibili-video-summary-{normalized}",
            )
        return "已开始在当前会话处理这个 Bilibili 视频，完成后会发送总结。"

    @filter.llm_tool(name="summarize_bilibili_video")
    async def summarize_bilibili_video(
        self,
        event: AstrMessageEvent,
        video_url: str,
        user_request: str = "",
        prompt_template: str = "",
    ) -> str:
        """总结用户明确要求总结的 Bilibili 视频。

        仅当用户明确要求总结、概括、分析或提炼 Bilibili 视频，并提供视频链接时调用。
        不要因为用户单纯分享链接、询问视频信息或讨论 Bilibili 而调用。
        工具会在后台处理，完成后自动把完整总结发送到当前会话。

        Args:
            video_url(string): 用户提供的 Bilibili BV号、AV号、视频链接或 b23.tv 短链接。
            user_request(string): 用户对总结内容或格式的额外要求；如果用户说“使用以下模板”，也将从这里识别本次模板。
            prompt_template(string): 用户明确提供的本次总结模板，可为空。模板支持 {{video_title}}、{{video_parts}}、{{subtitles}} 等占位符。
        """
        if self._config_value("enabled", True) is not True:
            return "Bilibili 视频总结工具当前已关闭。"
        errors = self._configuration_errors()
        if errors:
            return "Bilibili 视频总结配置无效：" + "；".join(errors)
        target = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not target:
            return "无法确定当前会话，暂时不能发送视频总结。"
        if not parse_video_identifier(video_url) and not is_short_video_url(video_url):
            return "无法识别视频链接，请提供 Bilibili BV号、AV号、标准视频链接或 b23.tv 短链接。"
        request_text = str(user_request or "").strip()[:2000]
        template_text = str(prompt_template or "").strip()[:MAX_PROMPT_TEMPLATE_LENGTH]
        return await self._start_job(
            target,
            str(video_url).strip(),
            request_text,
            template_text,
        )

    @filter.llm_tool(name="add_bilibili_summary_template")
    async def add_bilibili_summary_template(
        self,
        event: AstrMessageEvent,
        template_name: str,
        prompt_template: str,
    ) -> str:
        """新增或更新一个 Bilibili 视频总结模板。

        仅当管理员明确要求保存、新增或更新总结模板，并提供模板名称和完整提示词时调用。
        普通用户不应调用此工具；普通的总结请求应调用 summarize_bilibili_video。

        Args:
            template_name(string): 要保存的模板名称，例如技术分析或直播复盘。
            prompt_template(string): 完整提示词模板，可使用 {{video_title}}、{{video_parts}}、{{subtitles}} 等占位符。
        """
        if self._config_value("enabled", True) is not True:
            return "Bilibili 视频总结工具当前已关闭。"
        is_admin = getattr(event, "is_admin", None)
        if not callable(is_admin) or not is_admin():
            return "只有 AstrBot 管理员可以通过自然语言新增或更新总结模板。"
        return await self._save_named_prompt_template(template_name, prompt_template)
