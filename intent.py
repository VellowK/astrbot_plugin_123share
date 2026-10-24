"""Deterministic Chinese intent parsing; never send chat content to an LLM."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class Intent:
    action: str
    payload: str = ""
    explicit: bool = False


PREFIX = r"(?:(?:请|麻烦|劳驾|能不能|可以|能|请你|请帮忙|帮忙|帮我|帮我把|帮我将|帮我来|给我|替我|帮我来个|帮我来一下)\s*)*"
SHARE = re.compile(r"^" + PREFIX + r"(?:分享|上传|快传|发送)(?:\s*(?:一下|下))?", re.I)
RECEIVE = re.compile(r"^" + PREFIX + r"(?:提取|领取|接收|下载|取)(?:\s*(?:一下|下))?", re.I)
REFERENCE = re.compile(
    r"^(?:(?:这个|这份|这段|这条|这些|该|一份|一段)?\s*(?:文件|文字|文本|内容))(?:\s*(?:和|、|，|/|及)\s*(?:这个|这份|这段)?\s*(?:文件|文字|文本|内容))*$"
)
CODE = re.compile(r"(?<![0-9A-Za-z])[0-9]{4}(?![0-9A-Za-z])")


def extract_code(text: str, service_url: str = "https://123share.cn") -> str | None:
    """Accept one unambiguous four-digit code, or an actual service share URL."""
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    urls = re.findall(r"https?://[^\s<>\"'，。]+", text)
    if urls:
        host = urlsplit(service_url).hostname
        values = []
        for url in urls:
            parsed = urlsplit(url)
            if parsed.hostname != host or parsed.username or parsed.password:
                return None
            values.extend(parse_qs(parsed.query).get("code", []))
        unique = set(values)
        if len(unique) == 1 and re.fullmatch(r"[0-9]{4}", values[0]):
            return values[0]
        return None
    codes = set(CODE.findall(text))
    return next(iter(codes)) if len(codes) == 1 else None


def _payload(text: str) -> str:
    text = text.lstrip(" \t，,:：")
    # A reference is an instruction, not the text to upload. Preserve everything
    # after the first colon/newline, including its original whitespace.
    header, sep, tail = (
        re.split(r"([:：\n])", text, maxsplit=1) if re.search(r"[:：\n]", text) else (text, "", "")
    )
    header = re.sub(r"(?:吧|呀|啊|好吗|谢谢|一下)$", "", header.strip()).strip()
    if REFERENCE.fullmatch(header) or header in {"", "它", "这个", "这份", "这段"}:
        return tail if sep else ""
    return text


def parse_intent(text: str) -> Intent | None:
    """Match a leading request, not a verb buried in ordinary conversation."""
    stripped = text.strip()
    command = re.match(r"^/?123share(?:\s+|$)(.*)$", stripped, re.I | re.S)
    if command:
        rest = command[1].strip()
        if rest.lower() in {"", "help", "帮助"}:
            return Intent("help", explicit=True)
        if rest.lower() in {"cancel", "取消"}:
            return Intent("cancel", explicit=True)
        share = re.match(r"^(?:分享|上传|share|send)(?:\s+|[:：]|$)(.*)$", rest, re.I | re.S)
        if share:
            return Intent("share", share[1], True)
        receive = re.match(
            r"^(?:取|取件|提取|接收|领取|get|receive)(?:\s+|[:：]|$)(.*)$", rest, re.I | re.S
        )
        if receive:
            return Intent("receive", receive[1], True)
        return Intent("help", explicit=True)
    if stripped in {"取消快传", "取消分享", "取消取件"}:
        return Intent("cancel")
    inverted = re.match(
        r"^"
        + PREFIX
        + r"(?:把|将)?\s*(?:(?:这个|这份|这段|这条|这些|该)\s*)?"
        + r"(?:文件|文字|文本|内容)\s*(?:(?:请)?帮我\s*)?"
        + r"(?:分享|上传|快传)(?:\s*(?:一下|下))?(?:吧|呀|啊)?(?P<tail>.*)$",
        stripped,
        re.S,
    )
    if inverted:
        return Intent("share", _payload(inverted["tail"]))
    share = SHARE.match(stripped)
    if share:
        return Intent("share", _payload(stripped[share.end() :]))
    receive = RECEIVE.match(stripped)
    if receive:
        tail = stripped[receive.end() :].lstrip(" \t，,:：")
        # Do not mistake '取快递', '下载音乐', etc. for a transfer request.
        if (
            not tail
            or REFERENCE.fullmatch(tail.rstrip("吧呀啊。！! "))
            or re.search(r"[0-9０-９]|https?://|取件码|提取码|接收码", tail)
        ):
            return Intent("receive", tail)
    return None
