"""Reconstruct PPTX visual evidence without an Office process.

Read source shapes, not graph previews: XML order is paint order, and native
runs/crops/decorations contain information deliberately absent from the graph.
This is an approximation, not an Office-compatible renderer. Unsupported units
are reported per slide rather than silently turned into invented content.
"""
from __future__ import annotations

from io import BytesIO
import math
from pathlib import Path
from typing import NamedTuple

from lxml import etree
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn
from pydantic import BaseModel, Field

from ..extractors.pptx import _flatten_shapes, _ole_preview_image, _shape_slide_corners, _OLE_SHAPE_TYPES
from .xlsx_capture import FALLBACK_FONT_PATHS, _find_font_file_for_family, _load_capture_font

_EMU_PER_PT = 12700
_MAX_LAYER_PIXELS = 20_000_000
# Per-glyph fallback candidates, tried in order when the slide's own font is
# missing a specific character (see `_glyph_font`). Reuses xlsx_capture's
# shared last-resort font paths rather than hardcoding a second copy, with
# one PPTX-specific addition: Apple Symbols covers glyphs (e.g. the degree
# sign) that AppleGothic itself lacks — NanumGothic on the real fixture host
# has an empty Celsius glyph, confirmed by a real document.
_GLYPH_FALLBACK_PATHS = (
    FALLBACK_FONT_PATHS["regular"][0],  # AppleGothic
    "/System/Library/Fonts/Apple Symbols.ttf",
    FALLBACK_FONT_PATHS["regular"][1],  # DejaVu Sans (Linux)
)


class SlideReconstruction(BaseModel):
    png: bytes
    warnings: list[str] = Field(default_factory=list)


class _Glyph(NamedTuple):
    text: str
    font: ImageFont.FreeTypeFont
    color: tuple[int, int, int, int]
    bold: bool
    width: float


def _find(element, path: str):
    if element is None:
        return None
    return element.find('/'.join(qn(tag) for tag in path.split('/')))


def _first(elements):
    return next((element for element in elements if element is not None), None)


def _attribute(elements, name: str, default: str) -> str:
    return next((element.get(name) for element in elements
                 if element is not None and element.get(name) is not None), default)


def _composite(canvas: Image.Image, layer: Image.Image, corners: list[tuple[float, float]]) -> None:
    """Warp local pixels into the same affine quadrilateral used by extraction."""
    x0, y0 = corners[0]
    a = (corners[1][0] - x0) / layer.width
    b = (corners[3][0] - x0) / layer.height
    d = (corners[1][1] - y0) / layer.width
    e = (corners[3][1] - y0) / layer.height
    determinant = a * e - b * d
    if abs(determinant) < 1e-9:
        return
    left = max(0, math.floor(min(x for x, y in corners)))
    top = max(0, math.floor(min(y for x, y in corners)))
    right = min(canvas.width, math.ceil(max(x for x, y in corners)))
    bottom = min(canvas.height, math.ceil(max(y for x, y in corners)))
    if right <= left or bottom <= top:
        return
    inverse = (e / determinant, -b / determinant,
               (e * (left - x0) - b * (top - y0)) / determinant,
               -d / determinant, a / determinant,
               (-d * (left - x0) + a * (top - y0)) / determinant)
    warped = layer.transform((right - left, bottom - top), Image.Transform.AFFINE,
                             inverse, resample=Image.Resampling.BICUBIC)
    canvas.alpha_composite(warped, (left, top))


class PptxSlideRenderer:
    """Open once per enrichment stage; cache unannotated slides across anchors."""

    def __init__(self, path: str | Path, *, max_dimension: int = 1600) -> None:
        if not 128 <= max_dimension <= 4096:
            raise ValueError('max_dimension must be between 128 and 4096')
        self.presentation = Presentation(str(path))
        self.scale = max_dimension / max(self.presentation.slide_width, self.presentation.slide_height)
        self.size = (round(self.presentation.slide_width * self.scale),
                     round(self.presentation.slide_height * self.scale))
        self._cache: dict[int, SlideReconstruction] = {}
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        self._warnings: set[str] = set()
        self._glyph_fonts: dict[tuple[str, int, str], ImageFont.FreeTypeFont] = {}
        self._theme = None
        self._slide = None

    def close(self) -> None:
        self._cache.clear()
        self._fonts.clear()
        self._glyph_fonts.clear()

    def render(self, slide_index: int) -> SlideReconstruction:
        if slide_index in self._cache:
            return self._cache[slide_index]
        slide = self.presentation.slides[slide_index]
        self._slide = slide
        self._warnings = set()
        master = slide.slide_layout.slide_master
        theme_part = next((rel.target_part for rel in master.part.rels.values()
                           if rel.reltype.endswith('/theme')), None)
        self._theme = etree.fromstring(theme_part.blob) if theme_part else None
        background = _first(_find(owner._element, 'p:cSld/p:bg/p:bgPr')
                            for owner in (slide, slide.slide_layout, master))
        canvas = Image.new('RGBA', self.size, self._fill(background, (255, 255, 255, 255)))
        # Master/layout artwork is behind slide shapes. Their placeholders are
        # inheritance templates, not additional visible text instances.
        owners = [master, slide.slide_layout, slide]
        if slide._element.get('showMasterSp') in ('0', 'false'):
            owners = [slide]
        for owner in owners:
            for shape in _flatten_shapes(owner.shapes):
                if owner is not slide and shape.is_placeholder:
                    continue
                try:
                    self._draw_shape(canvas, shape)
                except (OSError, ValueError, TypeError, NotImplementedError) as exc:
                    self._warnings.add(f'{shape.name}: skipped ({type(exc).__name__})')
        stream = BytesIO()
        canvas.convert('RGB').save(stream, format='PNG')
        result = SlideReconstruction(png=stream.getvalue(), warnings=sorted(self._warnings))
        self._cache[slide_index] = result
        return result

    def _color(self, container, default=(0, 0, 0, 255)) -> tuple[int, int, int, int]:
        if container is None:
            return default
        color = next((c for c in container if etree.QName(c).localname in
                      ('srgbClr', 'schemeClr', 'sysClr', 'prstClr')), None)
        if color is None:
            return default
        name = etree.QName(color).localname
        value = color.get('val', '')
        if name == 'schemeClr':
            mapping = _first([_find(self._slide._element, 'p:clrMapOvr/a:overrideClrMapping'),
                              _find(self._slide.slide_layout._element, 'p:clrMapOvr/a:overrideClrMapping'),
                              _find(self._slide.slide_layout.slide_master._element, 'p:clrMap')])
            aliases = {'tx1': 'dk1', 'bg1': 'lt1', 'tx2': 'dk2', 'bg2': 'lt2'}
            value = mapping.get(value, aliases.get(value, value)) if mapping is not None else aliases.get(value, value)
            entry = _find(self._theme, f'a:themeElements/a:clrScheme/a:{value}')
            rgba = self._color(entry, default)
        elif name == 'sysClr':
            value = color.get('lastClr', '000000')
            rgba = (*tuple(bytes.fromhex(value)), 255)
        elif name == 'srgbClr':
            rgba = (*tuple(bytes.fromhex(value)), 255)
        else:
            from PIL import ImageColor
            try:
                rgba = ImageColor.getcolor(value, 'RGBA')
            except ValueError:
                rgba = default
        rgb = [float(c) for c in rgba[:3]]
        alpha = rgba[3]
        for modifier in color:
            kind = etree.QName(modifier).localname
            fraction = int(modifier.get('val', '100000')) / 100000
            if kind in ('lumMod', 'shade'):
                rgb = [c * fraction for c in rgb]
            elif kind == 'lumOff':
                rgb = [c + 255 * fraction for c in rgb]
            elif kind == 'tint':
                rgb = [c + (255 - c) * fraction for c in rgb]
            elif kind == 'alpha':
                alpha = round(255 * fraction)
        return (*[max(0, min(255, round(c))) for c in rgb], max(0, min(255, alpha)))

    def _fill(self, properties, default=None):
        if _find(properties, 'a:noFill') is not None:
            return (0, 0, 0, 0)
        fill = _find(properties, 'a:solidFill')
        if fill is not None:
            return self._color(fill)
        if any(_find(properties, tag) is not None for tag in ('a:gradFill', 'a:pattFill', 'a:blipFill')):
            self._warnings.add('Unsupported gradient/pattern/picture fill')
        return default

    def _draw_shape(self, canvas: Image.Image, shape) -> None:
        corners = [(x * self.scale, y * self.scale) for x, y in _shape_slide_corners(shape)]
        if max(x for x, y in corners) < 0 or min(x for x, y in corners) > canvas.width:
            return
        if max(y for x, y in corners) < 0 or min(y for x, y in corners) > canvas.height:
            return
        properties = _find(shape._element, 'p:spPr')
        geometry = _find(properties, 'a:prstGeom')
        preset = geometry.get('prst') if geometry is not None else 'rect'
        if shape.shape_type == MSO_SHAPE_TYPE.LINE:
            self._draw_line(canvas, corners, _find(properties, 'a:ln'), preset)
            return
        w = max(1, round(math.dist(corners[0], corners[1])))
        h = max(1, round(math.dist(corners[0], corners[3])))
        if w * h > _MAX_LAYER_PIXELS:
            self._warnings.add(f'{shape.name}: oversized layer skipped')
            return
        layer = Image.new('RGBA', (w, h))
        sx, sy = w / max(1, shape.width), h / max(1, shape.height)
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            with Image.open(BytesIO(shape.image.blob)) as source:
                picture = source.convert('RGBA')
                l, t, r, b = shape.crop_left, shape.crop_top, shape.crop_right, shape.crop_bottom
                box = (round(l * picture.width), round(t * picture.height),
                       round((1 - r) * picture.width), round((1 - b) * picture.height))
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise ValueError('empty picture crop')
                picture = picture.crop(box).resize((w, h), Image.Resampling.LANCZOS)
                layer.alpha_composite(picture)
        elif shape.shape_type in _OLE_SHAPE_TYPES:
            preview = _ole_preview_image(shape)
            if preview is None:
                raise ValueError('missing OLE preview')
            with Image.open(BytesIO(preview[0])) as source:
                layer.alpha_composite(source.convert('RGBA').resize((w, h), Image.Resampling.LANCZOS))
        elif shape.has_table:
            self._draw_table(layer, shape, sx, sy)
        elif shape.shape_type in (MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.TEXT_BOX, MSO_SHAPE_TYPE.PLACEHOLDER):
            self._draw_geometry(layer, properties, preset, sx)
            if shape.has_text_frame:
                self._draw_text(layer, shape.text_frame, shape, sx, sy)
        else:
            self._warnings.add(f'{shape.name}: unsupported {shape.shape_type}')
            return
        _composite(canvas, layer, corners)

    def _draw_geometry(self, layer, properties, preset, sx):
        draw = ImageDraw.Draw(layer)
        fill = self._fill(properties)
        line = _find(properties, 'a:ln')
        outline = self._fill(line) if line is not None else None
        width = max(1, round(int(line.get('w', '12700')) * sx)) if line is not None else 1
        rect = (0, 0, layer.width - 1, layer.height - 1)
        if preset == 'ellipse':
            draw.ellipse(rect, fill=fill, outline=outline, width=width)
        elif preset == 'roundRect':
            adjustment = _find(properties, 'a:prstGeom/a:avLst/a:gd')
            fraction = float(adjustment.get('fmla', 'val 16667').split()[-1]) / 100000 if adjustment is not None else 0.16667
            draw.rounded_rectangle(rect, radius=min(layer.size) * min(0.5, max(0, fraction)),
                                   fill=fill, outline=outline, width=width)
        elif preset == 'rect':
            draw.rectangle(rect, fill=fill, outline=outline, width=width)
        else:
            self._warnings.add(f'Unsupported shape geometry: {preset}')

    def _draw_line(self, canvas, corners, line, preset):
        color = self._fill(line, (0, 0, 0, 255))
        if color[3] == 0:
            return
        width = max(1, round(int(line.get('w', '12700')) * self.scale)) if line is not None else 1
        start, end = corners[0], corners[2]
        points = [start, end]
        if preset == 'bentConnector3':
            # Default three-segment elbow. Non-default adjustments are reported.
            middle = (start[0] + end[0]) / 2
            points = [start, (middle, start[1]), (middle, end[1]), end]
        elif preset not in ('line', 'straightConnector1'):
            self._warnings.add(f'Unsupported connector geometry: {preset}')
            return
        draw = ImageDraw.Draw(canvas)
        draw.line(points, fill=color, width=width)
        for tag, tip, other in [('a:headEnd', points[0], points[1]), ('a:tailEnd', points[-1], points[-2])]:
            arrow = _find(line, tag)
            if arrow is not None and arrow.get('type', 'none') != 'none':
                angle = math.atan2(tip[1] - other[1], tip[0] - other[0])
                length = max(6, width * 4)
                draw.polygon([tip, (tip[0] - length * math.cos(angle - 0.45), tip[1] - length * math.sin(angle - 0.45)),
                              (tip[0] - length * math.cos(angle + 0.45), tip[1] - length * math.sin(angle + 0.45))], fill=color)

    def _paragraph_defaults(self, shape, paragraph):
        level = paragraph.level + 1
        defaults = [paragraph._p.find(qn('a:pPr')),
                    _find(shape._element, f'p:txBody/a:lstStyle/a:lvl{level}pPr')]
        if shape.is_placeholder:
            index = shape.placeholder_format.idx
            inherited = next((p for p in self._slide.slide_layout.placeholders if p.placeholder_format.idx == index), None)
            if inherited is not None:
                defaults.extend([_find(inherited._element, 'p:txBody/a:p/a:pPr'),
                                 _find(inherited._element, f'p:txBody/a:lstStyle/a:lvl{level}pPr')])
        master = self._slide.slide_layout.slide_master
        kind = 'otherStyle'
        if shape.is_placeholder:
            kind = 'titleStyle' if 'TITLE' in str(shape.placeholder_format.type) else 'bodyStyle'
        defaults.append(_find(master._element, f'p:txStyles/p:{kind}/a:lvl{level}pPr'))
        defaults.append(_find(self.presentation._element, f'p:defaultTextStyle/a:lvl{level}pPr'))
        return defaults

    def _font_family(self, properties, char):
        tag = 'a:ea' if ord(char) > 0x2e80 else 'a:latin'
        face = _first(_find(p, tag) for p in properties)
        if face is None:
            face = _first(_find(p, 'a:latin') for p in properties)
        family = face.get('typeface', '') if face is not None else '+mn-lt'
        if family.startswith('+'):
            kind = 'majorFont' if family.startswith('+mj') else 'minorFont'
            root = _find(self._theme, f'a:themeElements/a:fontScheme/a:{kind}')
            font = _find(root, 'a:ea' if family.endswith('-ea') else 'a:latin')
            family = font.get('typeface', '') if font is not None else ''
            if not family and root is not None:
                family = next((c.get('typeface') for c in root if c.get('script') == 'Hang'), '')
        return family

    def _font(self, family: str, size: int):
        key = (family, size)
        if key not in self._fonts:
            path = _find_font_file_for_family(family) if family else None
            if path is not None:
                try:
                    self._fonts[key] = ImageFont.truetype(str(path), size)
                except OSError:
                    path = None
            if path is None:
                self._warnings.add(f'Font substituted: {family or "unspecified"}')
                self._fonts[key] = _load_capture_font(size)
        # Report substitutions on each slide even when its font was cached.
        if family and _find_font_file_for_family(family) is None:
            self._warnings.add(f'Font substituted: {family}')
        return self._fonts[key]

    def _glyph_font(self, family: str, size: int, char: str):
        font = self._font(family, size)
        key = (family, size, char)
        if key not in self._glyph_fonts:
            chosen = font
            if not char.isspace() and font.getmask(char).getbbox() is None:
                # Fall back per glyph, not for the whole paragraph.
                for path in _GLYPH_FALLBACK_PATHS:
                    try:
                        candidate = ImageFont.truetype(path, size)
                    except OSError:
                        continue
                    if candidate.getmask(char).getbbox() is not None:
                        chosen = candidate
                        break
            self._glyph_fonts[key] = chosen
        chosen = self._glyph_fonts[key]
        if chosen is not font:
            self._warnings.add(f'Glyph font substituted: {char}')
        elif not char.isspace() and chosen.getmask(char).getbbox() is None:
            self._warnings.add(f'Missing glyph: U+{ord(char):04X}')
        return chosen

    def _draw_text(self, layer, frame, shape, sx, sy, *, margins=None):
        left, top, right, bottom = margins or (frame.margin_left, frame.margin_top, frame.margin_right, frame.margin_bottom)
        left, right = round(left * sx), round(right * sx)
        top, bottom = round(top * sy), round(bottom * sy)
        available = max(1, layer.width - left - right)
        body = frame._txBody.bodyPr
        norm = _find(body, 'a:normAutofit')
        font_scale = int(norm.get('fontScale', '100000')) / 100000 if norm is not None else 1
        lines = []
        y = 0.0
        for paragraph in frame.paragraphs:
            defaults = self._paragraph_defaults(shape, paragraph)
            run_defaults = [_find(p, 'a:defRPr') for p in defaults]
            fallback = _first([_find(paragraph._p, 'a:endParaRPr'), *run_defaults])
            default_size = float(_attribute([fallback, *run_defaults], 'sz', '1800')) / 100
            glyphs = []
            for child in paragraph._p:
                if child.tag == qn('a:br'):
                    glyphs.append(None)
                    continue
                if child.tag not in (qn('a:r'), qn('a:fld')):
                    continue
                props = [_find(child, 'a:rPr'), *run_defaults]
                text = _find(child, 'a:t')
                for char in text.text or '' if text is not None else '':
                    size = max(1, round(float(_attribute(props, 'sz', str(default_size * 100))) / 100 * _EMU_PER_PT * sy * font_scale))
                    font = self._glyph_font(self._font_family(props, char), size, char)
                    color = self._color(_first(_find(p, 'a:solidFill') for p in props))
                    bold = _attribute(props, 'b', '0') in ('1', 'true')
                    glyphs.append(_Glyph(char, font, color, bold, font.getlength(char)))
            base_height = max(1, default_size * _EMU_PER_PT * sy * font_scale * 1.2)
            indent = int(_attribute(defaults, 'marL', '0')) * sx
            first_indent = int(_attribute(defaults, 'indent', '0')) * sx
            align = _attribute(defaults, 'algn', 'l')
            def spacing(tag, height, default):
                element = _first(_find(p, tag) for p in defaults)
                if element is None or not len(element):
                    return default
                value = int(element[0].get('val', '0'))
                return value / 100 * _EMU_PER_PT * sy if element[0].tag == qn('a:spcPts') else height * value / 100000
            # spcBef is not applied above the first paragraph in a text frame.
            if lines:
                y += spacing('a:spcBef', base_height, 0)
            current = []
            line_width = 0.0
            offset = indent + first_indent
            def finish():
                nonlocal y, current, line_width, offset
                height = max([g.font.size * 1.2 for g in current] or [base_height])
                step = spacing('a:lnSpc', height, height)
                lines.append((list(current), left + offset, y, line_width, align, available - offset))
                y += max(1, step)
                current, line_width, offset = [], 0.0, indent
            for glyph in glyphs:
                if glyph is None:
                    finish()
                    continue
                if body.get('wrap', 'square') != 'none' and current and line_width + glyph.width > available - offset:
                    # Prefer the last whitespace break for Latin words. CJK
                    # can wrap between glyphs without requiring spaces.
                    split = next((i for i in range(len(current) - 1, -1, -1) if current[i].text.isspace()), -1)
                    if split > 0 and ord(glyph.text) < 0x2e80:
                        tail = current[split + 1:]
                        current = current[:split]
                        line_width = sum(g.width for g in current)
                        finish()
                        current = tail
                        line_width = sum(g.width for g in tail)
                    else:
                        finish()
                current.append(glyph)
                line_width += glyph.width
            finish()
            y += spacing('a:spcAft', base_height, 0)
        free_height = layer.height - top - bottom
        anchor = body.get('anchor', 't')
        dy = top + (max(0, free_height - y) / 2 if anchor == 'ctr' else max(0, free_height - y) if anchor == 'b' else 0)
        content_width = max([available, *[x - left + width for _, x, _, width, _, _ in lines]])
        fit = min(1.0, max(1, free_height) / max(1, y), available / max(1, content_width))
        # A substituted font can be wider/taller than the source. Preserve the
        # text in its own box by fitting it uniformly instead of losing lines.
        natural_size = (max(layer.width, math.ceil(left + content_width + right)),
                        max(layer.height, math.ceil(top + y + bottom)))
        if natural_size[0] * natural_size[1] > _MAX_LAYER_PIXELS:
            raise ValueError('oversized text layer')
        text_layer = Image.new('RGBA', natural_size)
        draw = ImageDraw.Draw(text_layer)
        for glyphs, x, line_y, width, align, limit in lines:
            x += max(0, limit - width) / 2 if align == 'ctr' else max(0, limit - width) if align == 'r' else 0
            baseline = dy + line_y + max([g.font.getmetrics()[0] for g in glyphs] or [0])
            for glyph in glyphs:
                draw.text((x, baseline), glyph.text, font=glyph.font, fill=glyph.color, anchor='ls',
                          stroke_width=1 if glyph.bold and glyph.font.size >= 16 else 0, stroke_fill=glyph.color)
                x += glyph.width
        if fit < 0.995:
            self._warnings.add(f'{shape.name}: text fitted to box ({fit:.0%})')
            content = text_layer.crop((left, top, math.ceil(left + max(1, content_width)), math.ceil(top + max(1, y))))
            content = content.resize((max(1, round(content.width * fit)), max(1, round(content.height * fit))), Image.Resampling.LANCZOS)
            alignment = lines[0][4] if lines and all(line[4] == lines[0][4] for line in lines) else 'l'
            dx = left + (max(0, available - content.width) / 2 if alignment == 'ctr' else max(0, available - content.width) if alignment == 'r' else 0)
            dy = top + (max(0, free_height - content.height) / 2 if anchor == 'ctr'
                        else max(0, free_height - content.height) if anchor == 'b' else 0)
            layer.alpha_composite(content, (round(dx), round(dy)))
        else:
            layer.alpha_composite(text_layer.crop((0, 0, layer.width, layer.height)))

    def _draw_table(self, layer, shape, sx, sy):
        table = shape.table
        if _find(table._tbl, 'a:tblPr/a:tableStyleId') is not None:
            self._warnings.add('Table theme styles approximated; explicit cell formatting preserved')
        xs, ys = [0], [0]
        for column in table.columns:
            xs.append(xs[-1] + column.width * sx)
        for row in table.rows:
            ys.append(ys[-1] + row.height * sy)
        draw = ImageDraw.Draw(layer)
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                if cell.is_spanned:
                    continue
                box = tuple(round(v) for v in (xs[ci], ys[ri], xs[ci + cell.span_width], ys[ri + cell.span_height]))
                props = cell._tc.tcPr
                fill = self._fill(props, (255, 255, 255, 255))
                draw.rectangle(box, fill=fill)
                tile = Image.new('RGBA', (max(1, box[2] - box[0]), max(1, box[3] - box[1])))
                self._draw_text(tile, cell.text_frame, shape, sx, sy,
                                margins=(cell.margin_left, cell.margin_top, cell.margin_right, cell.margin_bottom))
                layer.alpha_composite(tile, (box[0], box[1]))
                for tag, points in [('a:lnL', (box[0], box[1], box[0], box[3])),
                                    ('a:lnR', (box[2], box[1], box[2], box[3])),
                                    ('a:lnT', (box[0], box[1], box[2], box[1])),
                                    ('a:lnB', (box[0], box[3], box[2], box[3]))]:
                    line = _find(props, tag)
                    color = self._fill(line, (170, 170, 170, 255))
                    width = max(1, round(int(line.get('w', '12700')) * sx)) if line is not None else 1
                    draw.line(points, fill=color, width=width)
