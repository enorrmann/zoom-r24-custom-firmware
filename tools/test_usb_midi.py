#!/usr/bin/env python3
"""test_usb_midi.py — sdk/patches/usb_midi.c (docs/22_usb_midi.md).

Runs the patch code in the emulator on build/R24SYS.BIN, with the USB host
API, the RTOS delay and the task calls stubbed and recorded:

  * hooks: SYSTEM menu has 9 items, the 9th is USB MIDI (a 2-value list on
    the patch's own variable, Off at power-on, never saved); the boot detour at 0x80008288, after the bank copy that puts the patch in RAM;
  * class driver record (audio interface class) and attach/detach: only a
    MIDIStreaming interface (subclass 3) is claimed, one at a time;
  * clock against a simulated transport (fs, tempo, position):
      - the transport position is in ms (the firmware's own 0x80059744);
      - play from 0: Start, then one F8 every 600000/(tempo*24) ms (48 per
        second at 120 BPM), each within one step (1 ms) of its exact time;
      - Stop -> FC;
      - play from the middle: Song Position = next 16th, Continue, first F8
        exactly on that 16th;
      - locate while playing: FC, then Song Position + Continue;
      - tempo change: the clock rate follows;
      - packets are USB-MIDI cable 0 (0F F8 00 00, 03 F2 lsb msb);
  * task: with the setting On and the port off it switches the port to host
    (0x8005B066(2, 2), the SYNC REC Master mode: no SD remount), registers the driver once the host stack runs, opens
    the bulk OUT pipe of the claimed interface and sends; SYNC REC != Off or
    the setting Off -> it never touches the port; setting Off after it took
    the port -> USB off (0x8005AFC6).

    python3 tools/build_firmware.py && python3 tools/test_usb_midi.py
"""
from __future__ import annotations

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import r24emu
    from unicorn.arm_const import UC_ARM_REG_SP
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn")
    raise SystemExit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build", "R24SYS.BIN")
BUILT = open(BUILT_PATH, "rb").read()
SYM = json.load(open(BUILT_PATH + ".syms.json"))["symbols"]

SETTINGS = 0x804C6DD4
SYNC_REC = SETTINGS + 0x3C
UM_SETTING = SYM["um_on"]
PORT, USBD_RUN = 0x801EC650, 0x801ED690 + 0x14
XPORT, TEMPO = 0x81220A90, 0x801EB744
USB_MODE, USB_OFF, REGISTER, OPEN_PIPE, XFER, DELAY = (
    0x8005B066, 0x8005AFC6, 0x800818E0, 0x80081BAC, 0x800819D6, 0x800547B8)
TASK_CREATE, TASK_START = 0x800545F8, 0x80054678
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise SystemExit("FAIL: " + what)


def w32(e, a, v):
    e.write(a, struct.pack("<I", v & 0xFFFFFFFF))


def file_u32(a):
    return struct.unpack_from("<I", BUILT, a - 0x80000000 + 0x5B4)[0]


class Usb:
    """Records what the patch sends; the transfer always succeeds."""

    def __init__(self, e):
        self.e, self.packets, self.xfers = e, [], 0
        e.stub(XFER, self.xfer)

    def xfer(self, e, r):
        pipe, buf, n, res = r
        data = e.read(buf, n)
        self.xfers += 1
        for i in range(0, n, 4):
            self.packets.append(data[i:i + 4])
        w32(e, res, 0)
        w32(e, res + 4, n)
        return 0


XSTART = 0x81220A80                 # transport start record: [0] pos (ms), [1] timer


TIMER = 0x80000C80                   # transport timer read (ARM): 64 * fs per second


def transport(e, playing, fs=44100, tempo=1200):
    w32(e, XPORT + 4, 1 if playing else 0)
    w32(e, XPORT + 8, fs)
    w32(e, TEMPO, tempo)
    if not playing:
        return


def timer_emu(e, per_read=0):
    """the hardware timer as a Python value; optionally advancing on each read"""
    e.tm = 0x10000000
    e.reads = 0

    def rd(e, r):
        e.reads += 1
        v = e.tm & 0xFFFFFFFF
        e.tm += per_read
        return v
    e.stub(TIMER, rd)


def play(e, start_ms, fs=44100, tempo=1200, at=None):
    """PLAY (or a locate) at song position start_ms: the start record as 0x800599EE writes it"""
    if at is not None:
        e.tm = at
    stamp = e.tm & 0xFFFFFFFF
    e.write(XSTART, struct.pack("<4I", start_ms, stamp, stamp, 0))
    e.start_ms, e.stamp, e.k = start_ms, e.tm, 64 * fs
    w32(e, XPORT + 0x10, start_ms)
    transport(e, True, fs, tempo)


def at_ms(e, ms):
    """song position ms (since the last play()): the timer, and the whole-ms
    position the firmware publishes (+0x10)"""
    e.tm = e.stamp + round((ms - e.start_ms) * e.k / 1000)
    w32(e, XPORT + 0x10, int(ms))


def step(e):
    e.call(SYM["um_clock_step"])
    e.call(SYM["um_flush"])


def linked_emu(per_read=0):
    e = r24emu.R24Emu(BUILT)
    usb = Usb(e)
    timer_emu(e, per_read)
    w32(e, SYM["um_iface"], 0x92080000)
    w32(e, SYM["um_pipe"], 0x92090000)
    return e, usb


F8, FA, FB, FC = (bytes([0x0F, b, 0, 0]) for b in (0xF8, 0xFA, 0xFB, 0xFC))


def run(e, usb, start, ms, tempo=1200, dt=1, fresh=True, fs=44100):
    """Play from `start` (ms) for `ms`, one step per dt ms; -> clock positions (ms)."""
    times, pos = [], start
    if fresh:                                         # a new PLAY at `start`
        play(e, start, fs=fs, tempo=tempo)
    while pos <= start + ms:
        at_ms(e, pos)
        transport(e, True, fs, tempo)
        before = len(usb.packets)
        step(e)
        times += [pos] * sum(1 for p in usb.packets[before:] if p == F8)
        last = pos
        pos += dt
    return times, last


def main() -> int:
    # ── hooks ──
    ok(file_u32(0x800D4AE4) == 11, "SYSTEM menu count 11")
    arr = file_u32(0x800D4B04)
    ok(arr == SYM["um_items"], "SYSTEM item array -> um_items")
    e = r24emu.R24Emu(BUILT)
    items = struct.unpack("<11I", e.read(arr, 44))
    ok(list(items[:8]) == [0x800D4830, 0x800D4880, 0x800D48D0, 0x800D4920,
                           0x800D49CC, 0x800D4A24, 0x800D4970, 0x800D4A74],
       "the 8 stock items keep their order")
    it = struct.unpack("<20I", e.read(items[8], 80))
    ok(e.cstring(it[0]) == "MIDI SYNC", "item label")
    ok(it[1:3] == (0, 3) and it[7] == UM_SETTING and it[10] == 2,
       "list item: 3 values, the patch's own variable um_on, max 2")
    ok([e.cstring(x) for x in struct.unpack("<3I", e.read(it[3], 12))] == ["Off", "Out", "In"],
       "values Off / Out / In")
    ok(it[11] == SYM["um_changed"] | 1, "change callback of its own (does not mark the settings dirty)")
    ok(e.u32(UM_SETTING) == 0, "Off at power-on (initial value, not loaded from the settings)")
    ofs = struct.unpack("<20I", e.read(items[9], 80))
    ok(e.cstring(ofs[0]) == "MIDI OFFSET" and ofs[2] == 51 and ofs[10] == 50, "MIDI OFFSET: 51 values, max 50")
    ok(not any("TIMING" in e.cstring(struct.unpack("<I", e.read(i, 4))[0]) for i in items),
       "no MIDI TIMING item: the timer timing is the only one")
    cal = struct.unpack("<20I", e.read(items[10], 80))
    ok(e.cstring(cal[0]) == "MIDI CALIBRATE" and cal[1] == 1 and cal[4] == SYM["um_calibrate"] | 1,
       "MIDI CALIBRATE: action item")
    ok(ofs[7] == SYM["um_offset"] and e.u32(SYM["um_offset"]) == 0, "offset variable of its own, 0 at power-on")
    ok([e.cstring(e.u32(ofs[3] + 4 * i)) for i in (0, 5, 20, 50)] == ["0 ms", "5 ms", "20 ms", "50 ms"], "offset values")
    ok(ofs[11] == SYM["um_changed"] | 1, "offset: no settings save either")
    ok(not (SETTINGS <= UM_SETTING < SETTINGS + 0x40), "not inside the saved settings block")
    w32(e, 0x801EB418, 0)
    e.call(SYM["um_changed"])
    ok(e.u32(0x801EB418) == 0, "changing it leaves the settings-dirty flag alone (no NOR write)")
    batt = struct.unpack("<20I", e.read(0x800D4A24, 80))
    ok(it[4:7] == batt[4:7] and it[8:10] == batt[8:10] and it[12:] == batt[12:],
       "other item words as BATTERY")
    ok(e.read(0x80008288, 4) == bytes.fromhex("004b1847"), "boot detour at 0x80008288")
    ok(e.u32(0x8000828C) == SYM["um_det_boot"] | 1, "detour target")
    # the patch code lives in the bank region, copied to RAM by 0x8002D888
    # (bl at 0x8000827E): the boot detour must come after that call
    ok(file_u32(0x8000827E) == struct.unpack("<I", bytes.fromhex("25f003fb"))[0],
       "bank copy bl 0x8002D888 at 0x8000827E")
    ok(0x8000827E < 0x80008288 and SYM["um_det_boot"] >= 0x87540000, "detour after the bank copy")

    # ── driver ──
    drv = SYM["um_driver"]
    vid, pid, dc, ic, match, _pad, att, det = struct.unpack("<HHBBBBII", e.read(drv, 16))
    ok((vid, pid, dc, ic, match) == (0, 0, 0, 1, 8), "driver matches interface class 1 only")
    ok(att == SYM["um_attach"] | 1 and det == SYM["um_detach"] | 1, "driver callbacks")
    ac = e.alloc(bytes(0x11) + bytes([1]) + bytes(0x2E))         # AudioControl
    ms = e.alloc(bytes(0x11) + bytes([3]) + bytes(0x2E))         # MIDIStreaming
    ms2 = e.alloc(bytes(0x11) + bytes([3]) + bytes(0x2E))
    ok(e.call(att, ac) != 0 and e.u32(SYM["um_iface"]) == 0, "AudioControl refused")
    ok(e.call(att, ms) == 0 and e.u32(SYM["um_iface"]) == ms, "MIDIStreaming claimed")
    ok(e.call(att, ms2) != 0 and e.u32(SYM["um_iface"]) == ms, "only one at a time")
    w32(e, SYM["um_pipe"], 0x1234)
    e.call(det, ms2)
    ok(e.u32(SYM["um_iface"]) == ms, "detach of another interface ignored")
    e.call(det, ms)
    ok(e.u32(SYM["um_iface"]) == 0 and e.u32(SYM["um_pipe"]) == 0, "detach forgets iface and pipe")

    # ── the position unit, from the firmware itself ──
    # 0x80059744 (the transport's live position): start + timer * 1000 / (64 * fs),
    # the timer counting 64 * fs per second -> +1000 per second: MILLISECONDS
    # (the first device build took it for samples: clock ~44x slow).
    e = r24emu.R24Emu(BUILT)
    w32(e, XPORT + 8, 44100)
    e.write(0x81220A80, bytes(16))                      # start pos 0, start timer 0
    e.stub(0x80000C80, lambda e, r: 64 * 44100)         # timer: one second later
    ok(e.call(0x80059744) == 1000, "transport position: +1000 per second = ms")

    # ── clock from 0 ──
    e, usb = linked_emu()
    transport(e, False)
    step(e)
    ok(usb.packets == [], "stopped: nothing sent")
    unit = 600000 / (1200 * 24)                       # 20.833 ms per clock at 120 BPM
    times, end = run(e, usb, 0, 2000)
    ok(usb.packets[0] == FA, "play at 0 -> Start first")
    ok(len(times) == 97, f"120 BPM: 48 clocks per second ({len(times)} in 0..2000 ms)")
    ok(all(0 <= t - k * unit < 1 for k, t in enumerate(times)),
       "every clock within one step (1 ms) of its exact time")
    ok(all(len(p) == 4 for p in usb.packets), "4-byte USB-MIDI packets")
    ok(usb.xfers <= len(times) + 1, "one transfer per step at most")
    transport(e, False)
    n = len(usb.packets)
    step(e)
    ok(usb.packets[n:] == [FC], "stop -> FC")
    step(e)
    ok(len(usb.packets) == n + 1, "stays quiet while stopped")

    # ── the timer, not the firmware's whole-ms position ──
    e, usb = linked_emu()
    play(e, 0)
    e.tm += 10                                         # 3.5 us after PLAY
    step(e)
    ok(usb.packets == [FA, F8], "Start + clock 0")
    k = 64 * 44100
    due1 = e.stamp + 600000 * k // (1200 * 24 * 1000)  # clock 1: 20.8333 ms, in ticks
    e.tm = due1 - 3
    step(e)
    ok(usb.packets[2:] == [], "1 us before clock 1: nothing")
    e.tm = due1 + 1
    step(e)
    ok(usb.packets[2:] == [F8], "clock 1 at 20.8333 ms, not at a whole ms")
    ok(e.u32(SYM["um_late_us"]) <= 1, "measured lateness ~0 us")
    # the elapsed time agrees with the firmware's own position function
    e.write(XSTART + 8, struct.pack("<2I", e.tm & 0xFFFFFFFF, 0))
    e.tm = e.stamp + 10 * k                            # 10 s later
    pos = e.call(0x80059744)
    ok(pos == 10000, f"firmware position after 10 s of timer = {pos} ms")
    # timer wrap: the firmware's wrap count + a wrap it has not seen yet
    e, usb = linked_emu()
    play(e, 0, at=0xFFFFF000)
    step(e)
    e.tm = 0x100000000 + 0x2000                        # wrapped, firmware still saw 0xFFFFF000
    e.write(XSTART + 8, struct.pack("<2I", 0xFFFFF000, 0))
    n = len(usb.packets)
    step(e)
    ok(e.u32(SYM["um_last_e"]) == 0x3000, "wrap seen from the timer alone")
    e.write(XSTART + 8, struct.pack("<2I", 0x2000, 1))  # firmware caught up: wrap counted
    step(e)
    ok(e.u32(SYM["um_last_e"]) == 0x3000 and len(usb.packets) == n, "same time once the firmware counted it")

    # ── waiting for the exact moment (um_wait_and_send) ──
    e, usb = linked_emu(per_read=50)                   # each timer read: +17.7 us
    play(e, 0)
    step(e)                                            # Start + clock 0
    w32(e, SYM["um_wake_ticks"], k // 1000)            # wakes every 1 ms
    e.tm = due1 - k // 2000                            # clock 1 due in 0.5 ms
    e.call(SYM["um_wait_and_send"])
    ok(usb.packets[2:] == [F8], "clock due before the next wake: waited for and sent")
    late = e.u32(SYM["um_late_us"])
    ok(late <= 5 * 18, f"sent {late} us after its exact time (here each timer read jumps 17.7 us; a few reads per wait step)")
    e, usb = linked_emu(per_read=50)
    play(e, 0)
    step(e)
    w32(e, SYM["um_wake_ticks"], k // 1000)
    e.tm = due1 - 3 * k // 1000                        # due in 3 ms: the next wake will do
    reads = e.reads
    e.call(SYM["um_wait_and_send"])
    ok(usb.packets[2:] == [] and e.reads - reads < 5, "clock due after the next wake: no busy wait")
    e, usb = linked_emu(per_read=50)
    play(e, 0)
    step(e)
    w32(e, SYM["um_wake_ticks"], k // 1000)
    e.tm = due1 - k // 2000
    w32(e, XPORT + 4, 0)                               # STOP during the wait
    e.call(SYM["um_wait_and_send"])
    ok(usb.packets[2:] == [], "stop during the wait: gives up")
    e, usb = linked_emu(per_read=50)
    play(e, 0)
    step(e)
    w32(e, SYM["um_wake_ticks"], 10 * k // 1000)       # an OS tick of 10 ms
    e.tm = due1 - 5 * k // 1000                        # due in 5 ms
    reads = e.reads
    e.call(SYM["um_wait_and_send"])
    ok(usb.packets[2:] == [] and e.reads - reads < 5, "long OS tick: still no more than 1.5 ms of busy wait")

    # ── the transport start hook: the firmware's own 0x800599EE ──
    def fw_start(e, usb, pos, on=1):
        w32(e, UM_SETTING, on)
        w32(e, SYNC_REC, 0)
        w32(e, XPORT + 0x10, pos)                     # 0x800599EE starts from the published position
        w32(e, XPORT + 8, 44100)
        w32(e, TEMPO, 1200)
        e.stub(0x80031336, lambda e, r: 0)            # the DSP start command
        e.call(0x800599EE, 0)
        return e.u32(XPORT + 4) & 1
    e, usb = linked_emu()
    ok(e.read(0x80059A3C, 4) == bytes.fromhex("004b1847"), "start hook at 0x80059A3C")
    ok(fw_start(e, usb, 0) == 1, "firmware start: playing bit set as stock")
    ok(e.u32(XSTART) == 0 and e.u32(XSTART + 4) == e.tm & 0xFFFFFFFF, "firmware start: start record as stock")
    ok(usb.packets == [FA, F8] and e.u32(SYM["um_hook_starts"]) == 1,
       "Start + clock 0 sent inside the firmware's transport start")
    e, usb = linked_emu()
    fw_start(e, usb, 4000)
    s16 = -(-4000 * 1200 * 24 // (6 * 600000))
    ok(usb.packets[:2] == [bytes([0x03, 0xF2, s16 & 0x7F, s16 >> 7]), FB], "hook from 4000 ms: Song Position + Continue")
    e, usb = linked_emu()
    fw_start(e, usb, 0, on=0)
    ok(usb.packets == [] and e.u32(SYM["um_hook_starts"]) == 0, "USB MIDI Off: the hook does nothing")
    e, usb = linked_emu()
    w32(e, SYM["um_busy"], 1)                          # the task is inside the clock
    fw_start(e, usb, 0)
    ok(usb.packets == [] and e.u32(SYM["um_kick"]) == 1, "clock busy in the task: left to it (kick), no race")
    w32(e, SYM["um_busy"], 0)
    step(e)
    ok(usb.packets == [FA, F8], "the task then sends Start + clock 0")

    # ── clock from the middle ──
    e, usb = linked_emu()
    start = 2300                                      # ms
    times, _ = run(e, usb, start, 1000)
    s16 = -(-start * 1200 * 24 // (6 * 600000))       # next 16th: 125 ms each at 120 BPM
    ok(s16 == 19, "2300 ms -> 16th #19")
    ok(usb.packets[0] == bytes([0x03, 0xF2, s16 & 0x7F, s16 >> 7]), f"Song Position {s16}")
    ok(usb.packets[1] == FB, "then Continue")
    first = s16 * 6 * unit
    ok(0 <= times[0] - first < 1, f"first clock on the 16th ({times[0]} vs {first})")
    ok(all(0 <= t - (first + k_ * unit) < 1 for k_, t in enumerate(times)), "on the grid after it")

    # ── locate while playing: a new start record ──
    n = len(usb.packets)
    play(e, 10000)
    step(e)
    s16 = -(-10000 * 1200 * 24 // (6 * 600000))
    ok(usb.packets[n:n + 3] == [FC, bytes([0x03, 0xF2, s16 & 0x7F, s16 >> 7]), FB],
       "locate -> Stop, Song Position, Continue")

    # ── Start vs Continue from the start record ──
    e, usb = linked_emu()
    play(e, 0)
    e.tm += 2 * 64 * 44100 // 1000                     # task first sees play 2 ms late
    step(e)
    ok(usb.packets[:2] == [FA, F8], "play from 00:00 seen 2 ms late -> still Start + clock 0")
    transport(e, False)
    step(e)
    play(e, 0, at=e.tm + 5000000)                      # REW to zero, PLAY again
    n0 = len(usb.packets) - 1
    step(e)
    ok(usb.packets[n0:n0 + 3] == [FC, FA, F8], "stop, back to 00:00, play -> Start again")
    play(e, 3000, at=e.tm + 7000)                      # FWD while playing, never seen stopped
    n1 = len(usb.packets)
    step(e)
    s16 = -(-3000 * 1200 * 24 // (6 * 600000))
    ok(usb.packets[n1:n1 + 3] == [FC, bytes([0x03, 0xF2, s16 & 0x7F, s16 >> 7]), FB],
       "restart seen only through the start record -> Stop, Song Position, Continue")
    play(e, 0, at=e.tm + 9000)                         # REW to 00:00 while playing
    n2 = len(usb.packets)
    step(e)
    ok(usb.packets[n2:n2 + 3] == [FC, FA, F8], "back to 00:00 while playing -> Stop, Start")

    # ── offset ──
    e, usb = linked_emu()
    w32(e, SYM["um_offset"], 5)
    times, _ = run(e, usb, 0, 1000)
    ok(usb.packets[0] == FA and times[0] == 0, "offset: Start and clock 0 at once")
    ok(all(0 <= t - max(0, k_ * unit - 5) < 1 for k_, t in enumerate(times)), "offset 5 ms: every later clock 5 ms early")
    e, usb = linked_emu()
    w32(e, SYM["um_offset"], 50)                       # 50 ms: 2.4 clocks early at 120 BPM
    times, _ = run(e, usb, 0, 1000)
    ok(times[:3] == [0, 0, 0] and all(0 <= t - (k_ * unit - 50) < 1 for k_, t in enumerate(times) if k_ >= 3),
       "offset 50 ms: clocks 0-2 at the Start, then 50 ms early")
    e, usb = linked_emu()
    times, _ = run(e, usb, 0, 500)
    w32(e, SYM["um_offset"], 8)
    more, _ = run(e, usb, 501, 500, fresh=False)
    first_k = round(times[-1] / unit) + 1
    ok(all(0 <= t - (k_ * unit - 8) < 1 for k_, t in enumerate(more, first_k)), "offset changed while playing: applied at once")

    # ── tempo / rate ──
    e, usb = linked_emu()
    times, last = run(e, usb, 0, 2000, tempo=600)
    ok(len(times) == 49, "60 BPM: 24 clocks per second")
    e, usb = linked_emu()
    times, last = run(e, usb, 0, 1000, tempo=1500)
    ok(len(times) == 61, "150 BPM: 60 clocks per second")
    e, usb = linked_emu()
    times, last = run(e, usb, 0, 1000, fs=48000)
    ok(len(times) == 49, "48 kHz project: same clock (timer counts 64 * 48000)")
    e, usb = linked_emu()
    times, last = run(e, usb, 0, 1000, tempo=1200, dt=10)
    ok(len(times) == 49, "coarse steps (10 ms): same count, never lost")

    # ── no link: steps still run, nothing is sent ──
    e = r24emu.R24Emu(BUILT)
    usb = Usb(e)
    timer_emu(e)
    play(e, 0)
    step(e)
    ok(usb.xfers == 0 and e.u32(SYM["um_n"]) == 0, "no pipe: dropped, nothing sent")

    # ── task ──
    def task_emu(setting, sync_rec, port_dir=0, max_delays=40, attach=True, off_at=None, sync_at=None):
        e = r24emu.R24Emu(BUILT)
        usb = Usb(e)
        log = []
        w32(e, UM_SETTING, setting)
        w32(e, SYNC_REC, sync_rec)
        e.write(PORT, bytes([1, port_dir, 1 if port_dir else 0]))
        w32(e, USBD_RUN, 1 if port_dir == 2 else 0)
        timer_emu(e)
        transport(e, False)
        delays = []

        def delay(e, r):
            delays.append(r[0])
            if off_at is not None and len(delays) == off_at:
                w32(e, UM_SETTING, 0)
            if sync_at is not None and len(delays) == sync_at:
                w32(e, SYNC_REC, 1)          # SYNC REC Master: same (2, 2) mode
            if len(delays) >= max_delays:
                e.uc.emu_stop()
            return 0

        def mode(e, r):
            log.append(("mode", r[0], r[1]))
            e.write(PORT, bytes([1, r[1], r[0]]))
            w32(e, USBD_RUN, 1)
            return 0

        def off(e, r):
            log.append(("off",))
            e.write(PORT, bytes([1, 0, 0]))
            w32(e, USBD_RUN, 0)
            return 0

        def register(e, r):
            log.append(("register", r[0]))
            if attach:                      # the stack offers the interface (attach tested above)
                w32(e, SYM["um_iface"], ms_if)
            return 0

        def open_pipe(e, r):
            sp = e.uc.reg_read(UC_ARM_REG_SP)
            out = e.u32(sp + 8)
            log.append(("open", r[0], r[1], r[2], r[3], e.u32(sp), e.u32(sp + 4)))
            w32(e, out, 0x920A0000)
            return 0

        ms_if = e.alloc(bytes(0x11) + bytes([3]) + bytes(0x2E))
        e.stub(DELAY, delay)
        e.stub(USB_MODE, mode)
        e.stub(USB_OFF, off)
        e.stub(REGISTER, register)
        e.stub(OPEN_PIPE, open_pipe)
        e.call(SYM["um_task"])
        return e, usb, log, delays, ms_if

    e, usb, log, delays, ms_if = task_emu(1, 0)
    ok(delays[0] == 3000, "waits 3 s after boot")
    ok(log[0] == ("mode", 2, 2), "port off + setting On -> host mode (2, 2) = Cstm, never mass storage (remounts the SD)")
    ok(delays[1:11] == [100] * 10, "only after the port stayed off ~1 s")
    ok(log[1] == ("register", SYM["um_driver"]), "driver registered once the host stack runs")
    ok(log[2] == ("open", ms_if, 0, 0, 2, 0, 0), "bulk OUT pipe of the claimed interface")
    ok(sum(1 for x in log if x[0] == "register") == 1, "registered once")
    ok(e.u32(SYM["um_pipe"]) == 0x920A0000 and e.u32(SYM["um_state"]) == 3, "linked")
    ok(e.u32(SYM["um_own"]) == 1, "owns the port")
    ok(delays[-1] == 1, "1-tick loop while linked")

    e, usb, log, delays, _ = task_emu(0, 0)
    ok(log == [], "setting Off: port untouched")
    e, usb, log, delays, _ = task_emu(1, 1)
    ok(log == [], "SYNC REC Master: port untouched")
    e, usb, log, delays, _ = task_emu(1, 0, port_dir=1)
    ok(log == [], "port in device mode (USB menu): left alone")

    # setting On -> link -> Off: the task gives the port back
    e, usb, log, delays, _ = task_emu(1, 0, max_delays=30, off_at=20)
    ok(log[-1] == ("off",) and e.u32(SYM["um_own"]) == 0, "setting Off after taking the port -> USB off")
    ok(e.u32(SYM["um_pipe"]) == 0 and e.u32(SYM["um_state"]) == 0, "link forgotten")

    # SYNC REC -> Master while we hold the port: it needs the same mode, keep it on
    e, usb, log, delays, _ = task_emu(1, 0, max_delays=30, sync_at=20)
    ok(("off",) not in log and e.u32(SYM["um_own"]) == 0, "SYNC REC Master takes over: port left on")

    # ── MIDI SYNC = In ──
    midi_in_checks(task_emu)

    # ── boot ──
    e = r24emu.R24Emu(BUILT)
    made = []
    e.stub(TASK_CREATE, lambda e, r: made.append(e.read(r[0], 24)) or 7)
    e.stub(TASK_START, lambda e, r: made.append(r[0]) or 0)
    e.call(SYM["um_boot"])
    d = made[0]
    entry, prio, size, stack = struct.unpack_from("<IHxxII", d, 8)
    ok(entry == SYM["um_task"] | 1 and prio == 0x39, "task: entry, priority 0x39")
    ok(stack == SYM["um_stack"] and size == 4096, "task: 4 KiB stack of its own")
    ok(made[1] == 7 and e.u32(SYM["um_task_id"]) == 7, "task started")

    # ── MIDI CALIBRATE ──
    calibrate_checks()

    print(f"OK: {checks} checks")
    return 0


# ── MIDI SYNC = In: Start / Continue / Stop -> the R24's keys ──
UI_POST, LOCATE = 0x80003C00, 0x8001F8C0
EV_STOP, EV_PLAY = 0x10032, 0x10033


def in_emu(playing=False, offset=0, tempo=1200):
    e = r24emu.R24Emu(BUILT)
    acts = []
    transport(e, playing, tempo=tempo)
    w32(e, SYM["um_offset"], offset)

    def post(e, r):
        acts.append(("key", r[0]))
        if r[0] == EV_STOP:
            w32(e, XPORT + 4, 0)          # the UI stops the transport
        return 0
    e.stub(UI_POST, post)
    e.stub(LOCATE, lambda e, r: acts.append(("locate", r[0], r[1])) or 0)
    e.stub(DELAY, lambda e, r: acts.append(("delay", r[0])) or 0)
    return e, acts


def feed(e, data, t=0):
    buf = e.alloc(data)
    e.call(SYM["um_in_parse"], buf, len(data), t & 0xFFFFFFFF)


SET_TEMPO, PRJ_TEMPO = 0x80004FEA, 0x804C44C0 + 0x20


def tempo_run(bpm, n, prj=1200, playing=False, jitter_ms=0.0, t0=0x10000000, seed=1):
    """n clocks at `bpm` (timer 64*44100/s, optional +-jitter at arrival);
    -> (tempo sets, measured BPM x 100)"""
    import random
    rnd = random.Random(seed)
    e, acts = in_emu(playing=playing)
    w32(e, PRJ_TEMPO, prj)
    sets = []
    e.stub(SET_TEMPO, lambda e, r: sets.append((r[0], e.u32(PRJ_TEMPO), clk[0])) or 0)
    k = 64 * 44100
    per = k * 60 / bpm / 24
    clk = [0]
    for i in range(n):
        clk[0] = i + 1
        t = t0 + i * per + rnd.uniform(-jitter_ms, jitter_ms) * k / 1000
        feed(e, F8, int(t))
    return sets, e.u32(SYM["um_in_bpm100"])


def midi_in_checks(task_emu):
    e, acts = in_emu()
    feed(e, FA)
    ok(acts == [("locate", 0, 0), ("key", EV_PLAY)], "Start: locate 00:00, PLAY key")
    e, acts = in_emu(offset=17)
    feed(e, FA)
    ok(acts == [("locate", 17, 0), ("key", EV_PLAY)], "Start: locate to MIDI OFFSET ms")
    e, acts = in_emu(playing=True)
    feed(e, FA)
    ok(acts == [("key", EV_STOP), ("locate", 0, 0), ("key", EV_PLAY)], "Start while playing: STOP first")
    e, acts = in_emu(playing=True)
    feed(e, FC)
    ok(acts == [("key", EV_STOP)], "Stop while playing: STOP key")
    e, acts = in_emu()
    feed(e, FC)
    ok(acts == [], "Stop while stopped: nothing (does not cancel REC standby)")
    e, acts = in_emu()
    feed(e, FB)
    ok(acts == [("key", EV_PLAY)], "Continue: PLAY from where it is")
    e, acts = in_emu(playing=True)
    feed(e, FB)
    ok(acts == [], "Continue while playing: nothing")
    e, acts = in_emu(tempo=1200)
    feed(e, bytes([0x03, 0xF2, 16, 0]) + FB)          # 16 16ths = 1 bar at 120 = 2000 ms
    ok(acts == [("locate", 2000, 0), ("key", EV_PLAY)], "Song Position + Continue: locate there, PLAY")
    e, acts = in_emu()
    feed(e, F8 * 6 + bytes(8) + bytes([0x09, 0x90, 60, 100]))
    ok(acts == [] and e.u32(SYM["um_in_pkts"]) == 7, "clock, padding and notes: ignored")
    e, acts = in_emu()
    feed(e, F8 + bytes([0x1F, 0xFA, 0, 0]))
    ok(acts[-1] == ("key", EV_PLAY), "any cable")

    # tempo from the clock
    sets, m = tempo_run(125.0, 96)
    ok(sets == [] and m == 0, "tempo: nothing before a full 4-beat window")
    sets, m = tempo_run(125.0, 97 + 30)
    ok(m == 12500 and sets and sets[0][:2] == (1250, 1250), "tempo: 125.0 measured, project tempo set (stopped)")
    ok(sets[0][2] == 97 + 23, "tempo: stopped -> after 24 steady clocks")
    ok(len(sets) == 1, "tempo: set once")
    sets, m = tempo_run(125.0, 97 + 60, playing=True)
    ok(sets and sets[0][2] == 97 + 47, "tempo: playing -> after 48 steady clocks (2 beats)")
    sets, m = tempo_run(120.0, 400, jitter_ms=1.0)
    ok(sets == [], "tempo: 120.0 with +-1 ms USB jitter does not touch a 120.0 project")
    sets, m = tempo_run(97.3, 400, jitter_ms=1.0)
    ok(sets and sets[-1][0] == 973 and len(sets) == 1, "tempo: 97.3 with jitter -> 97.3 once")
    sets, m = tempo_run(120.0, 400, t0=0xFFFF0000)
    ok(sets == [] and m == 12000, "tempo: timer wrap")
    sets, m = tempo_run(300.0, 300)
    ok(sets == [], "tempo: outside 40..250 ignored")

    # the task in In mode: IN pipe, blocking reads that feed the parser
    reads = []

    def xfer(e, r):
        pipe, buf, n, res = r
        sp = e.uc.reg_read(UC_ARM_REG_SP)
        reads.append((pipe, n, e.u32(sp), e.u32(sp + 4)))
        data = FA if len(reads) == 1 else b""
        e.write(buf, data)
        w32(e, res, 0)
        w32(e, res + 4, len(data))
        return 0
    posted = []
    import r24emu as _r
    orig = _r.R24Emu.call

    def hooked(self, *a, **k):                         # add the stubs before the task runs
        if not getattr(self, "_in_stubs", False):
            self._in_stubs = True
            self.stub(XFER, xfer)
            self.stub(UI_POST, lambda e, r: posted.append(r[0]) or 0)
            self.stub(LOCATE, lambda e, r: posted.append(("locate", r[0])) or 0)
        return orig(self, *a, **k)
    _r.R24Emu.call = hooked
    try:
        e, usb, log, delays, ms_if = task_emu(2, 0, max_delays=20)
    finally:
        _r.R24Emu.call = orig
    opens = [x for x in log if x[0] == "open"]
    ok(opens and opens[0] == ("open", ms_if, 0, 0x80, 2, 0, 0), "In: bulk IN pipe of the claimed interface")
    ok(not any(x[3] == 0 for x in opens), "In: no OUT pipe")
    ok(e.u32(SYM["um_pipe_in"]) == 0x920A0000 and e.u32(SYM["um_state"]) == 3, "In: linked")
    ok(reads and reads[0][1] == 64 and reads[0][2] == 100 and reads[0][3] == 0x2000,
       "In: 64-byte reads, 100 ms timeout, short packets allowed (flags 0x2000, as MSC)")
    ok(posted[:2] == [("locate", 0), EV_PLAY], "In: a Start read from the cable -> locate + PLAY")
    ok(usb.xfers == 0, "In: nothing sent")


# ── MIDI CALIBRATE: the device analysis vs the same algorithm in Python ──
FW = {"fopen": 0x80054E54, "busy_guard": 0x80005CA2, "browse": 0x800423C4,
      "progress": 0x8003D9DA, "progress_end": 0x8003DA74, "message": 0x8003D32E,
      "wait_event": 0x800033E0, "set_screen": 0x80039150, "ui_enter": 0x8003A240,
      "ui_leave": 0x8003A24E, "edit_reset": 0x8003C71E, "edit_active": 0x8003C4E6,
      "gfx_lock": 0x800321A0, "gfx_unlock": 0x80032176, "gfx_clear": 0x800338E2,
      "gfx_title": 0x8003716E, "gfx_text": 0x80037142}
CUR_PROJECT, CUR_PROJECT_NAME = 0x804C6E14, 0x804C6E24


def wav_frames(data):
    """-> (fs, bps, int16 array [frames, channels], top 16 bits) of a PCM WAV"""
    import numpy as np
    o, fs, bps, nch = 12, 0, 0, 0
    while o + 8 <= len(data):
        cid, sz = data[o:o + 4], struct.unpack_from("<I", data, o + 4)[0]
        if cid == b"fmt ":
            nch, fs = struct.unpack_from("<HI", data, o + 10)
            bps = struct.unpack_from("<H", data, o + 22)[0] // 8
        if cid == b"data":
            raw = data[o + 8:o + 8 + sz]
            a = np.frombuffer(raw[:len(raw) // (nch * bps) * nch * bps], dtype=np.uint8)
            a = a.reshape(-1, nch, bps)
            top = (a[:, :, bps - 1].astype(np.int32) << 8) | a[:, :, bps - 2]
            return fs, bps, np.where(top >= 0x8000, top - 0x10000, top)
        o += 8 + sz + (sz & 1)
    raise ValueError("no data")


def ref_cal(data, tempo10=1200):
    """the patch's algorithm in Python: -> (first hit 1/100 ms, jitter 1/100 ms or -1, hits)"""
    import numpy as np
    fs, _bps, x = wav_frames(data)
    w = fs // 1000
    v = np.abs(x[:, 0]).astype(np.int64)
    cs = np.concatenate([[0], np.cumsum(v)])
    t = np.arange(len(v))
    env = cs[t + 1] - cs[np.maximum(t + 1 - w, 0)]
    thr = int(env.max()) // 4
    prev = np.concatenate([[0], env[:-1]])
    cross = np.flatnonzero((env > thr) & (prev <= thr))
    on, last = [], -0x40000000
    lim = min(fs // 100, 511)
    for i in cross:
        if i - last > fs * 60 // 1000:
            j = 0
            while j < lim and j < i and env[i - j] > thr // 10:
                j += 1
            on.append(int(i) - j)
            last = int(i)
    lag = on[0] * 100000 // fs
    jit = -1
    if len(on) >= 4:
        g = 150 * fs
        res = []
        for i, o in enumerate(on[1:]):
            r = (o * tempo10) % g
            if i == 0:
                r0 = r
            r -= r0
            if r > g // 2: r -= g
            if r < -(g // 2): r += g
            res.append(r)
        lo, hi = min(min(res), 0), max(max(res), 0)
        jit = (hi - lo) * 100000 // tempo10 // fs
    return lag, jit, len(on)


def make_mono(first_ms, fs=44100, bps=2, n=16, period=0.5, jitter_ms=0.0, nch=1):
    """synth take from 00:00: hits on the beat grid, the first at first_ms"""
    import numpy as np
    total = int(fs * (n * period + 0.5))
    x = np.zeros(total)
    rng = np.random.default_rng(3)
    for k in range(n):
        j = 0 if k == 0 else rng.uniform(-jitter_ms / 2, jitter_ms / 2)
        s0 = int(round((k * period * 1000 + first_ms + j) * fs / 1000))
        ln = int(0.03 * fs)
        env = np.exp(-np.arange(ln) / (0.006 * fs))
        x[s0:s0 + ln] += 0.6 * env * np.sin(2 * np.pi * 300 * np.arange(ln) / fs)
    x += 0.002 * rng.standard_normal(x.shape)
    xs = np.stack([x] * nch, axis=1)
    q = np.clip(np.round(xs * (2 ** (8 * bps - 1) - 1)), -(2 ** (8 * bps - 1)),
                2 ** (8 * bps - 1) - 1).astype(np.int64)
    raw = bytearray()
    for v in q.reshape(-1):
        raw += int(v & ((1 << (8 * bps)) - 1)).to_bytes(bps, "little")
    fmt = struct.pack("<HHIIHH", 1, nch, fs, fs * nch * bps, nch * bps, 8 * bps)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    body += b"LIST" + struct.pack("<I", 1000) + bytes(1000)          # as the R24's
    body += b"data" + struct.pack("<I", len(raw)) + bytes(raw)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def mono_of(data, ch):
    """one channel of a stereo 16-bit WAV as a mono WAV"""
    import numpy as np
    fs, _bps, x = wav_frames(data)
    raw = x[:, ch].astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, 1, fs, fs * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(raw)) + raw
    return b"RIFF" + struct.pack("<I", len(body)) + body


def calibrate_checks():
    path = "A:\\ZOOM_R24\\PROJECT\\PRJ000\\AUDIO\\MONO-001.WAV"

    def analyze(data, tempo10=1200):
        e = r24emu.R24Emu(BUILT)
        e.files[path] = data
        rc = e.call(SYM["um_cal_analyze"], e.alloc(path.encode() + b"\0"), tempo10, limit=2_000_000_000)
        lag, jit, hits = struct.unpack("<iii", e.read(SYM["um_cal_lag"], 4) + e.read(SYM["um_cal_jitter"], 4)
                                       + e.read(SYM["um_cal_hits"], 4))
        return rc, lag, jit, hits

    cases = [("synthetic 16-bit, first hit 6.30 ms, steady", make_mono(6.3), 630, (0, 10)),
             ("synthetic 24-bit 48 kHz, first hit 2.00 ms", make_mono(2.0, fs=48000, bps=3), 200, (0, 10)),
             ("synthetic, first hit 9.00 ms, jitter 1.0 ms", make_mono(9.0, jitter_ms=1.0), 900, (60, 105))]
    ste = os.path.join(ROOT, "STE-001.WAV")
    if os.path.exists(ste):
        # the synth AND the R24's own pattern (R) are ~2 ms p-p off a perfect grid: the
        # R24's pattern itself is not on one; the synth followed it within 0.1 ms
        # a real take (whatever is in it now): only device == reference
        cases.append(("STE-001.WAV L as a mono take", mono_of(open(ste, "rb").read(), 0), None, None))
        cases.append(("STE-001.WAV R as a mono take", mono_of(open(ste, "rb").read(), 1), None, None))
    for what, data, want, jrange in cases:
        rc, lag, jit, hits = analyze(data)
        rlag, rjit, rhits = ref_cal(data)
        ok(rc == 0 and (lag, jit, hits) == (rlag, rjit, rhits),
           f"{what}: device {lag / 100:.2f} ms, jitter {jit / 100:.2f}, {hits} hits == reference")
        if want is not None:
            ok(abs(lag - want) <= 15, f"{what}: first hit found ({lag / 100:.2f} ms)")
        if jrange is not None:
            ok(jrange[0] <= jit <= jrange[1], f"{what}: jitter {jit / 100:.2f} ms in range")

    rc, *_ = analyze(make_mono(5.0, nch=2))
    ok(rc == -2, "stereo take -> -2 (Need Mono WAV)")
    e = r24emu.R24Emu(BUILT)
    ok(e.call(SYM["um_cal_analyze"], e.alloc(b"A:\\NOPE.WAV\0"), 1200) == -1, "missing file -> -1")
    rc, lag, jit, hits = analyze(make_mono(30.0, n=1))
    ok(rc == 0 and hits == 1 and jit == -1, "a single hit: no jitter figure")

    for lag, want in ((497, 5), (449, 4), (450, 5), (-30, 0), (2600, 26), (4949, 49), (6000, 50), (0, 0)):
        ok(e.call(SYM["um_cal_offset"], lag & 0xFFFFFFFF) == want, f"offset for {lag / 100} ms -> {want}")

    # the whole menu action: browse the project's AUDIO, analyse, ENTER sets the offset
    e = r24emu.R24Emu(BUILT)
    e.files[path] = make_mono(6.3)
    w32(e, CUR_PROJECT, 0)
    e.write(CUR_PROJECT_NAME, b"PRJ000\0")
    w32(e, TEMPO, 1200)
    w32(e, SYM["um_offset"], 2)
    seen = {"msg": [], "browse": None, "texts": []}

    def browse(e, r):
        seen["browse"] = (e.cstring(r[0]), e.cstring(r[2]))
        e.write(r[1], path.encode() + b"\0")
        return 0
    e.stub(FW["busy_guard"], lambda e, r: 0)
    e.stub(FW["browse"], browse)
    e.stub(FW["message"], lambda e, r: seen["msg"].append(e.cstring(r[0])) or 0)
    e.stub(FW["gfx_text"], lambda e, r: seen["texts"].append(e.cstring(r[0])) or 0)
    e.stub(FW["wait_event"], lambda e, r: 0x00010024)            # ENTER
    e.stub(FW["edit_active"], lambda e, r: 0)
    for k in ("progress", "progress_end", "set_screen", "ui_enter", "ui_leave", "edit_reset",
              "gfx_lock", "gfx_unlock", "gfx_clear", "gfx_title"):
        e.stub(FW[k], lambda e, r: 0)
    e.call(SYM["um_calibrate"], limit=2_000_000_000)
    ok(seen["browse"] == ("A:\\ZOOM_R24\\PROJECT\\PRJ000\\AUDIO",) * 2, "browser opens at the project's AUDIO")
    ok("FIRST HIT" in seen["texts"] and any(x.startswith("6.") for x in seen["texts"])
       and "2 > 6 ms" in seen["texts"], f"result screen: {seen['texts'][:8]}")
    ok(e.u32(SYM["um_offset"]) == 6 and seen["msg"] == ["MIDI OFFSET 6 ms"],
       "ENTER: MIDI OFFSET = the silence (6 ms), not current + rest")


if __name__ == "__main__":
    raise SystemExit(main())
