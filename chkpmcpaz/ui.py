"""Terminal UI for the long-running commands (deploy / destroy / status).

Port of the AWS repo's chkpmcpaws/ui.py -- same design goals, in priority order:
  1. NEVER lose information: every detail line is tee'd to a log file
     (~/.chkpmcpaz/logs/ by default, CHKP_LOG_DIR to override) with ANSI/OSC
     escape sequences stripped -- the visible text is verbatim -- and the
     non-TTY output is the same plain line stream the tool always printed --
     CI, pipes, and `script(1)` captures stay grep-able.
  2. Zero new dependencies: pure stdlib ANSI (truecolor when the terminal
     advertises it, 16-color fallback otherwise).
  3. When stdout IS an interactive terminal: take over the ALTERNATE SCREEN
     BUFFER (like vim/top/less) and render a centered live frame -- header,
     gradient progress bar, step checklist with per-step timers, and a tail
     pane streaming the current step's output -- repainted by a background
     ticker so spinners/timers move even while a subprocess blocks. On exit
     the alt screen is dropped (restoring the user's scrollback untouched)
     and only the summary is printed to the normal buffer.

Restore is bulletproof: close(), the exception paths in the callers, and an
atexit hook all guarantee the normal screen + cursor come back even on Ctrl-C
or an unhandled error -- a stuck alt-screen/hidden-cursor terminal is the one
truly unacceptable failure mode for a takeover UI.

Opt-outs: --plain flag, CHKP_UI=plain, or the NO_COLOR convention.

Secret values never reach the log: callers guarantee only names/key names are
ever passed to detail lines. The tee's only transformation is stripping
terminal escape sequences -- it does not inspect or redact content.
"""

import atexit
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ULINE = "\x1b[4m"
HIDE = "\x1b[?25l"
SHOW = "\x1b[?25h"
ALT_ON = "\x1b[?1049h"   # enter alternate screen buffer
ALT_OFF = "\x1b[?1049l"  # leave it -- restores the pre-run scrollback
HOME = "\x1b[H"
CLR_EOL = "\x1b[K"
CLR_BELOW = "\x1b[J"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Check Point brand ramp (magenta -> violet), same as the AWS repo.
C_A = (255, 45, 149)
C_B = (122, 60, 170)
C_OK = (46, 204, 113)
C_ERR = (220, 60, 60)
C_WARN = (252, 177, 23)
C_MUTED = (128, 132, 150)

TITLE = "◆ chkpmcpaz  ·  Check Point MCP on Microsoft Foundry"
TITLE_BRAND = "◆ chkpmcpaz"

FORCE_PLAIN = False  # set by the CLI --plain flag


def _truecolor():
    return "truecolor" in os.environ.get("COLORTERM", "") or "24bit" in os.environ.get(
        "COLORTERM", ""
    )


def _tty_ui_wanted():
    if FORCE_PLAIN or os.environ.get("CHKP_UI") == "plain" or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CHKP_UI") == "tui":
        return True
    return sys.stdout.isatty() and os.environ.get("TERM", "") not in ("", "dumb")


def _rgb(c):
    if _truecolor():
        return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
    # 16-color approximation: pick by hue-ish dominance.
    r, g, b = c
    if g > r and g > b:
        return "\x1b[32m"
    if r > 200 and b > 100:
        return "\x1b[95m"
    if r > 200 and g > 120:
        return "\x1b[33m"
    if r > 180:
        return "\x1b[31m"
    return "\x1b[90m"


def ok(text):
    """Green (success) styling -- or plain text when color is disabled (--plain,
    CHKP_UI=plain, NO_COLOR, piped/non-TTY). Used for the guardrail 'a block is a
    win' banner and the 'screening passed' line, so a caught attack reads as a
    security win (parity with the AWS port)."""
    return (_rgb(C_OK) + text + RESET) if _tty_ui_wanted() else text


def muted(text):
    """Dim styling for secondary lines (e.g. the 'screening…' progress line);
    plain when color is disabled."""
    return (_rgb(C_MUTED) + text + RESET) if _tty_ui_wanted() else text


def err(text):
    """Red styling -- used for a guardrail BLOCK (firewall-style deny: allow is
    green, block is red). Not an error render: the run still exits 0. Plain when
    color is disabled (--plain, CHKP_UI=plain, NO_COLOR, piped/non-TTY)."""
    return (_rgb(C_ERR) + text + RESET) if _tty_ui_wanted() else text


def _lerp(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _grad(text, c1=C_A, c2=C_B):
    if not _truecolor():
        return _rgb(c1) + text + RESET
    n = max(len(text) - 1, 1)
    return "".join(_rgb(_lerp(c1, c2, i / n)) + ch for i, ch in enumerate(text)) + RESET


def _log_path(command):
    """~/.chkpmcpaz/logs/<command>-<timestamp>.log (CHKP_LOG_DIR overrides)."""
    base = os.environ.get("CHKP_LOG_DIR") or os.path.join(
        os.path.expanduser("~"), ".chkpmcpaz", "logs"
    )
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.join(os.path.expanduser("~"), ".chkpmcpaz")
        os.makedirs(base, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(base, f"{command}-{stamp}.log")


class StepUI:
    """Step-oriented progress reporter (the AWS repo's Reporter, renamed per
    the contract).

    Plain mode  : prints `[k/N] label` headers + indented detail lines.
    TTY mode    : repaints a live frame; detail lines stream into the pane.
    Both modes  : everything is tee'd to the log file.

    Use either the imperative API (begin/detail/fail_current/close) or the
    `with ui_obj.step("label"):` context manager, which begins the step and
    marks it failed if the body raises.
    """

    PANE = 6

    def __init__(self, command, badge, steps, location):
        self.command = command
        self.badge = badge
        self.steps = list(steps)
        self.location = location
        self.state = ["pending"] * len(self.steps)  # pending|run|done|warn|fail
        self.secs = [None] * len(self.steps)
        self.idx = -1
        self.context = ""
        self.pane = []
        self.t0 = time.time()
        self._step_t0 = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._tty = _tty_ui_wanted()
        self._restored = False
        self.log_file = None
        self.log_path = None
        try:
            self.log_path = _log_path(command)
            self.log_file = open(self.log_path, "a", buffering=1, encoding="utf-8")
            self._tee(f"==== chkpmcpaz {command} · location {location} · "
                      f"{datetime.now().isoformat(timespec='seconds')} ====")
        except OSError:
            self.log_file = None
        if self._tty:
            # Take over the alternate screen; atexit guarantees we hand it back
            # even on SIGTERM / os._exit-adjacent paths close() never reaches.
            atexit.register(self._emergency_restore)
            sys.stdout.write(ALT_ON + HIDE + "\x1b[2J" + HOME)
            sys.stdout.flush()
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()

    def _emergency_restore(self):
        if self._tty and not self._restored:
            self._restored = True
            try:
                sys.stdout.write(SHOW + ALT_OFF)
                sys.stdout.flush()
            except Exception:
                pass

    # ---------------------------------------------------------------- tee --
    def _tee(self, line):
        # Defensive strip: the log file must stay plain-text grep-able, so any
        # ANSI/OSC styling that reaches the tee (e.g. a styled summary banner)
        # is removed. Detail/step lines are plain already -- bytes unchanged.
        if self.log_file:
            try:
                self.log_file.write(strip_ansi(line).rstrip("\n") + "\n")
            except OSError:
                pass

    # ------------------------------------------------------------- events --
    def set_context(self, text):
        with self._lock:
            self.context = text
            self._paint()

    def begin(self, label=None):
        """Advance to the next step (marks the previous one done)."""
        with self._lock:
            self._finish_current("done")
            self.idx += 1
            if label and self.idx < len(self.steps):
                self.steps[self.idx] = label
            self.state[self.idx] = "run"
            self._step_t0 = time.time()
            self.pane = []
            self._tee(f"[{self.idx + 1}/{len(self.steps)}] {self.steps[self.idx]}")
            if not self._tty:
                print(f"\n[{self.idx + 1}/{len(self.steps)}] {self.steps[self.idx]}",
                      flush=True)
            else:
                self._paint()

    @contextmanager
    def step(self, name=None):
        """Begin the next step (optionally relabelled `name`); mark it failed
        if the body raises. Exceptions propagate -- callers decide whether a
        step failure aborts the command or is collected as a partial failure."""
        self.begin(name)
        try:
            yield self
        except BaseException:
            self.fail_current()
            raise

    def detail(self, msg):
        for raw in str(msg).split("\n"):
            line = raw.rstrip()
            self._tee("    " + line if line else "")
            with self._lock:
                if line:
                    self.pane.append(line.strip())
                    self.pane = self.pane[-self.PANE:]
            if not self._tty:
                print(raw, flush=True)
        if self._tty:
            with self._lock:
                self._paint()

    def warn_current(self):
        with self._lock:
            if 0 <= self.idx < len(self.steps) and self.state[self.idx] == "run":
                self.state[self.idx] = "run-warn"

    def fail_current(self):
        with self._lock:
            self._finish_current("fail")

    def _finish_current(self, final):
        if 0 <= self.idx < len(self.steps) and self.state[self.idx].startswith("run"):
            self.state[self.idx] = (
                "warn" if (self.state[self.idx] == "run-warn" and final == "done") else final
            )
            self.secs[self.idx] = int(time.time() - (self._step_t0 or time.time()))

    def close(self, ok=True, summary=()):
        """Stop the ticker, leave the alt screen, print the summary (both modes)."""
        with self._lock:
            self._finish_current("done" if ok else "fail")
            self._stop.set()
        if self._tty:
            # Join the ticker BEFORE restoring so no stray frame paints into
            # the normal buffer after we switch back.
            if getattr(self, "_ticker", None):
                self._ticker.join(timeout=0.5)
            with self._lock:
                self._paint(final=True)   # show the completed 100% frame...
            time.sleep(0.4)               # ...briefly, so the eye catches it
            sys.stdout.write(SHOW + ALT_OFF)
            sys.stdout.flush()
            self._restored = True
        # Each summary item is either a plain `str` (tee'd and printed as-is,
        # the historical contract) or a `(terminal_line, log_line)` 2-tuple --
        # styled text for the terminal, plain text for the log -- the same
        # split the `full log:` tail below has always used.
        for item in summary:
            term, logline = item if isinstance(item, tuple) else (item, item)
            self._tee(logline)
            print(term, flush=True)
        if self.log_path:
            tail = f"full log: {self.log_path}"
            self._tee(tail)
            print(_rgb(C_MUTED) + tail + RESET if self._tty else tail, flush=True)
        if self.log_file:
            try:
                self.log_file.close()
            except OSError:
                pass

    # ------------------------------------------------------------ painting --
    def _tick(self):
        while not self._stop.is_set():
            with self._lock:
                self._paint()
            time.sleep(0.12)

    def _bar(self, pct, width):
        fill = max(0, min(width, int(width * pct)))
        if _truecolor():
            cells = "".join(
                _rgb(_lerp(C_A, C_B, i / max(width - 1, 1))) + "█" for i in range(fill)
            )
        else:
            cells = _rgb(C_A) + "█" * fill
        return cells + RESET + DIM + "·" * (width - fill) + RESET

    def _glyph(self, i):
        st = self.state[i]
        if st == "done":
            return _rgb(C_OK) + "✔", _rgb(C_OK)
        if st == "warn":
            return _rgb(C_WARN) + "⚠", _rgb(C_WARN)
        if st == "fail":
            return _rgb(C_ERR) + BOLD + "✗", _rgb(C_ERR) + BOLD
        if st.startswith("run"):
            ch = SPIN[int(time.time() * 8) % len(SPIN)]
            return _rgb(C_A) + BOLD + ch, _rgb(C_A) + BOLD
        return DIM + "○", DIM

    def _frame(self, W, final=False):
        done = sum(1 for s in self.state if s in ("done", "warn"))
        total = len(self.steps)
        pct = 1.0 if final and done == total else (max(self.idx, 0) + 0.2) / max(total, 1)
        el = int(time.time() - self.t0)
        out = []
        badge_vis = f"[ {self.badge} · {self.location} ]"
        badge = _rgb(C_A) + badge_vis + RESET
        title = _grad(TITLE_BRAND) + BOLD + TITLE[len(TITLE_BRAND):] + RESET
        gap = max(1, W - len(TITLE) - len(badge_vis) - 1)
        out.append(" " + title + " " * gap + badge)
        ctx = self.context or "resolving identity…"
        out.append(" " + _rgb(C_MUTED) + ctx + RESET
                   + f"   {BOLD}⏱ {el // 60:02d}:{el % 60:02d}{RESET}"
                   + _rgb(C_OK) + f"   ✓ {done}/{total}" + RESET)
        out.append(" " + self._bar(pct, W - 8) + f" {_rgb(C_A)}{int(pct * 100):3d}%{RESET}")
        out.append("")
        for i, label in enumerate(self.steps):
            g, col = self._glyph(i)
            t = self.secs[i]
            if t is not None:
                tstr = f"{t}s"
            elif self.state[i].startswith("run"):
                st = int(time.time() - (self._step_t0 or time.time()))
                tstr = f"{st // 60:02d}:{st % 60:02d}"
            else:
                tstr = ""
            plain_len = 4 + len(label)
            pad = " " * max(1, W - plain_len - len(tstr) - 2)
            out.append(f"  {g} {col}{label}{RESET}{pad}{_rgb(C_MUTED)}{tstr}{RESET}")
        out.append("")
        cur = self.steps[self.idx] if 0 <= self.idx < total else ""
        head = f"─ {cur} · live output "
        out.append(" " + _rgb(C_MUTED) + "╭" + head + "─" * max(0, W - len(head) - 3) + "╮" + RESET)
        pane = (self.pane + [""] * self.PANE)[: self.PANE] if not self.pane else \
               ([""] * (self.PANE - len(self.pane)) + self.pane)[-self.PANE:]
        for ln in pane:
            ln = ln[: W - 6]
            out.append(" " + _rgb(C_MUTED) + "│ " + RESET + DIM + ln + RESET
                       + " " * max(0, W - 4 - len(ln)) + _rgb(C_MUTED) + "│" + RESET)
        foot = f"─ full log → {self.log_path or 'n/a'} "
        out.append(" " + _rgb(C_MUTED) + "╰" + foot[: W - 4] + "─" * max(0, W - len(foot) - 3) + "╯" + RESET)
        return out

    def _paint(self, final=False):
        # Skip stray paints after close() has begun tearing down (the ticker
        # may still be mid-loop); the final paint from close passes final=True.
        if not self._tty or (self._stop.is_set() and not final):
            return
        size = shutil.get_terminal_size((100, 30))
        cols, rows = size.columns, size.lines
        W = max(52, min(cols - 2, 110))
        lines = self._frame(W, final=final)
        # Center vertically when the frame fits; clip from the top on a tiny
        # terminal so we never scroll (which would break home-based repaint).
        if len(lines) > rows:
            lines = lines[:rows]
            top = 0
        else:
            top = max(0, (rows - len(lines)) // 2)
        margin = " " * max(0, (cols - W) // 2)
        parts = [HOME]
        parts += [CLR_EOL + "\n"] * top
        for i, ln in enumerate(lines):
            parts.append(margin + ln + CLR_EOL)
            if i != len(lines) - 1:
                parts.append("\n")
        parts.append(CLR_BELOW)
        sys.stdout.write("".join(parts))
        sys.stdout.flush()


# The AWS repo calls this class Reporter; keep the alias so ported call sites
# and muscle memory both work.
Reporter = StepUI


# ---------------------------------------------------------------- summary --
def stack_up_banner(ok=True, partial=False):
    if not ok:
        return _rgb(C_ERR) + BOLD + "✗ INCOMPLETE" + RESET
    if partial:
        return _rgb(C_WARN) + BOLD + "⚠ UP -- PARTIAL" + RESET
    return _grad("✔ STACK UP") if _tty_ui_wanted() else "STACK UP"


def done_banner(label, ok=True):
    """A gradient/green success banner (or red failure) for any command --
    e.g. done_banner('DESTROYED'). Never build banners via str.replace on the
    gradient form: it interleaves ANSI codes between characters."""
    if not ok:
        return _rgb(C_ERR) + BOLD + f"✗ {label} INCOMPLETE" + RESET
    return _grad(f"✔ {label}") if _tty_ui_wanted() else label


# ------------------------------------------------------------ links block --
# Pretty rendering for the (label, url) pairs from the config link helpers
# (console_links in the AWS repo, portal_links in the Azure one). This whole
# section -- marker comment through links_block -- is shared between
# chkpmcpaws/ui.py and chkpmcpaz/ui.py: keep the two copies byte-identical
# (and Python 3.9 compatible, the AWS floor) so a plain diff between the two
# files reviews cleanly.
#
# CLICKABILITY IS SACRED: a URL is never hard-wrapped unless every visual
# fragment is emitted as a complete OSC 8 hyperlink (the link target travels
# in-band, so wrapping cannot break it). Plain terminals linkify by scanning
# contiguous text, so the plain/basic tiers keep each URL intact on its own
# line and rely on terminal-side soft wrap.

TIER_FANCY, TIER_BASIC, TIER_PLAIN = "fancy", "basic", "plain"
OSC8_TERM_PROGRAMS = frozenset({"iTerm.app", "WezTerm", "vscode", "ghostty"})

# CSI sequences (colors/bold/underline/cursor) OR OSC sequences (hyperlinks,
# titles) terminated by BEL or ST; DOTALL so an OSC payload spanning a newline
# still strips. One regex so strip_ansi stays a single sub.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL
)


def supports_osc8(env):
    """Pure. True iff the environment advertises a terminal known to render
    OSC 8 hyperlinks: TERM_PROGRAM in OSC8_TERM_PROGRAMS, or WT_SESSION
    (Windows Terminal), or KITTY_WINDOW_ID. Apple_Terminal is deliberately
    NOT in the set -- it auto-linkifies plain URLs but drops OSC 8 text."""
    return bool(
        env.get("TERM_PROGRAM") in OSC8_TERM_PROGRAMS
        or env.get("WT_SESSION")
        or env.get("KITTY_WINDOW_ID")
    )


def links_render_tier(isatty, env, force_plain):
    """Pure tier chooser (tty/env injected so it is unit-testable). PLAIN on
    any opt-out (--plain, CHKP_UI=plain, NO_COLOR, TERM empty/dumb) or when
    stdout is not a terminal; else FANCY iff the terminal supports OSC 8
    hyperlinks, else BASIC (colors, URLs left intact for auto-linkify).

    Deliberately UNLIKE _tty_ui_wanted(): the CHKP_UI=tui force-on override
    is ignored here, so styled links and hyperlinks never reach a pipe or a
    dumb terminal even when the alt-screen TUI is forced on."""
    if (
        force_plain
        or env.get("CHKP_UI") == "plain"
        or env.get("NO_COLOR")
        or not isatty
        or env.get("TERM", "") in ("", "dumb")
    ):
        return TIER_PLAIN
    return TIER_FANCY if supports_osc8(env) else TIER_BASIC


def strip_ansi(text):
    """Pure. Remove CSI and OSC escape sequences (both BEL- and ST-terminated),
    leaving only visible text (an OSC 8 wrapper's link text survives).
    Idempotent on plain text."""
    return _ANSI_RE.sub("", text)


def visible_len(text):
    """len(strip_ansi(text)) -- padding math for styled cells."""
    return len(strip_ansi(text))


def osc8(url, text):
    """One complete OSC 8 hyperlink: `text` is clickable and opens `url`."""
    return "\x1b]8;;" + url + "\x1b\\" + text + "\x1b]8;;\x1b\\"


def wrap_url(url, width):
    """Hard-chunk `url` into width-sized slices (URLs have no spaces, so no
    hyphenation). [] for an empty url. ONLY ever called on text that will be
    wrapped in osc8() -- enforced by the single caller, links_block's fancy
    tier -- because a bare hard-wrapped URL is unclickable."""
    width = max(width, 1)
    return [url[i:i + width] for i in range(0, len(url), width)]


def _links_plain(links, title, indent):
    """The PLAIN tier -- byte-identical to the config *_links_lines helpers
    (regression-pinned): '' lead, title, then per link a bullet label line and
    the URL ALONE on its own line so terminal auto-linkify keeps the whole
    link clickable."""
    lines = ["", f"{indent}{title}"]
    for label, url in links:
        lines.append(f"{indent}  • {label}")
        lines.append(f"{indent}    {url}")
    return lines


def _links_basic(links, title, indent):
    """COLORED BASIC tier: the exact plain two-line layout, styled. The URL is
    one contiguous run with zero escapes inside it (style prefix before, RESET
    after) so the terminal's plain-text linkify scanner still matches it."""
    lines = ["", indent + BOLD + _rgb(C_A) + title + RESET]
    for label, url in links:
        lines.append(indent + "  " + _rgb(C_MUTED) + "•" + RESET + " "
                     + BOLD + _rgb(C_A) + label + RESET)
        lines.append(indent + "    " + ULINE + _rgb(C_B) + url + RESET)
    return lines


def _links_fancy(links, title, indent, width):
    """FANCY tier: a rounded box (same style as the live-output pane), label
    column bold/brand-colored, URL wrapped to the cell width with EVERY chunk
    emitted as its own complete OSC 8 hyperlink -- hard-wrapping is safe here
    because each visual fragment carries the full target in-band."""
    mut, brand = _rgb(C_MUTED), _rgb(C_A)
    W = max(width - len(indent), 20)          # box width; every line == W cols
    Lw = min(max(visible_len(label) for label, _ in links), 36)
    Uw = W - Lw - 7                           # 7 = borders + gutters
    if Uw < 8:                                # narrow box: the URL cell wins
        Lw = max(W - 7 - 8, 1)                # and the label column shrinks,
        Uw = W - Lw - 7                       # so rows never outgrow borders
    head = title.rstrip(":")[: W - 5]
    lines = [
        "",
        indent + mut + "╭─ " + RESET + BOLD + brand + head + RESET
        + mut + " " + "─" * (W - len(head) - 5) + "╮" + RESET,
    ]
    for label, url in links:
        lab = label if len(label) <= Lw else label[: Lw - 1] + "…"
        for i, chunk in enumerate(wrap_url(url, Uw)):
            lab_cell = (BOLD + brand + lab + RESET) if i == 0 else ""
            cell = ULINE + _rgb(C_B) + osc8(url, chunk) + RESET
            lines.append(
                indent + mut + "│ " + RESET
                + lab_cell + " " * (Lw - visible_len(lab_cell)) + "  "
                + cell + " " * (Uw - visible_len(cell))
                + mut + "  │" + RESET
            )
    lines.append(indent + mut + "╰" + "─" * (W - 2) + "╯" + RESET)
    return lines


def links_block(links, title, indent="  ", tier=None, width=None):
    """Presentation for the (label, url) pairs from the config link helpers.
    Returns a list of (terminal_line, log_line) 2-tuples ready to append to a
    rep.close summary; [] when links is empty. The log side is ALWAYS the
    plain rendering (byte-identical to the *_links_lines helpers) so log
    files stay clean whatever the terminal got. When the two renderings
    differ in line count, the longer tail is newline-folded into the last
    tuple -- close() tees/prints multi-line entries intact, so neither side
    gains or loses a byte."""
    if not links:
        return []
    if tier is None:
        tier = links_render_tier(sys.stdout.isatty(), os.environ, FORCE_PLAIN)
    if width is None:
        width = max(60, min(shutil.get_terminal_size((100, 30)).columns - 2, 120))
    log_lines = _links_plain(links, title, indent)
    if tier == TIER_FANCY:
        term_lines = _links_fancy(links, title, indent, width)
    elif tier == TIER_BASIC:
        term_lines = _links_basic(links, title, indent)
    else:
        term_lines = list(log_lines)
    k = min(len(term_lines), len(log_lines))
    pairs = [(term_lines[i], log_lines[i]) for i in range(k - 1)]
    pairs.append(("\n".join(term_lines[k - 1:]), "\n".join(log_lines[k - 1:])))
    return pairs


def render_destroy_plan(location, sections, notes=()):
    """Branded destroy-plan panel, printed to the NORMAL screen buffer right
    before the interactive y/N (which can't run inside the alt-screen frame).

    sections: list of (title, [items]); notes: list of dim strings. Falls back
    to the plain line format when the UI is disabled (--plain / piped / CI).
    """
    if not _tty_ui_wanted():
        print(f"Found to destroy (location {location}):", flush=True)
        for title, items in sections:
            for it in items:
                print(f"  [{title}] {it}", flush=True)
        for n in notes:
            print(f"  ({n})", flush=True)
        return

    cols = shutil.get_terminal_size((100, 30)).columns
    W = max(60, min(cols - 2, 90))
    badge_vis = f"[ DESTROY · {location} ]"
    title = _grad(TITLE_BRAND) + BOLD + TITLE[len(TITLE_BRAND):] + RESET
    gap = max(1, W - len(TITLE) - len(badge_vis))
    rule = " " + _rgb(C_MUTED) + "─" * W + RESET
    out = [
        "",
        " " + title + " " * gap + _rgb(C_A) + badge_vis + RESET,
        rule,
        " " + _rgb(C_ERR) + BOLD + "These resources will be PERMANENTLY destroyed:" + RESET,
        "",
    ]
    for t, items in sections:
        out.append(" " + BOLD + _rgb(C_A) + t + RESET)
        for it in items:
            out.append("   " + _rgb(C_MUTED) + "•" + RESET + " " + it[: W - 5])
        out.append("")
    for n in notes:
        out.append(" " + DIM + n + RESET)
    out.append(rule)
    print("\n".join(out), flush=True)


_ACTIVE = None


def active():
    return _ACTIVE


def activate(reporter):
    """Route azutil.log() into this reporter until deactivate()."""
    global _ACTIVE
    _ACTIVE = reporter
    from . import azutil

    azutil.set_log_sink(reporter.detail)


def deactivate():
    global _ACTIVE
    _ACTIVE = None
    from . import azutil

    azutil.set_log_sink(None)
