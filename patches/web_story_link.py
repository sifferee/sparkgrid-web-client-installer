#!/usr/bin/env python3
"""Instagram Web private-API Story publisher.

WEB ONLY.

Pipeline:
  1. Prepare a 1080x1920 JPEG in Python with Pillow.
  2. POST the JPEG to i.instagram.com/rupload_igphoto.
  3. POST upload_id + optional story_link_stickers to
     www.instagram.com/api/v1/web/create/configure_to_story/.

The authenticated Playwright BrowserContext supplies Instagram cookies.
No normal Create Post composer is used.
"""

from __future__ import annotations

import io
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

try:
    from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter
except Exception as exc:  # pragma: no cover - clear runtime error on user machine
    Image = None
    ImageOps = None
    ImageDraw = None
    ImageFont = None
    ImageFilter = None
    _PIL_IMPORT_ERROR = exc
else:
    _PIL_IMPORT_ERROR = None


IG_APP_ID = "936619743392459"
IG_ASBD_ID = "129477"
STORY_WIDTH = 1080
STORY_HEIGHT = 1920


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _trace_path(dump: Any) -> Optional[Path]:
    root = getattr(dump, "root", None)
    if root is None:
        return None
    try:
        path = Path(root) / "story_private_api_trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        return None


def _trace(dump: Any, event: str, **payload: Any) -> None:
    path = _trace_path(dump)
    if path is None:
        return

    safe = {
        "ts": _now_iso(),
        "event": event,
        **payload,
    }

    for key in list(safe):
        lowered = key.lower()
        if any(
            token in lowered
            for token in (
                "cookie",
                "csrf",
                "session",
                "authorization",
                "claim_value",
            )
        ):
            safe[key] = "<redacted>"
        # Keep incident telemetry compact. API responses and URLs may contain
        # account/media metadata and are not required to diagnose a stage.
        elif any(token in lowered for token in ("url", "text", "json", "form", "input_name")):
            safe[key] = "<redacted>"

    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    safe,
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
    except Exception:
        pass


def _capture(
    dump: Any,
    page: Any,
    state: str,
    action: str,
) -> None:
    if dump is None:
        return
    try:
        dump.capture(
            page,
            state,
            action,
            force_snapshot=True,
        )
    except TypeError:
        try:
            dump.capture(
                page,
                state,
                note=action,
                force_snapshot=True,
            )
        except Exception:
            pass
    except Exception:
        pass


def _response_text(response: Any, limit: int = 2000) -> str:
    try:
        return response.text()[:limit]
    except Exception as exc:
        return f"<response text unavailable: {type(exc).__name__}>"


def _response_json(response: Any) -> Optional[Dict[str, Any]]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {"value": value}
    except Exception:
        return None


def _session_material(page: Any) -> Dict[str, str]:
    context = page.context
    cookies: Dict[str, str] = {}

    try:
        cookie_items = context.cookies(
            [
                "https://www.instagram.com/",
                "https://i.instagram.com/",
            ]
        )
    except Exception:
        try:
            cookie_items = context.cookies()
        except Exception:
            cookie_items = []

    for item in cookie_items:
        name = str(item.get("name") or "")
        if name:
            cookies[name] = str(item.get("value") or "")

    try:
        user_agent = str(
            page.evaluate("() => navigator.userAgent")
            or ""
        )
    except Exception:
        user_agent = ""

    try:
        www_claim = str(
            page.evaluate(
                "() => sessionStorage.getItem('www-claim-v2') || ''"
            )
            or ""
        )
    except Exception:
        www_claim = ""

    return {
        "csrftoken": cookies.get("csrftoken", ""),
        "mid": cookies.get("mid", ""),
        "ig_did": cookies.get("ig_did", ""),
        "user_agent": user_agent,
        "www_claim": www_claim,
    }


def _headers(
    session: Dict[str, str],
    *,
    referer: str,
) -> Dict[str, str]:
    headers = {
        "accept": "*/*",
        "origin": "https://www.instagram.com",
        "referer": referer,
        "x-csrftoken": session["csrftoken"],
        "x-ig-app-id": IG_APP_ID,
        "x-asbd-id": IG_ASBD_ID,
        "x-requested-with": "XMLHttpRequest",
    }

    if session.get("user_agent"):
        headers["user-agent"] = session["user_agent"]
    if session.get("mid"):
        headers["x-mid"] = session["mid"]
    if session.get("ig_did"):
        headers["x-ig-device-id"] = session["ig_did"]
    if session.get("www_claim"):
        headers["x-ig-www-claim"] = session["www_claim"]

    return headers



def _load_sticker_font(size: int, *, bold: bool = False):
    """Load a local system font without bundling or exporting font files."""

    candidates = []
    if bold:
        candidates.extend(
            [
                "/System/Library/Fonts/SFNSRounded.ttf",
                "/System/Library/Fonts/SFNS.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
                "/Library/Fonts/Arial Bold.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "/System/Library/Fonts/SFNSRounded.ttf",
                "/System/Library/Fonts/SFNS.ttf",
                "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica.ttf",
                "/Library/Fonts/Arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
        )

    for candidate in candidates:
        try:
            path = Path(candidate)
            if path.is_file():
                return ImageFont.truetype(str(path), size=size)
        except Exception:
            pass

    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _display_domain(link_url: str) -> str:
    try:
        parsed = urlparse(link_url)
        host = (parsed.netloc or parsed.path or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return host[:42] or "link destination"
    except Exception:
        return "link destination"


def _normalise_sticker_text(value: str) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split()).strip()
    return (text or "Open link")[:80]


def _extract_down_indicator(value: str) -> tuple[str, bool, str]:
    """Separate a down marker so it can be rendered reliably across desktop OSes."""

    text = _normalise_sticker_text(value)
    down_emoji = ""
    if "👇" in text:
        index = text.index("👇")
        down_emoji = "👇"
        if index + 1 < len(text) and 0x1F3FB <= ord(text[index + 1]) <= 0x1F3FF:
            down_emoji += text[index + 1]
        elif index + 1 < len(text) and ord(text[index + 1]) in {0xFE0F, 0xFE0E}:
            down_emoji += text[index + 1]
    has_down = bool(down_emoji) or any(marker in text for marker in ("↓", "⇩", "⬇"))
    if has_down:
        for marker in ("👇", "↓", "⇩", "⬇"):
            text = text.replace(marker, "")
        text = "".join(
            char
            for char in text
            if ord(char) not in {0xFE0F, 0xFE0E}
            and not (0x1F3FB <= ord(char) <= 0x1F3FF)
        )
        text = " ".join(text.split()).strip()
    return (text or "Open link"), has_down, down_emoji


def _render_system_emoji(emoji_text: str, target_size: int):
    """Render a system color emoji when Pillow and the host OS support it.

    Apple Color Emoji exposes fixed bitmap strikes on some Pillow versions, so
    several sizes are attempted. Returning None activates the vector fallback.
    """

    if not emoji_text or Image is None or ImageDraw is None or ImageFont is None:
        return None

    candidates = [
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "/System/Library/Fonts/Apple Color Emoji.ttf",
        "C:/Windows/Fonts/seguiemj.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    ]
    requested_sizes = [160, 128, 109, 96, 64, 48, 32, 20]

    for candidate in candidates:
        path = Path(candidate)
        if not path.is_file():
            continue
        for font_size in requested_sizes:
            try:
                font = ImageFont.truetype(str(path), size=font_size)
            except Exception:
                continue
            side = max(256, font_size * 3)
            tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            draw = ImageDraw.Draw(tile)
            try:
                draw.text(
                    (side // 4, side // 4),
                    emoji_text,
                    font=font,
                    embedded_color=True,
                )
            except TypeError:
                try:
                    draw.text((side // 4, side // 4), emoji_text, font=font)
                except Exception:
                    continue
            except Exception:
                continue

            bbox = tile.getbbox()
            if not bbox:
                continue
            crop = tile.crop(bbox)
            crop.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
            output = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
            output.alpha_composite(
                crop,
                ((target_size - crop.width) // 2, (target_size - crop.height) // 2),
            )
            return output
    return None


def _draw_chain_icon(draw, box, color):
    """Draw a small chain-link icon using only Pillow primitives."""

    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    stroke = max(4, int(min(width, height) * 0.12))

    a = (
        int(left + width * 0.02),
        int(top + height * 0.28),
        int(left + width * 0.58),
        int(top + height * 0.80),
    )
    b = (
        int(left + width * 0.42),
        int(top + height * 0.08),
        int(left + width * 0.98),
        int(top + height * 0.60),
    )

    radius = max(6, int(height * 0.20))
    try:
        draw.rounded_rectangle(a, radius=radius, outline=color, width=stroke)
        draw.rounded_rectangle(b, radius=radius, outline=color, width=stroke)
    except Exception:
        draw.rectangle(a, outline=color, width=stroke)
        draw.rectangle(b, outline=color, width=stroke)

    draw.line(
        (
            int(left + width * 0.37),
            int(top + height * 0.60),
            int(left + width * 0.63),
            int(top + height * 0.38),
        ),
        fill=color,
        width=stroke,
    )


def _text_width(draw, text: str, font) -> int:
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return max(0, int(box[2] - box[0]))
    except Exception:
        try:
            return int(draw.textlength(text, font=font))
        except Exception:
            return len(text) * 20


def _fit_sticker_text(draw, text: str, max_width: int, max_size: int, min_size: int):
    """Fit one-line CTA text and ellipsize only as a last resort."""

    for size in range(max_size, min_size - 1, -2):
        font = _load_sticker_font(size, bold=True)
        if _text_width(draw, text, font) <= max_width:
            return text, font, size

    font = _load_sticker_font(min_size, bold=True)
    candidate = text
    while len(candidate) > 4 and _text_width(draw, candidate + "…", font) > max_width:
        candidate = candidate[:-1].rstrip()
    return (candidate + "…" if candidate != text else candidate), font, min_size


def _draw_vertical_gradient(size, top_color, bottom_color):
    width, height = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    denominator = max(1, height - 1)
    for yy in range(height):
        t = yy / denominator
        color = tuple(
            int(round(top_color[i] * (1.0 - t) + bottom_color[i] * t))
            for i in range(4)
        )
        draw.line((0, yy, width, yy), fill=color)
    return layer


def _bake_visible_link_sticker(
    image,
    link_url: str,
    x: float,
    y: float,
    sticker_text: str = "Chat with me👇🏻",
    width_ratio: float = 0.60,
    height_ratio: float = 0.10,
):
    """Bake a premium custom CTA under Instagram's transparent hitbox.

    The normalized center and size exactly match story_link_stickers metadata,
    so the entire visible custom sticker remains tappable.
    """

    if not link_url:
        return {
            "drawn": False,
            "reason": "no_link",
        }

    original_text = _normalise_sticker_text(sticker_text)
    display_text, down_indicator, down_emoji = _extract_down_indicator(original_text)

    canvas = image.convert("RGBA")
    w, h = canvas.size

    cx = max(0.05, min(0.95, float(x))) * w
    cy = max(0.05, min(0.95, float(y))) * h

    sticker_w = int(max(420, min(w * 0.78, w * float(width_ratio))))
    sticker_h = int(max(132, min(h * 0.16, h * float(height_ratio))))

    left = int(cx - sticker_w / 2)
    top = int(cy - sticker_h / 2)
    left = max(24, min(w - sticker_w - 24, left))
    top = max(24, min(h - sticker_h - 24, top))
    right = left + sticker_w
    bottom = top + sticker_h
    radius = max(30, int(sticker_h * 0.25))

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (left + 9, top + 14, right + 9, bottom + 14),
        radius=radius,
        fill=(0, 0, 0, 145),
    )
    try:
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))
    except Exception:
        pass
    canvas.alpha_composite(shadow)

    sticker_layer = Image.new("RGBA", (sticker_w, sticker_h), (0, 0, 0, 0))
    mask = Image.new("L", (sticker_w, sticker_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, sticker_w - 1, sticker_h - 1),
        radius=radius,
        fill=255,
    )
    gradient = _draw_vertical_gradient(
        (sticker_w, sticker_h),
        (39, 33, 62, 247),
        (12, 14, 23, 247),
    )
    sticker_layer.paste(gradient, (0, 0), mask)
    layer_draw = ImageDraw.Draw(sticker_layer)
    layer_draw.rounded_rectangle(
        (1, 1, sticker_w - 2, sticker_h - 2),
        radius=radius,
        outline=(255, 255, 255, 52),
        width=3,
    )

    # Subtle top sheen gives the glass card a deliberate premium finish.
    sheen_h = max(22, int(sticker_h * 0.28))
    sheen = Image.new("RGBA", (sticker_w, sheen_h), (0, 0, 0, 0))
    sheen_mask = Image.new("L", (sticker_w, sheen_h), 0)
    ImageDraw.Draw(sheen_mask).rounded_rectangle(
        (0, 0, sticker_w - 1, sheen_h * 2),
        radius=radius,
        fill=95,
    )
    sheen_fill = Image.new("RGBA", (sticker_w, sheen_h), (255, 255, 255, 18))
    sheen.paste(sheen_fill, (0, 0), sheen_mask)
    sticker_layer.alpha_composite(sheen, (0, 0))

    padding = int(sticker_h * 0.13)
    icon_box = int(sticker_h * 0.62)
    icon_left = padding
    icon_top = (sticker_h - icon_box) // 2
    icon_radius = max(18, int(icon_box * 0.30))

    icon_layer = _draw_vertical_gradient(
        (icon_box, icon_box),
        (145, 101, 255, 255),
        (55, 145, 255, 255),
    )
    icon_mask = Image.new("L", (icon_box, icon_box), 0)
    ImageDraw.Draw(icon_mask).rounded_rectangle(
        (0, 0, icon_box - 1, icon_box - 1),
        radius=icon_radius,
        fill=255,
    )
    sticker_layer.paste(icon_layer, (icon_left, icon_top), icon_mask)
    _draw_chain_icon(
        layer_draw,
        (
            icon_left + int(icon_box * 0.24),
            icon_top + int(icon_box * 0.24),
            icon_left + int(icon_box * 0.76),
            icon_top + int(icon_box * 0.76),
        ),
        (255, 255, 255, 255),
    )

    indicator_space = int(sticker_h * 0.43) if down_indicator else 0
    text_left = icon_left + icon_box + int(sticker_h * 0.13)
    text_right = sticker_w - padding - indicator_space
    text_width = max(120, text_right - text_left)

    domain = "click it"
    title, title_font, title_size = _fit_sticker_text(
        layer_draw,
        display_text,
        text_width,
        max_size=max(38, int(sticker_h * 0.27)),
        min_size=max(24, int(sticker_h * 0.17)),
    )
    domain_font = _load_sticker_font(max(20, int(sticker_h * 0.135)), bold=False)

    title_y = int(sticker_h * 0.20)
    domain_y = int(sticker_h * 0.61)
    layer_draw.text(
        (text_left, title_y),
        title,
        font=title_font,
        fill=(255, 255, 255, 255),
    )
    layer_draw.text(
        (text_left, domain_y),
        domain,
        font=domain_font,
        fill=(190, 192, 205, 255),
    )

    indicator_style = "none"
    if down_indicator:
        center_x = sticker_w - padding - int(sticker_h * 0.17)
        center_y = sticker_h // 2
        emoji_size = max(52, int(sticker_h * 0.34))
        emoji_tile = _render_system_emoji(down_emoji, emoji_size) if down_emoji else None
        if emoji_tile is not None:
            sticker_layer.alpha_composite(
                emoji_tile,
                (center_x - emoji_size // 2, center_y - emoji_size // 2),
            )
            indicator_style = "system_emoji"
        else:
            circle_r = int(sticker_h * 0.16)
            layer_draw.ellipse(
                (
                    center_x - circle_r,
                    center_y - circle_r,
                    center_x + circle_r,
                    center_y + circle_r,
                ),
                fill=(255, 255, 255, 24),
                outline=(255, 255, 255, 58),
                width=2,
            )
            stroke = max(4, int(sticker_h * 0.028))
            arrow_top = center_y - int(circle_r * 0.45)
            arrow_bottom = center_y + int(circle_r * 0.48)
            layer_draw.line(
                (center_x, arrow_top, center_x, arrow_bottom),
                fill=(255, 255, 255, 242),
                width=stroke,
            )
            layer_draw.line(
                (
                    center_x - int(circle_r * 0.35),
                    center_y + int(circle_r * 0.15),
                    center_x,
                    arrow_bottom,
                    center_x + int(circle_r * 0.35),
                    center_y + int(circle_r * 0.15),
                ),
                fill=(255, 255, 255, 242),
                width=stroke,
                joint="curve",
            )
            indicator_style = "vector_arrow"

    canvas.alpha_composite(sticker_layer, (left, top))
    image.paste(canvas.convert("RGB"))

    return {
        "drawn": True,
        "style": "premium_glass",
        "x": float(x),
        "y": float(y),
        "width": float(width_ratio),
        "height": float(height_ratio),
        "pixel_box": [left, top, right, bottom],
        "input_text": original_text,
        "display_text": title,
        "title_font_size": title_size,
        "down_indicator": bool(down_indicator),
        "indicator_style": indicator_style,
        "domain": domain,
    }

def _prepare_story_jpeg(
    image_path: str,
    link_url: str = "",
    x: float = 0.5,
    y: float = 0.82,
    sticker_text: str = "Chat with me👇🏻",
) -> Dict[str, Any]:
    """Create a cover-fit 1080x1920 JPEG entirely in Python."""

    if Image is None or ImageOps is None:
        return {
            "ok": False,
            "error": (
                "Pillow is not installed: "
                + f"{type(_PIL_IMPORT_ERROR).__name__}: {_PIL_IMPORT_ERROR}"
            ),
        }

    path = Path(image_path).expanduser()

    if not path.exists() or not path.is_file():
        return {
            "ok": False,
            "error": f"story image not found: {path}",
        }

    try:
        with Image.open(path) as source:
            source = ImageOps.exif_transpose(source)

            if source.mode not in ("RGB", "RGBA"):
                source = source.convert("RGBA")

            if source.mode == "RGBA":
                background = Image.new(
                    "RGBA",
                    source.size,
                    (0, 0, 0, 255),
                )
                background.alpha_composite(source)
                source = background.convert("RGB")
            else:
                source = source.convert("RGB")

            src_w, src_h = source.size
            if src_w <= 0 or src_h <= 0:
                raise ValueError("invalid source image dimensions")

            scale = max(
                STORY_WIDTH / src_w,
                STORY_HEIGHT / src_h,
            )

            dst_w = max(
                STORY_WIDTH,
                int(round(src_w * scale)),
            )
            dst_h = max(
                STORY_HEIGHT,
                int(round(src_h * scale)),
            )

            resized = source.resize(
                (dst_w, dst_h),
                Image.Resampling.LANCZOS,
            )

            left = max(0, (dst_w - STORY_WIDTH) // 2)
            top = max(0, (dst_h - STORY_HEIGHT) // 2)
            cropped = resized.crop(
                (
                    left,
                    top,
                    left + STORY_WIDTH,
                    top + STORY_HEIGHT,
                )
            )

            visual_sticker = _bake_visible_link_sticker(
                cropped,
                link_url,
                x,
                y,
                sticker_text=sticker_text,
                width_ratio=0.60,
                height_ratio=0.10,
            )

            output = io.BytesIO()
            cropped.save(
                output,
                format="JPEG",
                quality=92,
                optimize=True,
                progressive=False,
                subsampling=0,
            )

            jpeg = output.getvalue()

    except Exception as exc:
        return {
            "ok": False,
            "error": (
                f"image conversion failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        }

    if not jpeg:
        return {
            "ok": False,
            "error": "prepared JPEG is empty",
        }

    return {
        "ok": True,
        "jpeg": jpeg,
        "size": len(jpeg),
        "width": STORY_WIDTH,
        "height": STORY_HEIGHT,
        "visual_sticker": visual_sticker,
    }


def _accepted_story_link_stickers(response_json: Any) -> list[Dict[str, Any]]:
    """Return native link-sticker objects confirmed by Instagram."""

    if not isinstance(response_json, dict):
        return []
    media = response_json.get("media")
    if not isinstance(media, dict):
        return []
    stickers = media.get("story_link_stickers")
    if not isinstance(stickers, list):
        return []
    return [item for item in stickers if isinstance(item, dict)]


def post_story_with_link(
    page: Any,
    image_path: str,
    link_url: str = "",
    x: float = 0.5,
    y: float = 0.82,
    dump: Any = None,
    stage_callback: Any = None,
    sticker_text: str = "Chat with me👇🏻",
) -> Dict[str, Any]:
    """Publish an image Story with an optional tappable link sticker."""

    def stage(name: str, **details: Any) -> None:
        """Advance a durable stage without exporting sensitive API data."""
        _trace(dump, "stage", stage=name, **details)
        if callable(stage_callback):
            try:
                stage_callback(name)
            except Exception:
                pass

    stage("story_preflight")
    link_url = str(link_url or "").strip()

    if link_url and not link_url.lower().startswith(
        ("http://", "https://")
    ):
        link_url = "https://" + link_url

    session = _session_material(page)

    if not session.get("csrftoken"):
        result = {
            "ok": False,
            "step": "cookies",
            "error": "no csrftoken in authenticated BrowserContext",
        }
        _trace(dump, "failed", **result)
        _capture(
            dump,
            page,
            "post_story_fail_cookies",
            str(result),
        )
        return result

    prepared = _prepare_story_jpeg(
        image_path,
        link_url=link_url,
        x=float(x),
        y=float(y),
        sticker_text=sticker_text,
    )

    if not prepared.get("ok"):
        result = {
            "ok": False,
            "step": "image",
            "error": prepared.get("error"),
        }
        _trace(dump, "failed", **result)
        _capture(
            dump,
            page,
            "post_story_fail_image",
            str(result),
        )
        return result

    stage("story_media_attached", media_category="image")
    jpeg = prepared["jpeg"]
    upload_id = str(int(time.time() * 1000))
    entity_name = (
        f"{upload_id}_0_{random.randint(100000000, 999999999)}"
    )

    rupload_params = {
        "upload_id": upload_id,
        "media_type": 1,
        "upload_media_width": STORY_WIDTH,
        "upload_media_height": STORY_HEIGHT,
    }

    api = page.context.request
    upload_url = (
        "https://i.instagram.com/rupload_igphoto/"
        + entity_name
    )

    upload_headers = _headers(
        session,
        referer="https://www.instagram.com/",
    )
    upload_headers.update(
        {
            "content-type": "image/jpeg",
            "offset": "0",
            "x-entity-name": entity_name,
            "x-entity-length": str(len(jpeg)),
            "x-entity-type": "image/jpeg",
            "x-instagram-rupload-params": json.dumps(
                rupload_params,
                separators=(",", ":"),
            ),
        }
    )

    _trace(
        dump,
        "image_prepared",
        input_name=Path(image_path).name,
        output_size=len(jpeg),
        width=STORY_WIDTH,
        height=STORY_HEIGHT,
        visual_sticker=prepared.get("visual_sticker"),
    )

    _trace(
        dump,
        "rupload_request",
        method="POST",
        url=upload_url,
        entity_name=entity_name,
        entity_length=len(jpeg),
        upload_id=upload_id,
        header_names=sorted(upload_headers),
    )

    try:
        upload_response = api.post(
            upload_url,
            data=jpeg,
            headers=upload_headers,
            timeout=120000,
            fail_on_status_code=False,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "step": "rupload_transport",
            "error": f"{type(exc).__name__}: {exc}",
            "upload_id": upload_id,
        }
        _trace(dump, "rupload_exception", **result)
        _capture(
            dump,
            page,
            "post_story_fail_rupload_transport",
            str(result),
        )
        return result

    upload_text = _response_text(upload_response)
    upload_json = _response_json(upload_response)

    _trace(
        dump,
        "rupload_response",
        status=upload_response.status,
        ok=upload_response.ok,
        upload_id=upload_id,
        response_json=upload_json,
        response_text=(
            upload_text
            if upload_json is None
            else "<json captured separately>"
        ),
    )

    if not upload_response.ok:
        result = {
            "ok": False,
            "step": "rupload",
            "status": upload_response.status,
            "text": upload_text,
            "json": upload_json,
            "upload_id": upload_id,
        }
        _capture(
            dump,
            page,
            "post_story_fail_rupload",
            str(result)[:900],
        )
        return result

    stage("story_preview_ready", media_category="image")
    if isinstance(upload_json, dict):
        returned_upload_id = str(
            upload_json.get("upload_id")
            or ""
        ).strip()
        if returned_upload_id:
            upload_id = returned_upload_id

    form = {
        "upload_id": upload_id,
    }

    if link_url:
        sticker = {
            "x": max(0.0, min(1.0, float(x))),
            "y": max(0.0, min(1.0, float(y))),
            "width": 0.6,
            "height": 0.1,
            "rotation": 0.0,
            "link_type": "web",
            "url": link_url,
        }
        form["story_link_stickers"] = json.dumps(
            [sticker],
            separators=(",", ":"),
        )

    configure_url = (
        "https://www.instagram.com/"
        "api/v1/web/create/configure_to_story/"
    )
    configure_headers = _headers(
        session,
        referer="https://www.instagram.com/",
    )

    last_status = 0
    last_text = ""
    last_json = None

    # A configure request crosses the Story publication boundary.  Do not
    # replay it after a timeout/ambiguous response; the caller will retain an
    # unverified submission for manual confirmation instead.
    for attempt in range(1, 2):

        _trace(
            dump,
            "configure_request",
            method="POST",
            url=configure_url,
            attempt=attempt,
            upload_id=upload_id,
            link_requested=bool(link_url),
            form_keys=sorted(form),
            header_names=sorted(configure_headers),
        )

        # configure_to_story is the irreversible publish boundary: a lost
        # response must never lead to a blind second publication.
        stage("story_publish_intent")
        try:
            response = api.post(
                configure_url,
                form=form,
                headers=configure_headers,
                timeout=90000,
                fail_on_status_code=False,
            )
        except Exception as exc:
            last_status = 0
            last_text = f"{type(exc).__name__}: {exc}"
            last_json = None
            _trace(
                dump,
                "configure_exception",
                attempt=attempt,
                error=last_text,
            )
            continue

        last_status = response.status
        stage("story_publish_clicked")
        last_text = _response_text(response)
        last_json = _response_json(response)

        _trace(
            dump,
            "configure_response",
            attempt=attempt,
            status=response.status,
            ok=response.ok,
            upload_id=upload_id,
            response_json=last_json,
            response_text=(
                last_text
                if last_json is None
                else "<json captured separately>"
            ),
        )

        combined = (
            json.dumps(last_json, ensure_ascii=False)
            if last_json is not None
            else last_text
        )

        normalized = combined.replace(" ", "").lower()
        transcode_pending = (
            "transcodenotfinished" in normalized
            or "transcode_not_finished" in normalized
        )

        status_ok = False
        if isinstance(last_json, dict):
            status_ok = (
                str(last_json.get("status") or "").lower()
                == "ok"
            )
        else:
            status_ok = (
                '"status":"ok"' in normalized
            )

        if response.ok and status_ok and not transcode_pending:
            accepted_stickers = _accepted_story_link_stickers(last_json)

            if link_url and not accepted_stickers:
                result = {
                    "ok": False,
                    "step": "link_sticker_missing",
                    "status": response.status,
                    "upload_id": upload_id,
                    "link_requested": True,
                    "sticker_accepted": False,
                    "visual_sticker": prepared.get("visual_sticker"),
                    "error": (
                        "Story was configured, but Instagram did not return "
                        "story_link_stickers in the published media."
                    ),
                }
                _trace(dump, "failed", **result)
                _capture(
                    dump,
                    page,
                    "post_story_fail_link_sticker_missing",
                    str(result)[:900],
                )
                return result

            first_sticker = accepted_stickers[0] if accepted_stickers else {}
            story_link = (
                first_sticker.get("story_link")
                if isinstance(first_sticker, dict)
                else {}
            ) or {}
            result = {
                "ok": True,
                "step": "done",
                "upload_id": upload_id,
                "link_requested": bool(link_url),
                "sticker_accepted": bool(accepted_stickers),
                "native_sticker_count": len(accepted_stickers),
                "native_sticker": {
                    "x": first_sticker.get("x"),
                    "y": first_sticker.get("y"),
                    "width": first_sticker.get("width"),
                    "height": first_sticker.get("height"),
                    "link_type": story_link.get("link_type"),
                    "display_url": story_link.get("display_url"),
                    "link_title": story_link.get("link_title"),
                } if accepted_stickers else {},
                "configure_status": response.status,
                "visual_sticker": prepared.get("visual_sticker"),
            }
            _trace(dump, "success", **result)
            stage("story_confirmed")
            _capture(
                dump,
                page,
                "post_story_ok",
                str(result)[:900],
            )
            return result

        if not transcode_pending:
            break

    result = {
        "ok": False,
        "step": "configure",
        "status": last_status,
        "upload_id": upload_id,
        "text": last_text[:1800],
        "json": last_json,
        "link_requested": bool(link_url),
    }
    _trace(dump, "failed", **result)
    _capture(
        dump,
        page,
        "post_story_fail_configure",
        str(result)[:1000],
    )
    return result


__all__ = [
    "post_story_with_link",
    "_prepare_story_jpeg",
]
