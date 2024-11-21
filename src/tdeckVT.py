import board as _board
import displayio as _displayio
import digitalio as _digitalio
import terminalio as _terminalio

_palette = _displayio.Palette(2)
_palette[1] = 0xFFFFFF

cl_str = b"\x1b[2J\x1b[3J\x1b[H"
lm_str = (
    cl_str
    + b" Console locked, press ENTER to unlock\n\r"
    + b"-" * 39
    + b"\n\r"
    + b"  ,-----------,     System active\n\r"
    + b"  | 4    9.01 |     -------------\n\r"
    + b"  |           |\n\r"
    + b"  |           |     Battery: ???%\n\r"
    + b"  | BERYLLIUM |\n\r"
    + b"  '-----------'\n\r"
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
        self._bat_vstate = -1
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
        self._terminal.write(lm_str.replace(b"     Battery: ???%", b""))
        self._kb_bus = _board.I2C()
        self._boot = _digitalio.DigitalInOut(_board.BOOT)
        self._boot.switch_to_input()
        self._boot_debounce = False
        self._alt_mode = False

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
    def in_waiting(self) -> int:
        self._rr()
        return len(self._in_buf)

    def _rr(self) -> None:
        kv = not self._boot.value
        if kv and not self._boot_debounce:
            self._alt_mode = not self._alt_mode
        self._boot_debounce = kv
        self._kb_bus.try_lock()
        try:
            self._kb_bus.readfrom_into(0x55, self._ch)
        except OSError:
            self._ch[0] = 0
        self._kb_bus.unlock()
        if self._ch[0]:
            kv = self._ch[0]
            if self._alt_mode:
                if kv > 96 and kv < 122:
                    kv -= 96
            if kv == 13:
                kv = 10
            elif kv == 8:
                kv = 127
            self._ch[0] = kv
            self._in_buf += self._ch

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
                if curr != -1 and curr != self._bat_vstate:
                    self._bat_vstate = curr
                    if curr < 10:
                        curr = 2 * " " + str(curr)
                    elif curr < 100:
                        curr = " " + str(curr)
                    else:
                        curr = str(curr)
                    curr = bytes(curr, "UTF-8")
                    self._terminal.write(lm_str.replace(b"???", curr))
                self._fpolls += 1
            elif _board.DISPLAY.brightness:
                try:
                    _board.DISPLAY.brightness -= 0.1
                except:
                    _board.DISPLAY.brightness = 0
        else:
            self._bat_vstate = -1
        return self._conn

    def disconnect(self) -> None:
        self.disable()

    def reset_input_buffer(self) -> None:
        self._in_buf = bytearray()

    def read(self, count=None):
        if count is None:
            raise OSError("This console does not support unbound reads")
        while self.in_waiting < count:
            self._rr()
        res = self._in_buf[:count]
        self._in_buf = self._in_buf[count:]
        del count
        return res.replace(b"\r", b"\n")

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
            cl_str + b" " * 9 + b"Console deinitialized\n\r" + b"-" * 39
        )
        del self._in_buf
        del self
