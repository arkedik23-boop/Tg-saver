"""Генерит banner.png для приветствия бота. Запускается один раз локально."""
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 820
BG = (14, 22, 36)
CARD = (23, 33, 52)
ACCENT = (235, 64, 52)
WHITE = (236, 240, 245)
GREY = (150, 162, 180)
BLUE = (90, 130, 245)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

F = "/usr/share/fonts/truetype/dejavu/"
def font(name, size):
    return ImageFont.truetype(F + name, size)

h1 = font("DejaVuSans-Bold.ttf", 58)
h2 = font("DejaVuSans-Bold.ttf", 38)
body = font("DejaVuSans.ttf", 32)
small = font("DejaVuSans.ttf", 26)
num = font("DejaVuSans-Bold.ttf", 44)


def rrect(xy, r, fill):
    d.rounded_rectangle(xy, radius=r, fill=fill)


def center(text, y, fnt, fill, cx=W // 2):
    w = d.textlength(text, font=fnt)
    d.text((cx - w / 2, y), text, font=fnt, fill=fill)


# заголовок + иконка (чат-пузырь с корзиной)
bx, by = W // 2 - 55, 55
rrect((bx, by, bx + 110, by + 85), 22, WHITE)
d.polygon([(bx + 20, by + 80), (bx + 20, by + 110), (bx + 50, by + 80)], fill=WHITE)
for i in range(3):
    yy = by + 22 + i * 20
    d.rounded_rectangle((bx + 18, yy, bx + 78, yy + 9), radius=4, fill=CARD)
# корзина
tx, ty = bx + 66, by + 44
rrect((tx, ty, tx + 56, ty + 62), 10, ACCENT)
d.rounded_rectangle((tx - 4, ty - 12, tx + 60, ty + 2), radius=6, fill=ACCENT)
for i in range(3):
    xx = tx + 12 + i * 16
    d.rounded_rectangle((xx, ty + 12, xx + 7, ty + 48), radius=3, fill=WHITE)

center("Восстановитель сообщений", 190, h1, WHITE)
center("Показываю удалённые и изменённые сообщения,", 268, body, GREY)
center("исчезающие фото и видео", 310, body, GREY)

# три шага
steps = [
    ("1", "Профиль", "«Изменить профиль» →\n«Автоматизация чатов»"),
    ("2", "Имя бота", "Вставь @Antidelite_bot\nв поиск"),
    ("3", "Добавить", "Нажми «Добавить» —\nи готово"),
]
cw, gap = 350, 30
total = cw * 3 + gap * 2
x0 = (W - total) // 2
cy = 400
ch = 360
for i, (n, title, desc) in enumerate(steps):
    x = x0 + i * (cw + gap)
    rrect((x, cy, x + cw, cy + ch), 28, CARD)
    # кружок с номером
    ccx = x + cw // 2
    r = 44
    d.ellipse((ccx - r, cy + 34, ccx + r, cy + 34 + 2 * r), fill=BLUE)
    nw = d.textlength(n, font=num)
    d.text((ccx - nw / 2, cy + 48), n, font=num, fill=WHITE)
    center(title, cy + 150, h2, WHITE, cx=ccx)
    ly = cy + 212
    for line in desc.split("\n"):
        center(line, ly, small, GREY, cx=ccx)
        ly += 36

img.save("banner.png")
print("banner.png saved", img.size)
