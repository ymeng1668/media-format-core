"""Green event outlines and bitmap labels for privacy previews."""
import numpy as np

# Printable ASCII, 0x20 through 0x7e; high bit is the leftmost pixel.
_FONT = np.frombuffer(bytes.fromhex(
    "0000000000000000"
    "183c3c1818001800"
    "6c6c000000000000"
    "6c6cfe6cfe6c6c00"
    "307cc0780cf83000"
    "00c6cc183066c600"
    "386c3876dccc7600"
    "3030600000000000"
    "1830606060301800"
    "6030181818306000"
    "00663cff3c660000"
    "003030fc30300000"
    "0000000000303060"
    "000000fc00000000"
    "0000000000303000"
    "060c183060c08000"
    "7cc6ced6e6c67c00"
    "1838181818187e00"
    "7cc60e3c78e2fe00"
    "7cc6061c06c67c00"
    "1c3c6cccfe0c0c00"
    "fec0fc0606c67c00"
    "3860c0fcc6c67c00"
    "fec60c1830303000"
    "7cc6c67cc6c67c00"
    "7cc6c67e060c7800"
    "0030300000303000"
    "0030300000303060"
    "183060c060301800"
    "0000fc0000fc0000"
    "6030180c18306000"
    "78cc0c1830003000"
    "7cc6dededec07800"
    "3078ccccfccccc00"
    "fc66667c6666fc00"
    "3c66c0c0c0663c00"
    "f86c6666666cf800"
    "fe6268786862fe00"
    "fe6268786860f000"
    "3c66c0c0ce663e00"
    "ccccccfccccccc00"
    "7830303030307800"
    "1e0c0c0ccccc7800"
    "e6666c786c66e600"
    "f06060606062fe00"
    "c6eefefed6c6c600"
    "c6e6f6decec6c600"
    "386cc6c6c66c3800"
    "fc66667c6060f000"
    "78ccccccdc781c00"
    "fc66667c786ce600"
    "78cce0381ccc7800"
    "fcb4303030307800"
    "cccccccccccc7c00"
    "cccccccccc783000"
    "c6c6c6d6feeec600"
    "c6c66c38386cc600"
    "cccccc7830307800"
    "fec6860c1832fe00"
    "7860606060607800"
    "c06030180c060200"
    "7818181818187800"
    "10386cc600000000"
    "00000000000000ff"
    "3030180000000000"
    "0000780c7ccc7600"
    "e060607c6666dc00"
    "000078ccc0cc7800"
    "1c0c0c7ccccc7600"
    "000078ccfcc07800"
    "386c60f06060f000"
    "000076cccc7c0c78"
    "e0606c766666e600"
    "3000703030307800"
    "0c000c0c0ccccc78"
    "e060666c786ce600"
    "7030303030307800"
    "0000ccfefed6c600"
    "0000f8cccccccc00"
    "000078cccccc7800"
    "0000dc66667c60f0"
    "000076cccc7c0c1e"
    "0000dc766660f000"
    "00007cc0780cf800"
    "10307c3030341800"
    "0000cccccccc7600"
    "0000cccccc783000"
    "0000c6d6fefe6c00"
    "0000c66c386cc600"
    "0000cccccc7c0cf8"
    "0000fc983064fc00"
    "1c3030e030301c00"
    "1818180018181800"
    "e030301c3030e000"
    "76dc000000000000"
), dtype=np.uint8).reshape(95, 8)


def draw_highlights(frame, events):
    boxes = [item for item in events if item.get("kind") in {"box", "track", "text"}
             and isinstance(item.get("bbox"), dict)]
    if not boxes:
        return
    height, width = frame.shape[:2]
    overlay = np.zeros((height, width, 4), dtype=np.uint8)

    def rectangle(x, y, w, h, color, alpha, fill=False):
        x0, y0 = max(0, min(x, width - 1)), max(0, min(y, height - 1))
        x1, y1 = max(0, min(x + w, width)), max(0, min(y + h, height))
        if x1 <= x0 or y1 <= y0:
            return

        def paint(region):
            values = region.astype(np.uint16)
            values[..., :3] = (np.asarray(color, dtype=np.uint16) * alpha
                              + values[..., :3] * (255 - alpha)) // 255
            values[..., 3] = alpha + values[..., 3] * (255 - alpha) // 255
            region[:] = values

        if fill:
            paint(overlay[y0:y1, x0:x1])
        else:
            # Borders are inside the box; corners receive both strokes.
            paint(overlay[y0:min(y0 + 3, y1), x0:x1])
            paint(overlay[max(y1 - 3, y0):y1, x0:x1])
            paint(overlay[y0:y1, x0:min(x0 + 3, x1)])
            paint(overlay[y0:y1, max(x1 - 3, x0):x1])

    for item in boxes:
        box = item["bbox"]
        x, y, w, h = (int(float(box[key]) + 0.5) for key in ("x", "y", "w", "h"))
        if w <= 0 or h <= 0:
            continue
        score = float(np.float32(item.get("confidence", 0.0)))
        alpha = int(min(255, max(0, min(1, score) * 160 + 40))) if score > 0 else 160
        rectangle(x, y, w, h, (0, 255, 0), alpha)
        label = str(item.get("label", item["kind"]))
        if item["kind"] == "text":
            label = str(item.get("text", {}).get("value", label))
        if score > 0.01:
            label += f" {score * 100:.0f}%"
        if not label:
            continue
        # Unsupported UTF-8 glyphs leave gaps in the byte-oriented ASCII font.
        characters = label.encode("utf-8")
        text_width = len(characters) * 9 - 1
        lx = max(0, min(x, width - text_width - 4))
        ly = y - 13 if y >= 13 else y + 4
        rectangle(lx, ly, text_width + 4, 12, (0, 0, 0), 168, fill=True)
        for index, character in enumerate(characters):
            if not 32 <= character <= 126:
                continue
            for row, bits in enumerate(_FONT[character - 32]):
                for col in range(8):
                    if bits & (128 >> col):
                        px, py = lx + 2 + index * 9 + col, ly + 2 + row
                        if 0 <= px < width and 0 <= py < height:
                            rectangle(px, py, 1, 1, (255, 255, 255), 220, fill=True)

    alpha = overlay[..., 3:4].astype(np.uint32)
    frame[:] = (overlay[..., :3].astype(np.uint32) * alpha
                + frame.astype(np.uint32) * (255 - alpha) + 127) // 255
