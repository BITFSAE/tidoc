"""PDF 拼接与付款截图转 PDF（设计文档第 9 节）。

- merge_pdfs：把多个 PDF（发票 / 查验单）拼成一份。
- merge_pdf_groups：按条目连续拼接材料，并按条目标记页码。
- images_to_pdf：把付款截图（jpg/png）按横版 A4、每页两张拼接。
- 页面信息标注：只保留份数 / 页码编号，减少遮挡。
"""

from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from PIL import Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

# 注册一个内置 CJK 字体，保证中文标注不乱码（无需外部字体文件）
_CJK_FONT = "STSong-Light"
try:
    pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
    _FONT_OK = True
except Exception:
    _FONT_OK = False


def _footer_text(batch_note: str, text: str) -> str:
    batch_note = (batch_note or "").strip()
    return f"{batch_note}  {text}" if batch_note else text


def _draw_label(c, text: str, anchor_x: float, baseline_y: float,
                font_size: float = 11, align: str = "right") -> None:
    """在锚点处画标注：先铺白色不透明底框盖住底层内容，再写字。
    align="right" 时 anchor_x 为右边界；align="center" 时 anchor_x 为水平中心。"""
    font = _CJK_FONT if _FONT_OK else "Helvetica"
    text_w = pdfmetrics.stringWidth(text, font, font_size)
    pad_x, pad_y = 2.2 * mm, 1.4 * mm
    box_x = anchor_x - text_w / 2 - pad_x if align == "center" else anchor_x - text_w - pad_x
    box_y = baseline_y - pad_y
    box_w = text_w + 2 * pad_x
    box_h = font_size + 2 * pad_y
    # 白底框：遮住底层单据原有内容，避免叠字重影
    c.setFillColorRGB(1, 1, 1)
    c.rect(box_x, box_y, box_w, box_h, stroke=0, fill=1)
    c.setFont(font, font_size)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    if align == "center":
        c.drawCentredString(anchor_x, baseline_y, text)
    else:
        c.drawRightString(anchor_x, baseline_y, text)


def _annotation_overlay(text: str, pagesize=A4) -> PdfReader:
    """生成一张只含标注文字的叠加页：右下角带白底框。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=pagesize)
    width, _ = pagesize
    _draw_label(c, text, width - 10 * mm, 7 * mm)
    c.save()
    buf.seek(0)
    return PdfReader(buf)


def merge_pdfs(pdf_paths: list[str | Path], out_path: str | Path,
               annotations: list[str] | None = None,
               numbered: bool = False,
               batch_note: str = "") -> Path:
    """把多个 PDF 拼成一份。numbered=True 时每页右下角标「第 N 份-P/T」。"""
    writer = PdfWriter()
    for idx, pdf_path in enumerate(pdf_paths):
        reader = PdfReader(str(pdf_path))
        note = annotations[idx] if annotations and idx < len(annotations) else ""
        total_pages = len(reader.pages)
        for page_idx, page in enumerate(reader.pages, start=1):
            # 先把页加进 writer，再对 writer 内的页做叠加（pypdf 推荐做法，避免不可靠）
            added = writer.add_page(page)
            label = _footer_text(batch_note, f"No.{idx + 1}-{page_idx}/{total_pages}") if numbered else note
            if label:
                box = added.mediabox
                overlay = _annotation_overlay(label, (float(box.width), float(box.height)))
                added.merge_page(overlay.pages[0])
    out_path = Path(out_path)
    with out_path.open("wb") as f:
        writer.write(f)
    return out_path


def merge_pdf_groups(pdf_groups: list[list[str | Path]], out_path: str | Path,
                     numbered: bool = True, batch_note: str = "") -> Path:
    """按组连续拼接 PDF；每组对应一个条目，编号不会被材料类型打断。"""
    writer = PdfWriter()
    for group_idx, pdf_paths in enumerate(pdf_groups, start=1):
        readers = [PdfReader(str(path)) for path in pdf_paths]
        total_pages = sum(len(reader.pages) for reader in readers)
        page_idx = 0
        for reader in readers:
            for page in reader.pages:
                page_idx += 1
                added = writer.add_page(page)
                if numbered:
                    box = added.mediabox
                    label = _footer_text(batch_note, f"No.{group_idx}-{page_idx}/{total_pages}")
                    overlay = _annotation_overlay(label, (float(box.width), float(box.height)))
                    added.merge_page(overlay.pages[0])
    out_path = Path(out_path)
    with out_path.open("wb") as f:
        writer.write(f)
    return out_path


def images_to_pdf(image_paths: list[str | Path], out_path: str | Path,
                  annotations: list[str] | None = None,
                  batch_note: str = "", show_page_footer: bool = True,
                  orientation: str = "landscape", images_per_page: int = 2,
                  margin_mm: float = 10, cancel_check=None) -> Path:
    """把付款截图按横版 A4、每页两张拼成 PDF。"""
    writer = PdfWriter()
    if orientation not in ("portrait", "landscape", "auto") or images_per_page not in (1, 2, 4) or not 0 <= margin_mm <= 40:
        raise ValueError("不支持的图片布局")
    if orientation == "auto":
        with Image.open(image_paths[0]) as probe:
            orientation = "landscape" if probe.width > probe.height else "portrait"
    page_w, page_h = landscape(A4) if orientation == "landscape" else A4
    margin = margin_mm * mm
    gap = 8 * mm
    footer_h = 10 * mm
    cols = 2 if images_per_page == 4 or images_per_page == 2 and orientation == "landscape" else 1
    rows = images_per_page // cols
    slot_w = (page_w - 2 * margin - gap * (cols - 1)) / cols
    slot_h = (page_h - 2 * margin - footer_h - gap * (rows - 1)) / rows
    from reportlab.lib.utils import ImageReader

    total_pages = (len(image_paths) + images_per_page - 1) // images_per_page
    for page_index, start in enumerate(range(0, len(image_paths), images_per_page), start=1):
        if cancel_check and cancel_check():
            raise RuntimeError("任务已取消")
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(page_w, page_h))
        for offset, img_path in enumerate(image_paths[start:start + images_per_page]):
            idx = start + offset
            with Image.open(img_path) as im:
                im = im.convert("RGB")
                iw, ih = im.size
                scale = min(slot_w / iw, slot_h / ih)
                draw_w, draw_h = iw * scale, ih * scale
                slot_x = margin + (offset % cols) * (slot_w + gap)
                slot_y = margin + footer_h + (rows - 1 - offset // cols) * (slot_h + gap)
                x = slot_x + (slot_w - draw_w) / 2
                y = slot_y + (slot_h - draw_h) / 2
                tmp = io.BytesIO()
                im.save(tmp, format="PNG")
                tmp.seek(0)
                c.drawImage(ImageReader(tmp), x, y, width=draw_w, height=draw_h)
            note = annotations[idx] if annotations and idx < len(annotations) else ""
            if note:
                _draw_label(c, note, slot_x + slot_w / 2, slot_y - 3 * mm,
                            font_size=10, align="center")
        if show_page_footer:
            _draw_label(c, _footer_text(batch_note, f"Page {page_index}/{total_pages}"),
                        page_w - margin, 6 * mm, font_size=10)
        c.save()
        buf.seek(0)
        for page in PdfReader(buf).pages:
            writer.add_page(page)
    out_path = Path(out_path)
    with out_path.open("wb") as f:
        writer.write(f)
    return out_path


def render_pdf_bundle(context, output, resources, resources_root, out_path, cancel_check=None):
    """All selected roles are rendered; unsupported resources are never silently skipped."""
    from pathlib import PurePosixPath
    from tempfile import TemporaryDirectory
    settings = output.get('pdf') or {}
    roles = settings.get('roles') or settings.get('include_roles') or ['invoice', 'payment_screenshot', 'inspection_pdf']
    order = settings.get('content_order',settings.get('order', 'entry'))
    role_order = settings.get('role_order') or roles
    role_order = list(dict.fromkeys(role_order + roles))
    root = Path(resources_root).resolve()
    selected = []
    for resource in resources:
        if resource.get('role_id') not in roles:
            continue
        relative = PurePosixPath(resource['path'])
        path = root.joinpath(*relative.parts).resolve()
        if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root) or not path.is_file():
            raise ValueError('材料资源缺失或路径不安全')
        if path.suffix.lower() not in ('.pdf', '.png', '.jpg', '.jpeg'):
            # XML supplies invoice facts, but has no printable face.
            if resource.get('role_id') == 'invoice' and path.suffix.lower() == '.xml':
                continue
            raise ValueError('此材料不能转换为 PDF，请使用附件整理包')
        selected.append((resource, path))
    if not selected:
        raise ValueError('没有可打印的材料')
    with TemporaryDirectory(prefix='tidoc-materials-') as tmp:
        groups = []
        entry_ids = [entry['id'] for entry in context['entries']]
        keys = [(entry_id, None) for entry_id in entry_ids] if order == 'entry' else [(None, role) for role in role_order]
        if order not in ('entry', 'role'):
            raise ValueError('不支持的材料排序')
        for group_index, (entry_id, role_id) in enumerate(keys,1):
            group = [(r,p) for r,p in selected if (entry_id is None or r.get('entry_id') == entry_id) and (role_id is None or r.get('role_id') == role_id)]
            if order == 'entry':
                group.sort(key=lambda pair: role_order.index(pair[0]['role_id']) if pair[0]['role_id'] in role_order else len(role_order))
            paths = []
            pending = []
            def flush():
                if pending:
                    dest = Path(tmp) / f'images-{group_index}-{len(paths)}.pdf'
                    layout = settings.get('image_layout', '')
                    orientation = settings.get('orientation') or ('portrait' if 'portrait' in layout else 'landscape')
                    per_page = settings.get('images_per_page') or (int(layout[-1]) if layout and layout[-1] in '124' else 2)
                    images_to_pdf(pending, dest, show_page_footer=False, orientation=orientation, images_per_page=per_page, margin_mm=settings.get('margin_mm',10), cancel_check=cancel_check)
                    paths.append(dest); pending.clear()
            previous_role = None
            for resource,path in group:
                if cancel_check and cancel_check():
                    raise RuntimeError('任务已取消')
                if resource['role_id'] != previous_role:
                    flush()
                previous_role = resource['role_id']
                if path.suffix.lower() == '.pdf':
                    flush(); paths.append(path)
                else:
                    pending.append(path)
            flush()
            if paths:
                groups.append(paths)
        numbering = settings.get('numbering', True)
        writer = PdfWriter()
        for group_index, paths in enumerate(groups,1):
            readers = [PdfReader(str(path)) for path in paths]
            count = sum(len(reader.pages) for reader in readers)
            index = 0
            for reader in readers:
                for page in reader.pages:
                    if cancel_check and cancel_check():
                        raise RuntimeError('任务已取消')
                    index += 1
                    if settings.get('page_size')=='a4':
                        from pypdf import PageObject, Transformation
                        box=page.mediabox;scale=min(A4[0]/float(box.width),A4[1]/float(box.height))
                        sheet=PageObject.create_blank_page(width=A4[0],height=A4[1])
                        sheet.merge_transformed_page(page,Transformation().scale(scale).translate((A4[0]-float(box.width)*scale)/2,(A4[1]-float(box.height)*scale)/2))
                        added=writer.add_page(sheet)
                    else:
                        added=writer.add_page(page)
                    if numbering not in (False, 'none'):
                        label = f'No.{group_index}-{index}/{count}' if numbering in (True,'entry_page','no_entry_page') else f'Page {len(writer.pages)}'
                        note = settings.get('batch_note') or (context.get('batch') or {}).get('notes') or (context.get('batch') or {}).get('note') or ''
                        box = added.mediabox
                        added.merge_page(_annotation_overlay(_footer_text(note,label),(float(box.width),float(box.height))).pages[0])
        if not writer.pages:
            raise ValueError('材料 PDF 没有页面')
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True,exist_ok=True)
        with out_path.open('wb') as stream:
            writer.write(stream)
    return out_path
