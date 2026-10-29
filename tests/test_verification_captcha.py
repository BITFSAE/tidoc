import io

from PIL import Image, ImageDraw

from tidoc.services import payment_ai, verification_captcha as captcha


def sample_image() -> Image.Image:
    image = Image.new("RGB", (120, 50), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 24, 24), fill=(0, 0, 255))
    draw.rectangle((34, 10, 48, 24), fill=(255, 0, 0))
    draw.rectangle((58, 10, 72, 24), fill=(0, 0, 0))
    return image


def image_bytes() -> bytes:
    buffer = io.BytesIO()
    sample_image().save(buffer, "PNG")
    return buffer.getvalue()


def test_mode_from_hint():
    assert captcha.mode_from_hint("请输入验证码图片中蓝色文字") == "blue"
    assert captcha.mode_from_hint("请输入验证码图片中红色文字") == "red"
    assert captcha.mode_from_hint("请输入验证码文字") == "all"


def test_isolate_keeps_only_target_color():
    blue = captcha.isolate(image_bytes(), "blue")
    assert blue.mode == "RGB"
    assert blue.getpixel((17, 17)) == (0, 0, 255)
    assert blue.getpixel((41, 17)) == (255, 255, 255)
    assert blue.getpixel((65, 17)) == (255, 255, 255)

    red = captcha.isolate(image_bytes(), "red")
    assert red.getpixel((41, 17)) == (255, 0, 0)
    assert red.getpixel((17, 17)) == (255, 255, 255)

    black = captcha.isolate(image_bytes(), "all")
    assert black.mode == "L"
    assert black.getpixel((65, 17)) == 0
    assert black.getpixel((17, 17)) == 255


def test_prepare_returns_original_and_mask_data_urls():
    raw, mask, mode = captcha.prepare(image_bytes(), "请输入验证码图片中红色文字")
    assert mode == "red"
    assert raw.startswith("data:image/png;base64,")
    assert mask.startswith("data:image/png;base64,")


def test_clean_text_normalizes_letters_and_keeps_chinese():
    assert captcha.clean_text(" 4A 7e！") == "4a7e"
    assert captcha.clean_text("天巡P") == "天巡p"
    assert captcha.clean_text("  ") == ""
    assert captcha.clean_text("a" * 20) == "a" * captcha.MAX_TEXT_LENGTH


def test_parse_result_skips_uncertain_or_empty():
    assert captcha.parse_result({"text": "XWU", "uncertain": False}) == {
        "text": "xwu", "uncertain": False, "reason": "",
    }
    assert captcha.parse_result({"text": "xwu", "uncertain": True})["text"] == ""
    assert captcha.parse_result({"text": "！？", "uncertain": False})["uncertain"] is True
    assert captcha.parse_result([{"text": "xr", "uncertain": False}])["text"] == "xr"


def test_make_payload_uses_two_images_and_disables_thinking():
    cfg = payment_ai.validate_config({
        **payment_ai.DEFAULT_CONFIG,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
    })
    payload = captcha.make_payload(
        cfg, "data:image/png;base64,cmF3", "data:image/png;base64,bWFzaw==", "blue")
    content = payload["messages"][0]["content"]
    assert [item["type"] for item in content] == ["text", "image_url", "image_url"]
    assert "蓝色文字" in content[0]["text"]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}

    all_payload = captcha.make_payload(
        cfg, "data:image/png;base64,cmF3", "data:image/png;base64,bWFzaw==", "all")
    assert "图2是去掉彩色噪点" in all_payload["messages"][0]["content"][0]["text"]
