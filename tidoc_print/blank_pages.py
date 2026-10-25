"""保守识别查验单的空白尾页；无法确认时保留原页。"""
from __future__ import annotations

import re
from math import hypot

# 这些指令只改变状态、路径或文字；其余未识别指令按可能绘制内容处理。
_NONPAINTING_OPERATORS = frozenset(b'''
    q Q cm w J j M d ri i h re m l c v y W W* n
    g rg k cs sc scn G RG K CS SC SCN
    BT ET Tc Tw Tz TL Tf Tr Ts Td TD Tm T* Tj TJ ' "
    MP DP BMC BDC EMC BX EX
'''.split())


def _text_geometry(cm, tm, size):
    return (
        tm[4] * cm[0] + tm[5] * cm[2] + cm[4],
        tm[4] * cm[1] + tm[5] * cm[3] + cm[5],
        size * hypot(tm[2] * cm[0] + tm[3] * cm[2], tm[2] * cm[1] + tm[3] * cm[3]),
    )


def _remark_label_at_bottom(page):
    """查验单最后一行的「备注」标签，是判断空表格跨页的依据。"""
    labels = []

    def text(value, cm, tm, font, size):
        _, y, _ = _text_geometry(cm, tm, size)
        if float(page.mediabox.bottom) - 12 <= y <= float(page.mediabox.bottom) + 75:
            labels.append(re.sub(r'\s+', '', value))

    try:
        page.extract_text(visitor_text=text)
    except Exception:
        return False
    return '备注' in ''.join(labels)


def _empty_remark_overflow(page, previous_page):
    """接受上一页空备注行溢出的灰色边框和标签碎片，正文仍须为空。

    部分税务查验单用带 ICC 色彩空间的细长填充矩形画表格；
    即使只剩空表格，文字之外也有绘制指令，不能按「有图形」直接保留。
    """
    if (page.rotation or page.get('/Annots') or page.cropbox != page.mediabox
            or not _remark_label_at_bottom(previous_page)):
        return False
    box = page.mediabox
    left, bottom, right, top = map(float, box)
    if right <= left or top <= bottom:
        return False
    top_band = max(42.5, (top - bottom) * 0.12)
    resources = page.get('/Resources') or {}
    if hasattr(resources, 'get_object'):
        resources = resources.get_object()
    spaces = resources.get('/ColorSpace') or {}
    if hasattr(spaces, 'get_object'):
        spaces = spaces.get_object()
    clip = (left, bottom, right, top)
    color_space = '/DeviceGray'
    color = (0.0,)
    stack = []
    rectangles = []
    other_path = False
    valid = True
    wide_border = False
    has_text_operator = False
    extracted_text = False

    def intersection(a, b):
        return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])

    def rgb_space(name):
        value = spaces.get(name, name)
        if hasattr(value, 'get_object'):
            value = value.get_object()
        if isinstance(value, list) and value[0] == '/ICCBased':
            profile = value[1].get_object()
            # ICCBased 默认的替代色彩空间由通道数决定。
            return profile.get('/N') == 3 and profile.get('/Alternate', '/DeviceRGB') == '/DeviceRGB'
        return value == '/DeviceRGB'

    def operand(op, args, cm, tm):
        nonlocal clip, color_space, color, other_path, valid, wide_border, has_text_operator
        if op in (b'Tj', b'TJ', b"'", b'"'):
            has_text_operator |= any(bool(value) for value in args)
        if op == b'q':
            stack.append((clip, color_space, color))
        elif op == b'Q':
            if stack:
                clip, color_space, color = stack.pop()
        elif op == b'cs':
            color_space = args[0]
        elif op == b'g':
            color_space, color = '/DeviceGray', tuple(map(float, args))
        elif op == b'rg':
            color_space, color = '/DeviceRGB', tuple(map(float, args))
        elif op == b'k':
            color_space, color = '/DeviceCMYK', tuple(map(float, args))
        elif op in (b'sc', b'scn'):
            color = tuple(map(float, args))
        elif op == b're':
            x, y, w, h = map(float, args)
            if cm[1] or cm[2]:
                other_path = True
            points = [(px * cm[0] + py * cm[2] + cm[4], px * cm[1] + py * cm[3] + cm[5])
                      for px, py in ((x, y), (x + w, y + h))]
            rectangles.append((min(p[0] for p in points), min(p[1] for p in points),
                               max(p[0] for p in points), max(p[1] for p in points)))
        elif op in (b'm', b'l', b'c', b'v', b'y'):
            other_path = True
        elif op in (b'W', b'W*'):
            if len(rectangles) == 1 and not other_path:
                clip = intersection(clip, rectangles[0])
            else:
                valid = False
        elif op in (b'f', b'F', b'f*'):
            neutral = (len(color) == 1 and color_space == '/DeviceGray'
                       or len(color) == 3 and rgb_space(color_space) and max(color) - min(color) < 1e-6)
            white = ((neutral and min(color) >= 0.999)
                     or (color_space == '/DeviceCMYK' and color == (0, 0, 0, 0)))
            if not white:
                if other_path or not neutral or not 0.5 <= min(color) < 0.999:
                    valid = False
                for rect in rectangles:
                    x0, y0, x1, y1 = intersection(rect, clip)
                    if x1 <= x0 or y1 <= y0:
                        continue
                    # 仅接受页顶的灰色细边框，正文区线条、实心块和印章保留。
                    if y0 < top - top_band or min(rect[2] - rect[0], rect[3] - rect[1]) > 1.5:
                        valid = False
                    if x1 - x0 > (right - left) / 2:
                        wide_border = True
            rectangles.clear()
            other_path = False
        elif op == b'n':
            rectangles.clear()
            other_path = False
        elif op not in _NONPAINTING_OPERATORS:
            valid = False

    def text(value, cm, tm, font, size):
        nonlocal valid, extracted_text
        extracted_text |= bool(value.strip())
        if font is not None and font.get('/Subtype') == '/Type3':
            valid = False
        value = re.sub(r'\s+', '', value)
        if not value:
            return
        x, y, height = _text_geometry(cm, tm, size)
        if (font is None or value not in ('备', '注', '备注')
                or not left <= x <= left + (right - left) * 0.1
                or not top - top_band <= y <= top or not 0 < height <= 12):
            valid = False

    try:
        page.extract_text(visitor_text=text, visitor_operand_before=operand)
    except Exception:
        return False
    return valid and wide_border and not (has_text_operator and not extracted_text)


def _page_facts(page):
    """读取文字位置和绘制指令，不把无文字的扫描件当空白页。"""
    if page.rotation or page.cropbox != page.mediabox:
        return None
    box = page.mediabox
    if float(box.width) <= 0 or float(box.height) <= 0:
        return None
    texts = []
    graphics = bool(page.get('/Annots'))
    fill_white = False
    stack = []
    has_text_operator = False
    extracted_text = False

    def operand(op, args, cm, tm):
        nonlocal graphics, fill_white, has_text_operator
        if op in (b'Tj', b'TJ', b"'", b'"'):
            has_text_operator |= any(bool(value) for value in args)
        if op == b'q':
            stack.append(fill_white)
        elif op == b'Q':
            fill_white = stack.pop() if stack else False
        elif op == b'g':
            fill_white = list(args) == [1]
        elif op == b'rg':
            fill_white = list(args) == [1, 1, 1]
        elif op == b'k':
            fill_white = list(args) == [0, 0, 0, 0]
        elif op in (b'cs', b'sc', b'scn'):
            fill_white = False
        elif op in (b'f', b'F', b'f*'):
            graphics |= not fill_white
        elif op not in _NONPAINTING_OPERATORS:
            graphics = True

    def text(value, cm, tm, font, size):
        nonlocal graphics, extracted_text
        if font is not None and font.get('/Subtype') == '/Type3':
            graphics = True
        extracted_text |= bool(value)
        value = re.sub(r'\s+', '', value)
        if not value:
            return
        if font is None or size <= 0:
            graphics = True
        x, y, height = _text_geometry(cm, tm, size)
        # 只容许纸边 15 mm 内、字号较小的浏览器页眉页脚。
        margin = 42.5
        at_edge = (float(box.bottom) <= y <= float(box.bottom) + margin
                   or float(box.top) - margin <= y <= float(box.top))
        texts.append((value, at_edge and float(box.left) <= x <= float(box.right) and 0 < height <= 12))

    try:
        page.extract_text(visitor_text=text, visitor_operand_before=operand)
    except Exception:  # 识别失败只影响筛选，不影响原文件的正常拼接。
        return None
    return texts, graphics or (has_text_operator and not extracted_text)


def inspection_pages(reader, *, cancel_check=None):
    """只移除连续空白尾页，至少保留第一页；返回原始 PageObject。"""
    pages = list(reader.pages)
    if len(pages) < 2:
        return pages
    first = _page_facts(pages[0])
    # 页眉标题必须与首页相同；不能仅因文字短就删掉一个结论或金额。
    headers = {value for value, edge in first[0] if edge and not re.fullmatch(
        r'[¥￥]?[+-]?\d+(?:[,.]\d+)*(?:元)?', value
    )} if first else set()
    while len(pages) > 1:
        if cancel_check and cancel_check():
            raise RuntimeError('任务已取消')
        facts = _page_facts(pages[-1])
        if facts is None:
            break
        if facts[1]:
            try:
                empty_overflow = _empty_remark_overflow(pages[-1], pages[-2])
            except Exception:
                empty_overflow = False
            if empty_overflow:
                pages.pop()
                continue
            break
        texts = facts[0]
        if sum(len(value) for value, _ in texts) > 200:
            break
        if any(not edge or not (
            value in headers
            or re.fullmatch(r'(?:第)?\d+[/／]\d+(?:页)?', value)
        ) for value, edge in texts):
            break
        pages.pop()
    return pages
