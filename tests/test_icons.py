import importlib.util
import os
import struct
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

ICON_DIRECTORY = Path(__file__).resolve().parents[1] / "icon"


def load_generator():
    spec = importlib.util.spec_from_file_location("make_icon", ICON_DIRECTORY / "make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.generator = load_generator()

    def test_committed_svgs_match_the_generator(self):
        self.assertEqual(
            (ICON_DIRECTORY / "stellaris-fan-control.svg").read_text(encoding="ascii"),
            self.generator.app_svg(),
        )
        for suffix, (fill, glow) in self.generator.TRAY_VARIANTS.items():
            self.assertEqual(
                (ICON_DIRECTORY / f"stellaris-fan-control-tray{suffix}.svg").read_text(encoding="ascii"),
                self.generator.tray_svg(fill, glow),
            )
        self.assertEqual(self.generator.tray_svg(), self.generator.tray_svg(
            *self.generator.TRAY_VARIANTS[""]))

    def test_tray_and_window_icons_render(self):
        names = [f"stellaris-fan-control-tray{suffix}.svg" for suffix in self.generator.TRAY_VARIANTS]
        for name in (*names, "stellaris-fan-control.png"):
            icon = QIcon(str(ICON_DIRECTORY / name))
            self.assertFalse(icon.isNull(), name)
            image = icon.pixmap(22, 22).toImage()
            self.assertTrue(
                any(image.pixelColor(x, y).alpha() for x in range(22) for y in range(22)), name
            )

    def test_ico_holds_every_size_from_16_to_256(self):
        data = (ICON_DIRECTORY / "stellaris-fan-control.ico").read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data)
        self.assertEqual((reserved, kind), (0, 1))
        sizes = [data[6 + 16 * index] or 256 for index in range(count)]
        self.assertEqual(sizes, list(self.generator.ICO_SIZES))


if __name__ == "__main__":
    unittest.main()
