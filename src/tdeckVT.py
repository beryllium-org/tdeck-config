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

cl_str = b"\x1b[2J\x1b[3J\x1b[H"
lm_str = (
    cl_str
    + b" " * 7
    + b"Console locked, press ENTER to unlock\n\r"
    + b"-" * 52
    + b"\n\r  ,-----------,     System active\n\r"
    + b"  | 4    9.01 |     -------------\n\r"
    + b"  |           |     Beryllium OS\n\r"
    + b"  |           |     "
    + bytes(_fetch("git_tag", "BERYLLIUM"), "UTF-8")
    + b"\n\r  | BERYLLIUM |     CircuitPython\n\r"
    + b"  '-----------'     "
    + bytes(_uname()[3][: _uname()[3].find(" on ")], "UTF-8")
    + (b"\n\r" * 2)
    + b"-" * 52
    + b"\n\rTo toggle Ctrl, press the trackball.\n\r"
    + b"The trackball is the system's arrow keys.\n\r"
    + b"When Ctrl is pressed, RIGHT is TAB and UP/DOWN\n\r"
    + b"is HOME/END, accordingly.\n\r\n\r"
    + b"-" * 52
    + b"Ctrl: Disabled | Battery: ???% | RAM: ????/????KB"
)


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
        _board.DISPLAY.brightness = 1.0
        self._terminal.write(lm_str)
        self._kb_bus = _board.I2C()
        self._boot = _countio.Counter(_board.BOOT)
        self._bstv = 0
        self._bst = False
        self._w = _pulsein(_board.TRACKBALL_UP, maxlen=10)
        self._a = _pulsein(_board.TRACKBALL_LEFT, maxlen=10)
        self._s = _pulsein(_board.TRACKBALL_DOWN, maxlen=10)
        self._d = _pulsein(_board.TRACKBALL_RIGHT, maxlen=10)
        self._bdebounce = _monotonic()

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
                _board.DISPLAY.brightness = 1.0
        return self._bst

    @property
    def in_waiting(self) -> int:
        self._rr()
        return len(self._in_buf)

    def _rr(self) -> None:
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
                    self._in_buf += b'\x1b[A'
                elif len(self._a) > 4:
                    self._in_buf += b'\x1b[D'
                elif len(self._s) > 4:
                    self._in_buf += b'\x1b[B'
                elif len(self._d) > 4:
                    self._in_buf += b'\t' if self.alt_mode else b'\x1b[C'
                self._w.clear()
                self._a.clear()
                self._s.clear()
                self._d.clear()


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
        if self._conn:
            return True
        if self.in_waiting and b"\n" in self._in_buf:
            _board.DISPLAY.brightness = 1.0
            self._fpolls = 0
            self.enable()
        if self._in_buf:
            self.reset_input_buffer()
            _board.DISPLAY.brightness = 1.0
            self._fpolls = 0
        if not self._conn:
            if self._fpolls < 15:
                curr = self.battery
                if curr != -1:
                    if curr < 10:
                        curr = 2 * " " + str(curr)
                    elif curr < 100:
                        curr = " " + str(curr)
                    else:
                        curr = str(curr)
                    curr = bytes(curr, "UTF-8")
                    _gc.collect()
                    _gc.collect()
                    mt = _gc.mem_alloc() + _gc.mem_free()
                    mu = mt-_gc.mem_free()
                    mused = bytes(str(mu//1024), "UTF-8")
                    mtot = bytes(str((mt)//1024), "UTF-8")
                    mdstr = lm_str.replace(b"RAM: ????/????KB", mused + b"/" + mtot + "KB")
                    mdstr = mdstr.replace(b"???", curr)
                    if self.alt_mode:
                        mdstr = mdstr.replace(b"Disabled", b"Enabled ")
                    self._terminal.write(mdstr)
                self._fpolls += 1
            elif _board.DISPLAY.brightness:
                try:
                    _board.DISPLAY.brightness -= 0.2
                except:
                    _board.DISPLAY.brightness = 0
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
        self._terminal.write(cl_str)
        _board.DISPLAY.root_group = self._r
        self._conn = True
        _board.DISPLAY.brightness = 1.0

    def disable(self) -> None:
        self._conn = False
        if not self._bat:
            self._terminal.write(lm_str.replace(b"     Battery: ???%", b""))
        else:
            self.connected

    def write(self, data=bytes) -> int:
        if not self._conn:
            return 0
        res = self._terminal.write(data)
        return res

    def deinit(self) -> None:
        _board.DISPLAY.brightness = 1.0
        self._terminal.write(
            cl_str + b" " * ((self._chars // 2) - 10) + b"Console deinitialized\n\r" + b"-" * self._chars
        )
        del self._in_buf
        del self
