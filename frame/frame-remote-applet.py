#!/usr/bin/env python3
"""Frame Remote: a SteamVR dashboard panel that runs commands on your PC.

Runs on the Steam Frame (systemd user service frame-remote-applet). It adds an
icon to the SteamVR dashboard's bottom bar; the panel shows the buttons and
status lights the PC's frame-remote-server sends (configured on the PC), and a
small pop-up in front of your eyes when a command starts and finishes.

Config: ~/.config/frame-remote/frame.toml  (pc = "http://<pc>:38112", token = "...")
That file can also hold local buttons ([[button]]), which run on the headset
itself: a command, or `wake = "<MAC>"` (Wake-on-LAN, works while the PC is off).

How it draws: Pillow paints the panel into ONE persistent OpenGL texture per
overlay (headless EGL, no window), updated in place and handed to
SetOverlayTexture. SetOverlayFromFile / SetOverlayRaw make SteamVR swap in a new
texture on every change, which flickers. Input is OpenVR's laser-pointer mouse.
"""
import ctypes
import functools
import importlib.util
import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

__version__ = '0.1.0'
HOME = Path.home()
CONFIG = Path(os.environ.get('FRAME_REMOTE_CONFIG', HOME / '.config/frame-remote/frame.toml'))


def log(msg):
    print(msg, flush=True)


def ensure_openvr_lib():
    """pyopenvr ships x86 libraries only; on ARM point it at SteamVR's own one.
    Re-checked at every start, so a SteamVR update that moves the file is fine."""
    if platform.machine() not in ('aarch64', 'arm64'):
        return
    pkg = Path(importlib.util.find_spec('openvr').submodule_search_locations[0])
    runtimes = []
    try:
        vrpath = json.loads((HOME / '.config/openvr/openvrpaths.vrpath').read_text())
        runtimes = vrpath.get('runtime') or []
    except (OSError, ValueError):
        pass
    for rt in runtimes + ['/opt/steamvr']:
        lib = Path(rt) / 'bin/linuxarm64/libopenvr_api.so'
        if lib.exists():
            link = pkg / 'libopenvr_api_64.so'
            if not (link.is_symlink() and link.resolve() == lib.resolve()):
                link.unlink(missing_ok=True)
                link.symlink_to(lib)
                log(f'OpenVR library: {lib}')
            return
    raise SystemExit('No SteamVR runtime with an ARM64 libopenvr_api.so found')


os.environ.setdefault('EGL_PLATFORM', 'surfaceless')
os.environ.setdefault('PYOPENGL_PLATFORM', 'egl')
ensure_openvr_lib()
import openvr                                   # noqa: E402
from OpenGL import EGL, GL                      # noqa: E402
from PIL import Image, ImageDraw, ImageFont     # noqa: E402

with CONFIG.open('rb') as _f:
    CFG = tomllib.load(_f)
PC = CFG['pc'].rstrip('/')
TOKEN = CFG['token']
PANEL_NAME = CFG.get('title', 'PC Remote')
TOAST = CFG.get('toast', {})
TOAST_ON = TOAST.get('enabled', True)
TOAST_SECS = float(TOAST.get('seconds', 4))
TOAST_DIST = float(TOAST.get('distance', 1.2))     # metres ahead
TOAST_DROP = float(TOAST.get('height', -0.22))     # metres up (negative = below eye level)
WAKE_TIMEOUT = float(CFG.get('wake_timeout', 150))   # seconds to wait for a woken PC
RUN = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'frame-remote'


def local_buttons():
    """[[button]] entries from frame.toml: they run here, on the headset."""
    out = []
    for b in CFG.get('button', []):
        if 'wake' not in b and 'command' not in b:
            raise SystemExit(f"{CONFIG}: button {b.get('id')!r} needs 'command' or 'wake'")
        mac = re.sub(r'[^0-9a-fA-F]', '', b.get('wake', ''))
        if 'wake' in b and len(mac) != 12:
            raise SystemExit(f"{CONFIG}: button {b.get('id')!r}: wake needs a MAC like aa:bb:cc:dd:ee:ff")
        out.append({
            'id': 'local:' + b['id'], 'title': b.get('title', b['id']),
            'subtitle': b.get('subtitle', ''), 'row': b.get('row', ''),
            'confirm': bool(b.get('confirm', False)), 'show': b.get('show', 'always'),
            'mac': mac or None, 'broadcast': b.get('broadcast', '255.255.255.255'),
            'command': b.get('command'),
        })
    return out


LOCAL = local_buttons()
RUN.mkdir(exist_ok=True)

W = 1200
BG, SURFACE, SURFACE_HI = (15, 15, 15), (23, 22, 20), (40, 36, 32)
ACCENT, TEXT, MUTED = (217, 119, 87), (232, 227, 221), (150, 144, 138)
GREEN, RED, EDGE = (120, 190, 120), (225, 95, 85), (55, 50, 46)
BH, GAP, LEFT = 200, 30, 60          # button height, gap, side margin
ROW_TOP, ROW_PITCH = 270, 275        # first row's buttons, distance between rows
RESULT_SECS = 8                      # how long a button shows Done / Failed
TW, TH = 900, 180                    # toast size in pixels


# --- fonts -----------------------------------------------------------------

FONTS = [  # (regular, bold or None for a variable font with a Bold instance)
    ('/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf', None),
    ('/usr/share/fonts/TTF/DejaVuSans.ttf', '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/dejavu/DejaVuSans.ttf', '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
]


@functools.lru_cache(maxsize=None)
def font(size, bold=False):
    for regular, bold_file in FONTS:
        if bold_file is None and os.path.exists(regular):
            f = ImageFont.truetype(regular, size)
            try:
                f.set_variation_by_name(b'Bold' if bold else b'Regular')
            except OSError:
                pass
            return f
        if bold_file and os.path.exists(bold_file if bold else regular):
            return ImageFont.truetype(bold_file if bold else regular, size)
    return ImageFont.load_default(size)


def fit(d, text, max_w, size, bold=False, min_size=20):
    """The largest font (down to min_size) that fits text into max_w pixels."""
    while size > min_size and d.textlength(text, font=font(size, bold)) > max_w:
        size -= 2
    return font(size, bold)


def clip(d, text, max_w, fnt):
    if d.textlength(text, font=fnt) <= max_w:
        return text
    while text and d.textlength(text + '…', font=fnt) > max_w:
        text = text[:-1]
    return text + '…'


# --- state + talking to the PC --------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = None        # last /status reply, or None
        self.error = 'Connecting to the PC...'
        self.hover = None         # button id under the pointer
        self.confirm = None       # (button id, deadline)
        self.phase = {}           # button id -> (sent|running|ok|failed, until or None)
        self.message = ''
        self.msg_color = MUTED
        self.toast = None         # (text, color, until)
        self.toast_dirty = False
        self.dirty = True

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.dirty = True

    def set_phase(self, bid, phase, secs=None):
        with self.lock:
            self.phase[bid] = (phase, time.time() + secs if secs else None)
            self.dirty = True

    def pop(self, text, color):
        if not TOAST_ON:
            return
        with self.lock:
            self.toast = (text, color, time.time() + TOAST_SECS)
            self.toast_dirty = True

    def buttons(self):
        """Local buttons (filtered by `show`) first, then the PC's."""
        online = self.status is not None
        local = [b for b in LOCAL if b['show'] == 'always'
                 or (b['show'] == 'offline') != online]
        return local + ((self.status or {}).get('buttons') or [])


def http(method, path, timeout=4):
    req = urllib.request.Request(PC + path, method=method, headers={'X-Token': TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except ValueError:
            return e.code, {}


def same_status(a, b):
    strip = lambda s: {k: v for k, v in (s or {}).items() if k != 'now'}
    return a is not None and b is not None and strip(a) == strip(b)


def refresh(st):
    try:
        code, data = http('GET', '/status')
        if code == 200:
            if not (same_status(st.status, data) and st.error is None):
                st.set(status=data, error=None)   # redraw only on a real change
            else:
                st.status = data
            return
        err = {403: 'PC refused: wrong token, or this headset is not in [server].allowed'}.get(
            code, f'PC answered {code}')
    except Exception as e:
        err = f"Can't reach the PC at {PC}"
        log(f'status failed: {e}')
    if st.error != err:
        st.set(status=None, error=err)


def finish(st, bid, title, ok, detail):
    color = GREEN if ok else RED
    st.set_phase(bid, 'ok' if ok else 'failed', RESULT_SECS)
    st.set(message=f'{title}: {detail}', msg_color=color)
    st.pop(f'{"✓" if ok else "✗"}  {title}: {detail}', color)


def run_command(st, bid, title):
    st.set_phase(bid, 'sent')
    st.set(message=f'{title}: sending to the PC...', msg_color=ACCENT)
    try:
        code, data = http('POST', f'/run/{bid}')
    except Exception as e:
        log(f'run {bid} failed: {e}')
        return finish(st, bid, title, False, 'PC not reachable')
    if code not in (202, 409):
        return finish(st, bid, title, False, f'refused ({code} {data.get("error", "")})')
    st.set_phase(bid, 'running')
    st.set(message=f'{title}: started on the PC, running...', msg_color=ACCENT)
    st.pop(f'▶  {title}: started on the PC', ACCENT)
    t0 = time.time()
    while time.time() - t0 < 600:
        time.sleep(1)
        refresh(st)
        job = (st.status or {}).get('jobs', {}).get(bid)
        if job and job['state'] != 'running':
            ok = job['state'] == 'ok'
            detail = job.get('detail') or ('done' if ok else f'failed (exit {job["rc"]})')
            return finish(st, bid, title, ok, detail)
    finish(st, bid, title, False, 'no answer from the PC')


def send_magic_packet(mac, broadcast):
    packet = b'\xff' * 6 + bytes.fromhex(mac) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for addr in dict.fromkeys((broadcast, '255.255.255.255')):
                sock.sendto(packet, (addr, 9))
            time.sleep(0.2)


def run_local(st, b):
    bid, title = b['id'], b['title']
    st.set_phase(bid, 'running')
    if b['mac']:
        try:
            send_magic_packet(b['mac'], b['broadcast'])
        except OSError as e:
            return finish(st, bid, title, False, f'could not send: {e}')
        log(f"wake: magic packet for {b['mac']} sent")
        st.set(message=f'{title}: magic packet sent, waiting for the PC...', msg_color=ACCENT)
        st.pop(f'▶  {title}: magic packet sent', ACCENT)
        t0 = time.time()
        while time.time() - t0 < WAKE_TIMEOUT:
            refresh(st)
            if st.status is not None:
                return finish(st, bid, title, True, f'PC is up ({time.time() - t0:.0f} s)')
            time.sleep(3)
        return finish(st, bid, title, False, f'no answer after {WAKE_TIMEOUT:.0f} s')
    cmd = b['command']
    argv = cmd if isinstance(cmd, list) else ['sh', '-c', cmd]
    st.set(message=f'{title}: running on the headset...', msg_color=ACCENT)
    try:
        p = subprocess.run([os.path.expanduser(a) for a in argv], capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
        out = (p.stdout + p.stderr).strip()
        ok = p.returncode == 0
        detail = out.splitlines()[-1][:100] if out else ('done' if ok else f'failed (exit {p.returncode})')
    except Exception as e:
        ok, detail = False, str(e)[:100]
    finish(st, bid, title, ok, detail)


# --- layout + drawing ------------------------------------------------------

def layout(buttons):
    """{button id: rect}, [(row label, y)], panel height."""
    rows = []
    for b in buttons:
        if b['row'] not in rows:
            rows.append(b['row'])
    rects, labels = {}, []
    for r, row in enumerate(rows):
        top = ROW_TOP + r * ROW_PITCH
        labels.append((row, top))
        in_row = [b for b in buttons if b['row'] == row]
        bw = (W - 2 * LEFT - (len(in_row) - 1) * GAP) // len(in_row)
        for k, b in enumerate(in_row):
            x = LEFT + k * (bw + GAP)
            rects[b['id']] = (x, top, x + bw, top + BH)
    last = ROW_TOP + (len(rows) - 1) * ROW_PITCH if rows else ROW_TOP - ROW_PITCH
    return rects, labels, last + BH + 145


def centered(d, box, text, fnt, fill):
    x0, y0, x1, y1 = box
    d.multiline_text(((x0 + x1) // 2, (y0 + y1) // 2), text, font=fnt, fill=fill,
                     anchor='mm', align='center')


def render(st, rects, labels, h):
    img = Image.new('RGBA', (W, h), BG + (255,))
    d = ImageDraw.Draw(img)
    d.text((LEFT, 40), PANEL_NAME, font=font(56, True), fill=TEXT)
    s = st.status
    if s is None:
        d.text((LEFT, 122), clip(d, st.error or '', W - 2 * LEFT, font(30)), font=font(30), fill=RED)
    else:
        name_x = LEFT + d.textlength(PANEL_NAME, font=font(56, True)) + 30
        d.text((name_x, 62), clip(d, s.get('name', ''), W - LEFT - name_x, font(30)),
               font=font(30), fill=MUTED)
        lights = ([('SteamVR', s['steamvr'])] if s.get('steamvr') is not None else []) + \
                 [(i['label'], i['on']) for i in s.get('indicators', [])]
        x = LEFT
        for label, on in lights:
            txt = f'{label}: {"on" if on else "off"}'
            w = d.textlength(txt, font=font(30)) + 30
            if x + w > W - LEFT:
                break
            d.ellipse((x, 134, x + 18, 152), fill=GREEN if on else MUTED)
            d.text((x + 30, 122), txt, font=font(30), fill=TEXT)
            x += w + 50
        games = s.get('games')
        if games is not None:
            line = ('Game: ' + ', '.join(games)) if games else 'No game running'
            d.text((LEFT, 168), clip(d, line, W - 2 * LEFT, font(30)), font=font(30),
                   fill=TEXT if games else MUTED)
    for label, top in labels:
        if label:
            d.text((LEFT + 4, top - 44), label.upper(), font=font(26, True), fill=ACCENT)
    now = time.time()
    for b in st.buttons():
        bid = b['id']
        x0, y0, x1, y1 = rects[bid]
        phase, until = st.phase.get(bid, (None, None))
        if until and until < now:
            phase = None
        confirming = st.confirm and st.confirm[0] == bid and st.confirm[1] > now
        fill, outline, width = SURFACE, EDGE, 3
        if st.hover == bid:
            fill, outline = SURFACE_HI, ACCENT
        if phase in ('sent', 'running') or confirming:
            outline, width = ACCENT, 6
        elif phase == 'ok':
            fill, outline, width = (28, 44, 28), GREEN, 6
        elif phase == 'failed':
            fill, outline, width = (52, 24, 22), RED, 6
        d.rounded_rectangle((x0, y0, x1, y1), 28, fill=fill, outline=outline, width=width)
        inner = x1 - x0 - 30
        tf = fit(d, b['title'], inner, 36, True, 24)
        d.text(((x0 + x1) // 2, y0 + 72), clip(d, b['title'], inner, tf), font=tf,
               fill=TEXT, anchor='mm')
        lower = (x0, y0 + 105, x1, y0 + 170)
        if confirming:
            centered(d, lower, 'Tap again to confirm', fit(d, 'Tap again to confirm', inner, 32, True), ACCENT)
        elif phase == 'sent':
            centered(d, lower, 'Sent...', font(34, True), ACCENT)
        elif phase == 'running':
            centered(d, lower, 'Running...', font(34, True), ACCENT)
        elif phase == 'ok':
            centered(d, lower, '✓ Done', font(38, True), GREEN)
        elif phase == 'failed':
            centered(d, lower, '✗ Failed', font(38, True), RED)
        elif b.get('subtitle'):
            centered(d, lower, clip(d, b['subtitle'], inner, font(26)), font(26), MUTED)
    if st.message:   # result banner along the bottom
        by = h - 120
        d.rounded_rectangle((LEFT, by, W - LEFT, h - 25), 20,
                            fill=tuple(c // 5 for c in st.msg_color) + (255,),
                            outline=st.msg_color, width=3)
        fnt = fit(d, st.message, W - 2 * LEFT - 40, 34, True, 22)
        centered(d, (LEFT, by, W - LEFT, h - 25), clip(d, st.message, W - 2 * LEFT - 40, fnt),
                 fnt, st.msg_color)
    return img


def render_toast(text, color):
    img = Image.new('RGBA', (TW, TH), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, TW - 4, TH - 4), 40, fill=SURFACE + (240,), outline=color, width=8)
    fnt = fit(d, text, TW - 60, 44, True, 24)
    centered(d, (0, 0, TW, TH), clip(d, text, TW - 60, fnt), fnt, color)
    return img


def make_icon(path):
    img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), 48, fill=ACCENT + (255,))
    d.rounded_rectangle((52, 70, 204, 172), 12, outline=BG + (255,), width=12)  # monitor
    d.rectangle((116, 172, 140, 196), fill=BG + (255,))
    d.rounded_rectangle((80, 192, 176, 208), 6, fill=BG + (255,))
    d.text((128, 121), '>_', font=font(56, True), fill=BG + (255,), anchor='mm')
    img.save(path)


# --- OpenGL-backed overlays ------------------------------------------------

def gl_init():
    d = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
    EGL.eglInitialize(d, None, None)
    EGL.eglBindAPI(EGL.EGL_OPENGL_API)
    ctx = EGL.eglCreateContext(d, EGL.EGLConfig(), EGL.EGL_NO_CONTEXT, None)
    EGL.eglMakeCurrent(d, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, ctx)
    log(f'GL: {GL.glGetString(GL.GL_RENDERER).decode()}')


class GLOverlay:
    """One overlay backed by one OpenGL texture that is updated in place."""

    def __init__(self, ov, handle):
        self.ov, self.handle, self.tex, self.size = ov, handle, None, None
        self.vrtex = openvr.Texture_t()
        self.vrtex.eType = openvr.TextureType_OpenGL
        self.vrtex.eColorSpace = openvr.ColorSpace_Auto

    def show(self, img):
        w, h = img.size
        if self.size != (w, h):              # new size (more rows): new texture once
            if self.tex is not None:
                GL.glDeleteTextures([self.tex])
            self.tex = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA,
                            GL.GL_UNSIGNED_BYTE, bytes(w * h * 4))
            self.vrtex.handle = ctypes.c_void_p(int(self.tex))
            self.size = (w, h)
        # GL's origin is bottom-left, so the first row uploaded is the bottom one
        data = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, w, h, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
        GL.glFinish()
        self.ov.setOverlayTexture(self.handle, self.vrtex)


def make_toast_overlay(ov):
    h = ov.createOverlay('frame-remote.toast', f'{PANEL_NAME} toast')
    ov.setOverlayWidthInMeters(h, 0.6)
    ov.setOverlaySortOrder(h, 200)            # above the dashboard
    m = openvr.HmdMatrix34_t()
    for r, row in enumerate(((1, 0, 0, 0), (0, 1, 0, TOAST_DROP), (0, 0, 1, -TOAST_DIST))):
        for c, v in enumerate(row):
            m.m[r][c] = v
    ov.setOverlayTransformTrackedDeviceRelative(h, openvr.k_unTrackedDeviceIndex_Hmd, m)
    return h


def wait_for_vrserver():
    while subprocess.run(['pgrep', '-x', 'vrserver'], capture_output=True).returncode:
        time.sleep(5)


# --- main loop -------------------------------------------------------------

def main():
    wait_for_vrserver()
    gl_init()
    openvr.init(openvr.VRApplication_Overlay)
    ov = openvr.IVROverlay()
    main_h, thumb_h = ov.createDashboardOverlay('frame-remote.panel', PANEL_NAME)
    icon = RUN / 'icon.png'
    make_icon(icon)
    ov.setOverlayFromFile(thumb_h, str(icon))
    ov.setOverlayWidthInMeters(main_h, 2.0)
    ov.setOverlayInputMethod(main_h, openvr.VROverlayInputMethod_Mouse)
    panel = GLOverlay(ov, main_h)
    toast_h = make_toast_overlay(ov)
    toast = GLOverlay(ov, toast_h)
    toast_shown = False
    log(f'Frame Remote {__version__}: dashboard overlay created, PC {PC}')

    st = State()
    threading.Thread(target=refresh, args=(st,), daemon=True).start()
    ev = openvr.VREvent_t()
    last_poll = 0.0
    rects, labels, h = layout([])
    scaled_h = None                 # panel height the mouse scale was set for
    while True:
        while True:
            ok, ev = ov.pollNextOverlayEvent(main_h, ev)
            if not ok:
                break
            t = ev.eventType
            if t in (openvr.VREvent_MouseMove, openvr.VREvent_MouseButtonUp):
                x, y = ev.data.mouse.x, h - ev.data.mouse.y   # OpenVR's mouse y is bottom-up
                bid = next((k for k, (x0, y0, x1, y1) in rects.items()
                            if x0 <= x <= x1 and y0 <= y <= y1), None)
                if bid != st.hover:
                    st.set(hover=bid)
                if t == openvr.VREvent_MouseButtonUp and bid is not None:
                    b = next(b for b in st.buttons() if b['id'] == bid)
                    log(f'click {bid}')
                    if st.phase.get(bid, (None,))[0] in ('sent', 'running'):
                        continue
                    armed = st.confirm and st.confirm[0] == bid and st.confirm[1] > time.time()
                    if b.get('confirm') and not armed:
                        st.set(confirm=(bid, time.time() + 4))
                    else:
                        st.set(confirm=None)
                        if bid.startswith('local:'):
                            threading.Thread(target=run_local, args=(st, b), daemon=True).start()
                        else:
                            threading.Thread(target=run_command, args=(st, bid, b['title']),
                                             daemon=True).start()
            elif t == openvr.VREvent_OverlayHidden:
                st.set(hover=None, confirm=None)
            elif t == openvr.VREvent_OverlayShown:
                threading.Thread(target=refresh, args=(st,), daemon=True).start()
            elif t == openvr.VREvent_Quit:
                log('SteamVR is quitting')
                openvr.VRSystem().acknowledgeQuit_Exiting()
                openvr.shutdown()
                return
        now = time.time()
        if st.confirm and st.confirm[1] < now:
            st.set(confirm=None)
        with st.lock:   # results expiring back to the normal button
            for bid, (phase, until) in list(st.phase.items()):
                if until and until < now:
                    del st.phase[bid]
                    st.dirty = True
        if ov.isActiveDashboardOverlay(main_h) and now - last_poll > 3:
            last_poll = now
            threading.Thread(target=refresh, args=(st,), daemon=True).start()
        if st.dirty:
            with st.lock:
                st.dirty = False
                new_rects, labels, new_h = layout(st.buttons())
                img = render(st, new_rects, labels, new_h)
            if new_h != scaled_h:
                scale = openvr.HmdVector2_t()
                scale.v[0], scale.v[1] = W, new_h
                ov.setOverlayMouseScale(main_h, scale)
                scaled_h = new_h
            rects, h = new_rects, new_h
            panel.show(img)
        if st.toast_dirty:
            with st.lock:
                st.toast_dirty = False
                text, color, _ = st.toast
            toast.show(render_toast(text, color))
            ov.showOverlay(toast_h)
            toast_shown = True
        elif toast_shown and st.toast and st.toast[2] < now:
            ov.hideOverlay(toast_h)
            toast_shown = False
        time.sleep(0.03)


if __name__ == '__main__':
    main()
