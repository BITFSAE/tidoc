"""全国增值税发票查验平台的验证码识别。

官网的验证码由 120×50 的图片和一行提示组成：有时要求填写全部文字，有时只填写
图中蓝色或红色文字。这里先用提示把目标颜色的像素分离出来，再把原图和分离图
一起交给用户已配置的视觉模型。模型只负责读字，取色、放大和结果校验都在本机
完成。
"""

from __future__ import annotations

import base64
import io
import json
import re
from urllib.error import HTTPError, URLError

from PIL import Image, ImageFilter

from . import payment_ai

PROMPT_VERSION = "captcha-1"
MAX_TEXT_LENGTH = 8
# 允许汉字、英文字母和数字；字母不区分大小写，统一按小写提交。
_TEXT_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]+")
_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", re.S)


def mode_from_hint(hint: str) -> str:
    """把官网提示归为一种取色方式。"""
    if "蓝色" in hint:
        return "blue"
    if "红色" in hint:
        return "red"
    return "all"


def _decode_image(value: bytes | str) -> Image.Image:
    if isinstance(value, str):
        match = _DATA_URL_RE.match(value.strip())
        if not match:
            raise ValueError("验证码图片格式有误")
        value = base64.b64decode(match.group(1), validate=True)
    try:
        image = Image.open(io.BytesIO(value))
        image.load()
    except Exception:
        raise ValueError("验证码图片格式有误") from None
    if image.width < 40 or image.height < 20 or image.width > 2000 or image.height > 2000:
        raise ValueError("验证码图片尺寸有误")
    return image.convert("RGB")


def _is_mode_color(r: int, g: int, b: int, mode: str) -> bool:
    if mode == "blue":
        return b > 120 and b - max(r, g) > 50
    if mode == "red":
        return r > 120 and r - max(g, b) > 50
    return max(r, g, b) < 90


def _remove_specks(pixels: list[bool], width: int, height: int, minimum: int = 4) -> list[bool]:
    """去掉彩色噪点。字符笔画足够大，小于 minimum 像素的连通块不是字。"""
    seen = [False] * (width * height)
    keep = [False] * (width * height)
    for start in range(width * height):
        if not pixels[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = True
        component = []
        while stack:
            index = stack.pop()
            component.append(index)
            x, y = index % width, index // width
            for dy in (-1, 0, 1):
                ny = y + dy
                if not 0 <= ny < height:
                    continue
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if not 0 <= nx < width:
                        continue
                    neighbour = ny * width + nx
                    if pixels[neighbour] and not seen[neighbour]:
                        seen[neighbour] = True
                        stack.append(neighbour)
        if len(component) >= minimum:
            for index in component:
                keep[index] = True
    return keep


def isolate(image: bytes | str | Image.Image, mode: str) -> Image.Image:
    """只保留目标颜色：彩色文字保留原色，全部文字转为黑色；其余变白。"""
    source = image if isinstance(image, Image.Image) else _decode_image(image)
    width, height = source.size
    source_pixels = source.load()
    selected = [
        _is_mode_color(*source_pixels[x, y], mode)
        for y in range(height)
        for x in range(width)
    ]
    if mode != "all":
        selected = _remove_specks(selected, width, height)
        # 彩色文字保留原来的颜色，模型能同时看到笔画和颜色；纯黑蒙版会把
        # 相邻笔画的深浅边界抹平，反而容易多读出一个字符。
        mask = Image.new("RGB", (width, height), "white")
        mask_pixels = mask.load()
        for y in range(height):
            for x in range(width):
                if selected[y * width + x]:
                    mask_pixels[x, y] = source_pixels[x, y]
        return mask
    mask = Image.new("L", (width, height), 255)
    mask_pixels = mask.load()
    for y in range(height):
        for x in range(width):
            if selected[y * width + x]:
                mask_pixels[x, y] = 0
    # 黑色字符是空心描边，轻微加粗后模型更容易读出笔画。
    return mask.filter(ImageFilter.MinFilter(3))


def _png_data_url(image: Image.Image, scale: int = 4) -> str:
    scaled = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    scaled.save(buffer, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def prepare(image: bytes | str, hint: str) -> tuple[str, str, str]:
    """返回 (原图 data URL, 目标颜色分离图 data URL, 取色方式)。"""
    mode = mode_from_hint(hint)
    source = _decode_image(image)
    return _png_data_url(source), _png_data_url(isolate(source, mode)), mode


def clean_text(value) -> str:
    text = str(value or "").strip().lower()
    text = "".join(_TEXT_RE.findall(text))
    if not text:
        return ""
    return text[:MAX_TEXT_LENGTH]


def make_payload(cfg, raw_url: str, mask_url: str, mode: str) -> dict:
    label = "全部文字" if mode == "all" else ("蓝色文字" if mode == "blue" else "红色文字")
    if mode == "all":
        guide = "图1是原图；图2是去掉彩色噪点、只保留黑色字符的图。"
    else:
        guide = f"图1是原图；图2是只保留{label}的分离图，其他颜色和彩色噪点已去掉。"
    images = [raw_url, mask_url]
    prompt = (
        f"税务网站验证码。只填写图中{label}，按从左到右排列，"
        "英文字母统一小写，不含空格。\n"
        f"{guide}\n"
        "字符可能是汉字、字母或数字，可能有旋转、变形、互相重叠；"
        "彩色小点和线条不是字符。\n"
        '只输出JSON：{"text":"识别结果","uncertain":false,"reason":""}。'
        "字符旋转、重叠但笔画仍可辨认时请给出结果；"
        "只有确实无法辨认时才留空并令 uncertain 为 true。"
    )
    content = [{"type": "text", "text": prompt}]
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in images)
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 160,
        "temperature": 0,
    }
    if cfg["provider"] == "deepseek" or cfg["model"].startswith("deepseek"):
        payload["thinking"] = {"type": "disabled"}
    elif cfg["provider"] == "qwen" or cfg["model"].startswith("qwen"):
        payload["enable_thinking"] = False
    return payload


def parse_result(raw) -> dict:
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict):
        raise ValueError("验证码识别结果格式有误")
    uncertain = raw.get("uncertain") is True
    text = clean_text(raw.get("text"))
    reason = str(raw.get("reason") or "").strip()[:40]
    if uncertain:
        return {"text": "", "uncertain": True, "reason": reason or "模型不确定"}
    if not text:
        return {"text": "", "uncertain": True, "reason": reason or "没有读出文字"}
    return {"text": text, "uncertain": False, "reason": reason}


def recognize(cfg, key: str, image: bytes | str, hint: str) -> dict:
    """识别一次验证码。每次调用都是新上下文，不复用上一次的结果。"""
    if not key or not cfg["base_url"]:
        raise ValueError("请先在设置的开发者选项里填写 AI 识别的 Key")
    raw_url, mask_url, mode = prepare(image, hint)
    payload = make_payload(cfg, raw_url, mask_url, mode)
    pricing = payment_ai.price_info(cfg)
    try:
        response = payment_ai.request_completion(
            cfg["base_url"] + "/chat/completions", key, payload, 60
        )
    except HTTPError as exc:
        labels = {401: "Key 无效", 403: "Key 无访问权限", 402: "余额不足",
                  429: "请求受限，请降低并发"}
        raise ValueError(labels.get(exc.code, f"服务请求失败（{exc.code}）")) from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("网络超时，请重试") from None
    except Exception:
        raise ValueError("识别结果不完整，请重试") from None
    usage = response.get("usage") or {}
    result = parse_result(response["result"])
    return {
        **result,
        "mode": mode,
        "usage": usage,
        "cost": payment_ai.usage_cost(usage, pricing) if usage else None,
        "api_seconds": response.get("api_seconds"),
        "model": response.get("resolved_model") or cfg["model"],
        "prompt_version": PROMPT_VERSION,
    }
