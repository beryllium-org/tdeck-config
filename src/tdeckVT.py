import board as _board
import displayio as _displayio
import digitalio as _digitalio
import countio as _countio
import terminalio as _terminalio
from time import monotonic as _monotonic
from pulseio import PulseIn as _pulsein
import gc as _gc
from cptoml import fetch as _fetch
from os import uname as _uname

_palette = _displayio.Palette(2)
_palette[1] = 0xFFFFFF


class tdeckVT:
    def __init__(self) -> None:
        self._terminal = None
        self._r = None
        self._tg = None
        self._b = False
        self._lines = None
        self._chars = None
        self._conn = False
        self._fpolls = 0
        self._bat = None
        self._in_buf = bytearray(0)
        self._ch = bytearray(1)
        self._r = _displayio.Group()
        self._bckbit = _displayio.Bitmap(
            _board.DISPLAY.width, _board.DISPLAY.height, 256
        )
        self._pal256 = _displayio.Palette(256)
        self._background = None
        font_width, font_height = _terminalio.FONT.get_bounding_box()
        self._lines = int(_board.DISPLAY.height / font_height) - 1
        self._chars = int(_board.DISPLAY.width / font_width) - 1
        tg = _displayio.TileGrid(
            _terminalio.FONT.bitmap,
            pixel_shader=_palette,
            width=self._chars,
            height=self._lines,
            tile_width=font_width,
            tile_height=font_height,
            x=(_board.DISPLAY.width - (self._chars * font_width)) // 2,
            y=(_board.DISPLAY.height - (self._lines * font_height)) // 2,
        )
        self._terminal = _terminalio.Terminal(tg, _terminalio.FONT)
        self._r.append(tg)
        _board.DISPLAY.root_group = self._r
        _board.DISPLAY.brightness = 0.0
        self._kb_bus = _board.I2C()
        self._boot = _countio.Counter(_board.BOOT)
        self._bstv = 0
        self._bst = False
        self._w = _pulsein(_board.TRACKBALL_UP, maxlen=10)
        self._a = _pulsein(_board.TRACKBALL_LEFT, maxlen=10)
        self._s = _pulsein(_board.TRACKBALL_DOWN, maxlen=10)
        self._d = _pulsein(_board.TRACKBALL_RIGHT, maxlen=10)
        self._bdebounce = _monotonic()
        self._lst = _monotonic()

    @property
    def enabled(self):
        return self._conn

    @property
    def display(self):
        return _board.DISPLAY

    @display.setter
    def display(self, displayobj) -> None:
        raise OSError("This console does not support alternative displays")

    @property
    def terminal(self):
        return self._terminal

    @property
    def size(self) -> list:
        return [self._chars, self._lines]

    @property
    def alt_mode(self) -> bool:
        if self._boot.count != self._bstv:
            self._bstv = self._boot.count
            ct = _monotonic()
            if ct - self._bdebounce > 0.25:
                self._bst = not self._bst
                self._bdebounce = ct
                self._fpolls = 0
        return self._bst

    @alt_mode.setter
    def alt_mode(self, value: bool):
        self._bst = bool(value)

    @property
    def in_waiting(self) -> int:
        self._rr()
        return len(self._in_buf)

    def _rr(self) -> None:
        if _monotonic() - self._lst < 0.15:
            return
        self._kb_bus.try_lock()
        try:
            self._kb_bus.readfrom_into(0x55, self._ch)
        except OSError:
            self._ch[0] = 0
        self._kb_bus.unlock()
        kv = self._ch[0]
        if kv:
            if self.alt_mode:
                if kv > 96 and kv < 122:
                    kv -= 96
            if kv == 13:
                kv = 10
            elif kv == 8:
                kv = 127
            self._ch[0] = kv
            self._in_buf += self._ch
        else:
            if len(self._w) or len(self._a) or len(self._s) or len(self._d):
                if len(self._w) > 4:
                    self._in_buf += b"\x1b[H" if self.alt_mode else b"\x1b[A"
                elif len(self._a) > 4:
                    self._in_buf += b"\x1b[D"
                elif len(self._s) > 4:
                    self._in_buf += b"\x1b[F" if self.alt_mode else b"\x1b[B"
                elif len(self._d) > 4:
                    self._in_buf += b"\t" if self.alt_mode else b"\x1b[C"
                self._w.clear()
                self._a.clear()
                self._s.clear()
                self._d.clear()
        self._lst = _monotonic()

    @property
    def battery(self) -> int:
        return self._bat.percentage if self._bat is not None else -1

    @battery.setter
    def battery(self, batobj) -> None:
        if batobj is None:
            self._bat = None
            return
        try:
            batobj.voltage
            self._bat = batobj
        except:
            print("Invalid battery object")

    @property
    def connected(self) -> bool:
        return self._conn

    def disconnect(self) -> None:
        self.disable()

    def reset_input_buffer(self) -> None:
        self._in_buf = bytearray()

    def read(self, count=None):
        if count is None:
            raise OSError("This console does not support unbound reads")
        while self.in_waiting < count:
            pass
        res = self._in_buf[:count]
        self._in_buf = self._in_buf[count:]
        return res

    def enable(self) -> None:
        _board.DISPLAY.root_group = self._r
        self._conn = True
        _board.DISPLAY.brightness = 1.0

    def disable(self) -> None:
        self._conn = False
        _board.DISPLAY.brightness = 0.0

    def write(self, data: bytes) -> int:
        if not self._conn:
            return 0
        res = self._terminal.write(data)
        return res

    def mode(self, graphics: bool = False) -> None:
        pass

    def deinit(self) -> None:
        _board.DISPLAY.brightness = 0.0
        del self._in_buf
        del self
