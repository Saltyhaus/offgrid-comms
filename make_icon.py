"""One-off script to generate assets/icon.png -- a dark-theme icon with a
teal/orange radio-wave glyph, matching the look of the amp monitor and
Shack Services apps. Run once: python3 make_icon.py
"""

from PIL import Image, ImageDraw

SIZE = 256
BG = (18, 22, 26, 255)        # near-black, matches the dark theme
TEAL = (0, 217, 217, 255)     # FT-991A-style teal readout
ORANGE = (255, 149, 0, 255)   # FT-991A-style orange readout

img = Image.new("RGBA", (SIZE, SIZE), BG)
draw = ImageDraw.Draw(img)

cx, cy = SIZE // 2, SIZE // 2

# concentric "signal" arcs, alternating teal/orange, open to the upper right
# like a stylized broadcast/antenna glyph
for i, radius in enumerate(range(40, 120, 20)):
    color = TEAL if i % 2 == 0 else ORANGE
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    draw.arc(bbox, start=200, end=340, fill=color, width=10)

# center dot (the "station")
dot_r = 16
draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=ORANGE)

# rounded dark square background so it reads cleanly as an app icon
bg_img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
bg_draw = ImageDraw.Draw(bg_img)
bg_draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=48, fill=BG)
bg_img.alpha_composite(img)

bg_img.save("assets/icon.png")
print("wrote assets/icon.png")
