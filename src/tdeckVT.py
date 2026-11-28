import board as _board
import countio as _countio
import displayio as _displayio
import gc as _gc
import keypad as _keypad
import terminalio as _terminalio
from time import monotonic as _monotonic

_palette = _displayio.Palette(2)
_palette[1] = 0xFFFFFF

_KB = 0x55
_POLL = 0.02  # input pump period (s); the C3 latches one char
_COMPOSE_T = 1.2  # "$" compose timeout (s)
_CHEAT_T = 4.0  # "$P" substitution cheat sheet display (s)
_FLICK_ON = 200.0  # per-frame edges/s rate that counts as a strong flick
_FLICK_OFF = 60.0  # instantaneous rate a gesture must drop to before
# the next flick can trigger
_HCAP = 4  # max plain arrows per poll (light/medium flicks)
_XSENS = 0.6  # horizontal arrow sensitivity (1.0 = one per edge)
_BRT_SHOW = 2.0  # seconds the brightness readout stays in the help line
_TBASE = 1.0  # output units per trackball edge, slow
_TFAST = 14.0  # output units per trackball edge, fast
_VFULL = 220.0  # edges/s for "full speed"
_VTAU = 0.12  # velocity EMA time constant
_HINT_T = 5.0  # help line rotation period

_CL = b"\x1b[2J\x1b[H"

# "$" compose table: chord char -> emitted char
_COMPOSE = {
    ",": "<",
    ".": ">",
    "(": "{",
    ")": "}",
    "/": "\\",
    "?": "|",
    "5": "%",
    "6": "^",
    "7": "&",
    "8": "*",
    "-": "=",
    "'": "`",
    '"': "~",
}

# "$P" cheat sheet: one line of key->char pairs on the status row,
# one line of the chord specials on the help row
_CHEAT_KEYS = " ".join(k + v for k, v in _COMPOSE.items())
_CHEAT_L2 = "$$=$  $a-z=^A-Z  $+ball=Tab  $P=show this"

_HINTS = (
    "roll: arrows  flick: Home/End/1 line",
    "click the ball for ALT (Ctrl) mode",
    "hold sym + key for digits/symbols",
    "hold shift for CAPITAL letters",
    "$+key: chars  $+P: all substitutions",
    "$$ = $   $c = Ctrl-C (abort)",
)


class _Axis:
    """One trackball axis: two one-direction hall outputs, dominant wins."""

    __slots__ = ("neg", "pos", "vel", "acc", "net", "inst", "sgn")

    def __init__(self, neg_pin, pos_pin):
        self.neg = _countio.Counter(neg_pin, edge=_countio.Edge.RISE_AND_FALL)
        self.pos = _countio.Counter(pos_pin, edge=_countio.Edge.RISE_AND_FALL)
        self.vel = 0.0
        self.acc = 0.0
        self.net = 0
        self.inst = 0.0
        self.sgn = 0

    def poll(self, dt):
        n = self.neg.count
        p = self.pos.count
        self.neg.reset()
        self.pos.reset()
        net = 0
        if p + n >= 2:  # a real crossing is >= 2 edges; a lone one is noise
            if p > n:
                net = p
            elif n > p:
                net = -n
        self.net = net
        inst = (p + n) / dt if dt else 0.0
        self.inst = inst
        self.sgn = 1 if p > n else (-1 if n > p else 0)
        self.vel += (inst - self.vel) * (1.0 - 2.718281828 ** (-dt / _VTAU))
        self.acc += net * (_TBASE + (_TFAST - _TBASE) * min(self.vel / _VFULL, 1.0))
        steps = int(self.acc)
        if steps:
            self.acc -= steps
        return steps


class tdeckVT:
    def __init__(self) -> None:
        self._conn = False
        self._bat = None
        self._bat_vstate = -1
        self._in_buf = bytearray()
        self._ch = bytearray(1)
        self._lst = _monotonic()
        self._alt = False
        self._pend = False
        self._pend_t = 0.0
        self._hint = 0
        self._hint_t = 0.0
        self._cheat_t = 0.0  # "$P" cheat sheet visible until this time
        self._cheat_on = False
        self._l1 = None
        self._l2 = None
        self._fpolls = 0  # locked-screen lit/dim poll counter

        self._r = _displayio.Group()
        fw, fh = _terminalio.FONT.get_bounding_box()
        w = _board.DISPLAY.width
        h = _board.DISPLAY.height
        self._chars = int(w / fw) - 1
        rows = int(h / fh) - 1
        self._rows = rows - 3  # bottom 3 rows belong to the driver
        x = (w - (self._chars * fw)) // 2
        y = (h - (rows * fh)) // 2

        tg = _displayio.TileGrid(
            _terminalio.FONT.bitmap,
            pixel_shader=_palette,
            width=self._chars,
            height=self._rows,
            tile_width=fw,
            tile_height=fh,
            x=x,
            y=y,
        )
        self._terminal = _terminalio.Terminal(tg, _terminalio.FONT)
        self._r.append(tg)

        stg = _displayio.TileGrid(
            _terminalio.FONT.bitmap,
            pixel_shader=_palette,
            width=self._chars,
            height=3,
            tile_width=fw,
            tile_height=fh,
            x=x,
            y=y + self._rows * fh,
        )
        self._bar = _terminalio.Terminal(stg, _terminalio.FONT)
        self._r.append(stg)
        _board.DISPLAY.root_group = self._r
        try:
            # not settable on every build; if it is, make sure firmware
            # auto-brightness cannot fight the driver's dimming
            _board.DISPLAY.auto_brightness = False
        except AttributeError:
            pass
        self._bar.write(b"\x1b[1;1H" + b"_" * self._chars)

        self._kb_bus = _board.I2C()
        # NOTE: ESP32-S3 has only 4 PCNT units -- the four trackball
        # counters consume all of them, so the click must NOT use
        # countio (keypad uses plain GPIO interrupts instead).
        self._click = _keypad.Keys(
            (_board.TRACKBALL_CLICK,), value_when_pressed=False, max_events=8
        )
        self._bst = 0.0
        self._ax_x = _Axis(_board.TRACKBALL_LEFT, _board.TRACKBALL_RIGHT)
        self._ax_y = _Axis(_board.TRACKBALL_UP, _board.TRACKBALL_DOWN)
        self._xgest = False  # inside a strong horizontal flick
        self._ygest = False  # inside a strong vertical flick
        self._xwas = False  # horizontal was moving in the previous poll
        self._xbudget = 0.0  # fractional arrow budget (sensitivity < 1)
        self._brt_until = 0.0  # timestamp until the brightness readout shows
        self._brt_showing = False
        self._brt = 80  # THE stored brightness, int 0-100
        self._brt_dir = 0  # last wheel step direction (bounce filter)
        self._brt_t = 0.0  # last wheel step time
        self._gfx = False  # graphics mode (app owns the screen)
        self._gui_group = _displayio.Group()  # graphics-mode root group
        self._mdx = 0.0  # mouse dx since last read_mouse()
        self._mdy = 0.0  # mouse dy since last read_mouse()
        self._mclicks = 0  # mouse presses since last read_mouse()
        self._dead = False
        self._draw_bar(True)
        # Draw the lock screen right away: if another console is active,
        # `connected` is never polled, and the driver must still own the
        # display from boot on.
        self._draw_lock()
        self._apply_brt()

    # ------------------------------------------------------------- helpers

    def _hint_line(self) -> bytes:
        if not self._conn:
            return b"press ENTER to unlock the console"
        if self._pend:
            return b"$: key  $$=$  a-z=Ctrl  ball->=Tab"
        if _monotonic() < self._cheat_t:
            return bytes(_CHEAT_L2, "utf-8")
        if _monotonic() < self._brt_until:
            return bytes("Brightness: {}%".format(self._brt), "utf-8")
        if self._alt:
            return b"ALT: a-z=Ctrl  up/down=brightness"
        return bytes(_HINTS[self._hint], "utf-8")

    def _lock_screen(self) -> bytes:
        bat = self.battery
        if bat >= 0:
            bats = "{}%".format(bat)
        else:
            bats = "---"
        if len(bats) < 3:
            bats = (" " * (3 - len(bats))) + bats
        mem = "{}k".format(_gc.mem_free() // 1024)
        rows = (
            b"Console locked, press ENTER to unlock",
            b"-" * self._chars,
            b" ,-----------,    System",
            b" | BERYLLIUM |    -------",
            b" |           |    Battery: " + bytes(bats, "utf-8"),
            b" |           |    Free mem: " + bytes(mem, "utf-8"),
            b" |           |    Console: tty1",
            b" '-----------'",
            b"",
            b" ball: arrows   click: ALT mode",
            b" $+key: chars   $+a..z: ^A-^Z",
        )
        out = b""
        for row in rows:
            out += row + b"\n\r"
        return out

    def _draw_lock(self) -> None:
        self._terminal.write(_CL + self._lock_screen())

    def _apply_brt(self) -> None:
        if self._brt > 100:
            self._brt = 100
        elif self._brt < 0:
            self._brt = 0
        # CP bug: brightness 1.0 renders DIMMER than 0.99
        _board.DISPLAY.brightness = min(self._brt / 100.0, 0.99)

    def _draw_bar(self, force=False) -> None:
        if self._gfx:
            return  # graphics mode: no overlays
        l1 = b""
        if _monotonic() < self._cheat_t:
            # "$P" cheat sheet: substitution pairs on the status row
            l1 = bytes(_CHEAT_KEYS, "utf-8")
        else:
            if self._alt:
                l1 += b"ALT "
            if self._pend:
                l1 += b"$ "
            bat = self.battery
            if bat >= 0:
                l1 += bytes("B:{}%".format(bat), "utf-8")
        pad = self._chars - len(l1)
        if pad > 0:
            l1 += b" " * pad
        l2 = self._hint_line()
        # The help line sits on the bar's LAST row: writing its final
        # column pushes the Terminal cursor past the end and triggers
        # an auto-scroll that eats the separator. Keep the last
        # column untouched on that row.
        pad = self._chars - 1 - len(l2)
        if pad > 0:
            l2 += b" " * pad
        elif pad < 0:
            l2 = l2[: self._chars - 1]
        if force or l1 != self._l1:
            self._bar.write(b"\x1b[2;1H" + l1)
            self._l1 = l1
        if force or l2 != self._l2:
            self._bar.write(b"\x1b[3;1H" + l2)
            self._l2 = l2

    def _key(self, kv, now) -> None:
        if kv == 13:
            kv = 10
        elif kv == 8:
            kv = 127
        if self._alt and 96 < kv < 123:
            kv -= 96
        ch = chr(kv) if 32 <= kv < 127 else None
        if self._pend:
            self._pend = False
            if ch == "P" or ch == "p":
                # "$P": show the substitution cheat sheet for a while
                self._cheat_t = _monotonic() + _CHEAT_T
                self._draw_bar(True)
            elif ch and ch in _COMPOSE:
                self._in_buf += bytes(_COMPOSE[ch], "utf-8")
            elif ch == "$":
                self._in_buf += b"$"
            elif 97 <= kv <= 122:
                self._in_buf += bytes((kv - 96,))
            elif kv == 10:
                pass  # $ + enter cancels the compose
            else:
                self._in_buf += b"$" + bytes((kv,))
            self._draw_bar(True)
        elif ch == "$":
            self._pend = True
            self._pend_t = now
            self._draw_bar(True)
        else:
            self._in_buf += bytes((kv,))
        if len(self._in_buf) > 128:
            self._in_buf = self._in_buf[len(self._in_buf) - 128 :]

    def _rr(self) -> None:
        if self._dead:
            return
        now = _monotonic()
        dt = now - self._lst
        if dt < _POLL:
            return
        self._lst = now

        # ---- trackball -> VT navigation (the engine itself is untouched;
        # these semantics apply only to terminal arrow emission)
        dx = self._ax_x.poll(dt)
        dy = self._ax_y.poll(dt)
        nx = self._ax_x.net
        ny = self._ax_y.net
        if self._gfx:
            # graphics mode: the trackball is a mouse data stream with the
            # exact trackball.py semantics -- velocity-scaled steps, the
            # axis acc keeps the sub-step fraction between frames
            self._mdx += dx
            self._mdy += dy
        elif dx or dy or nx or ny:
            if not self._conn:
                # locked: ball movement relights the screen
                self._fpolls = 0
                self._apply_brt()
        if self._conn and not self._gfx:
            if self._pend and (dx or dy or nx or ny):
                # "$" + ball right = TAB; other ball input cancels the compose
                if nx > 0 or dx > 0:
                    self._in_buf += b"\t"
                self._pend = False
                self._draw_bar(True)
            elif not self._pend:
                # Flick detection uses the instant per-frame edge
                # rate (ax.inst), not the smoothed EMA velocity: a real
                # flick spikes way past the threshold for a few frames,
                # while the EMA never gets there in time.
                ix = self._ax_x.inst
                iy = self._ax_y.inst
                # horizontal: light flicks are 1 arrow, medium a few,
                # a strong flick is Home / End, once per gesture
                moved_x = dx or nx or self._ax_x.sgn
                if not self._xgest and ix > _FLICK_ON and nx:
                    self._xgest = True
                    self._in_buf += b"\x1b[F" if nx > 0 else b"\x1b[H"
                    self._xwas = False
                    self._xbudget = 0.0
                elif self._xgest:
                    if ix < _FLICK_OFF:
                        self._xgest = False
                    self._xwas = True  # suppression counts as ongoing motion
                elif moved_x:
                    # a lone edge frame (sgn) still counts as movement:
                    # light flicks must never be swallowed by the gate
                    n = max(dx, 1)
                    if n > _HCAP:
                        n = _HCAP
                    self._xbudget += n * _XSENS
                    if not self._xwas:
                        k = 1  # a flick onset always answers with 1 arrow
                        self._xbudget = 0.0
                    else:
                        k = int(self._xbudget)
                        self._xbudget -= k
                    self._xwas = True
                    s = nx or dx or self._ax_x.sgn
                    if s > 0:
                        self._in_buf += (b"\t" if self._alt else b"\x1b[C") * k
                    else:
                        self._in_buf += b"\x1b[D" * k
                else:
                    self._xwas = False
                    self._xbudget = 0.0
                # vertical: in ALT mode it is the brightness wheel (+/-1
                # percent per moving frame); otherwise strong flicks
                # only, exactly one event per gesture
                moved_y = dy or ny or self._ax_y.sgn
                if self._alt:
                    if moved_y:
                        s = ny or dy or self._ax_y.sgn
                        if s:
                            if now - self._brt_t > 0.3 or s == self._brt_dir:
                                # an instant reversal is magnet bounce at the
                                # end of a flick: ignore it
                                self._brt += 1 if s < 0 else -1
                                self._apply_brt()
                                self._brt_dir = s
                                self._brt_t = now
                                self._brt_until = now + _BRT_SHOW
                elif not self._ygest and iy > _FLICK_ON and ny:
                    self._ygest = True
                    self._in_buf += b"\x1b[B" if ny > 0 else b"\x1b[A"
                elif self._ygest and iy < _FLICK_OFF:
                    self._ygest = False
        if len(self._in_buf) > 128:
            self._in_buf = self._in_buf[len(self._in_buf) - 128 :]

        # ---- click = ALT toggle (keypad debounces internally)
        click = self._click.events.get()
        while click:
            if click.pressed:
                if self._gfx:
                    self._mclicks += 1
                elif self._conn:
                    if now - self._bst > 0.25:
                        self._bst = now
                        self._alt = not self._alt
                        self._draw_bar(True)
                else:
                    # locked: click relights the screen
                    self._fpolls = 0
                    self._apply_brt()
            click = self._click.events.get()

        # ---- "$" compose timeout
        if self._pend and now - self._pend_t > _COMPOSE_T:
            self._pend = False
            self._in_buf += b"$"
            self._draw_bar(True)

        # ---- "$P" cheat sheet expiry
        cheat = now < self._cheat_t
        if cheat != self._cheat_on:
            self._cheat_on = cheat
            self._draw_bar(cheat)

        # ---- help line: live brightness readout, otherwise rotation
        if now < self._brt_until:
            self._brt_showing = True
            self._draw_bar()
        elif self._brt_showing:
            self._brt_showing = False
            self._draw_bar()
        if now - self._hint_t > _HINT_T:
            self._hint_t = now
            self._hint = (self._hint + 1) % len(_HINTS)
            self._draw_bar()

        # ---- keyboard
        if self._kb_bus.try_lock():
            try:
                self._kb_bus.readfrom_into(_KB, self._ch)
            except OSError:
                self._ch[0] = 0
            finally:
                self._kb_bus.unlock()
        kv = self._ch[0]
        if kv:
            if self._conn:
                self._key(kv, now)
            else:
                # any key relights the locked screen
                self._fpolls = 0
                self._apply_brt()
                if kv == 13 or kv == 10:
                    self.enable()
            # every other key stays swallowed while locked

    # ---------------------------------------------------------- properties

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
        return [self._chars, self._rows]

    @property
    def alt_mode(self) -> bool:
        return self._alt

    @alt_mode.setter
    def alt_mode(self, value: bool):
        self._alt = bool(value)
        self._draw_bar(True)

    @property
    def in_waiting(self) -> int:
        self._rr()
        return len(self._in_buf)

    @property
    def battery(self) -> int:
        if self._bat is None:
            return -1
        try:
            return int(self._bat.percentage)
        except (OSError, RuntimeError, TypeError):
            return -1

    @battery.setter
    def battery(self, batobj) -> None:
        if batobj is None:
            self._bat = None
            return
        try:
            batobj.voltage
            self._bat = batobj
        except:  # noqa: E722
            print("Invalid battery object")

    @property
    def connected(self) -> bool:
        self._rr()
        if self._dead:
            return False
        if self._gfx:
            # graphics mode: the app owns the screen -- no overlays, no
            # dimming, no lock screen; just report the connection state
            return self._conn
        if self._conn:
            self._bat_vstate = -1
            return True
        # Locked: cardputerVT-style fade -- a short lit window for the
        # battery readout, then a stepwise dim to dark. Any input
        # relights (see _rr); only ENTER unlocks.
        if self._fpolls < 15:
            self._fpolls += 1
            bat = self.battery
            if bat != self._bat_vstate:
                self._bat_vstate = bat
                self._draw_lock()
        elif _board.DISPLAY.brightness:
            try:
                _board.DISPLAY.brightness -= 0.1
            except:
                _board.DISPLAY.brightness = 0
        return False

    # -------------------------------------------------------------- methods

    def disconnect(self) -> None:
        self.disable()

    def reset_input_buffer(self) -> None:
        self._in_buf = bytearray()

    def read(self, count=None):
        self._rr()
        if count is None:
            count = len(self._in_buf)
        else:
            count = min(count, len(self._in_buf))
        if not count:
            return b""
        res = bytes(self._in_buf[:count])
        self._in_buf = self._in_buf[count:]
        return res

    def enable(self) -> None:
        if self._dead or self._gfx:
            return
        self._terminal.write(_CL)
        _board.DISPLAY.root_group = self._r
        self._conn = True
        self._apply_brt()
        self._draw_bar(True)

    def disable(self) -> None:
        if self._dead:
            return
        if self._gfx:
            # never stay in graphics mode without an app driving it
            self.mode(False)
        self._conn = False
        self._fpolls = 0
        self._in_buf = bytearray()
        self._draw_lock()
        self._draw_bar(True)
        self._apply_brt()

    def write(self, data: bytes) -> int:
        if not self._conn:
            return 0
        self._rr()
        return self._terminal.write(data)

    def mode(self, graphics: bool = False) -> None:
        """Switch between text (False) and graphics (True) mode.

        Graphics mode hands the screen to `gui_group` (empty unless an
        app allocated its own group via the gui_group property), pins
        brightness to 0.99, turns the trackball into a mouse data
        stream with the trackball.py semantics (read_mouse()) and
        disables the overlays, dimming and ALT mode. Keyboard input,
        including "$" compose, is unchanged.
        """
        if self._dead:
            return
        if graphics and not self._gfx:
            self._gfx = True
            _board.DISPLAY.brightness = 0.99
            _board.DISPLAY.root_group = self._gui_group
        elif not graphics and self._gfx:
            self._gfx = False
            _board.DISPLAY.root_group = self._r
            self._draw_bar(True)
            self._apply_brt()

    def read_mouse(self):
        """Graphics mode only: mouse data as (dx, dy, clicks).

        dx/dy are the velocity-scaled steps accumulated since the last
        call -- exactly the trackball.py poll() output (precise at slow
        roll, up to 14 units per edge at full flick, EMA-smoothed; the
        axis acc keeps sub-step fractions). clicks is the number of
        presses. The counters reset on read.
        """
        self._rr()
        res = (self._mdx, self._mdy, self._mclicks)
        self._mdx = 0
        self._mdy = 0
        self._mclicks = 0
        return res

    @property
    def gui_group(self):
        return self._gui_group

    @gui_group.setter
    def gui_group(self, group) -> None:
        if group is None:
            group = _displayio.Group()
        self._gui_group = group
        if self._gfx:
            _board.DISPLAY.root_group = group

    def deinit(self) -> None:
        if self._gfx:
            self._gfx = False
            _board.DISPLAY.root_group = self._r
        _board.DISPLAY.brightness = 0.99
        msg = b"Console deinitialized"
        pad = b" " * ((self._chars - len(msg)) // 2)
        self._terminal.write(_CL + pad + msg + b"\n\r" + b"-" * self._chars)
        self._conn = False
        self._dead = True
