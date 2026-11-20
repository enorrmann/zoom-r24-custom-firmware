# 22 — SYSTEM > USB MIDI: clock and start/stop to a USB-MIDI cable

> Added 2026-11-20. SDK patch `sdk/patches/usb_midi.c`, in the default image
> (`tools/build_firmware.py`). Proven in the emulator
> (`tools/test_usb_midi.py`). **Not yet tested on a device.** The first device
> build (sha256 `a73e5bfa…`) did not boot; see Boot below.

## Use

Connect a class-compliant USB-MIDI interface to the R24's USB port (an OTG
adapter, mini-B → A female), e.g. a USB → DIN5 cable. Then **SYSTEM >
USB MIDI = On**. About one second later the port switches to host mode, the
one SYNC REC Master uses (it does not touch the SD card). When the recorder
plays, the R24 sends on cable 0 (the cable's DIN OUT):

| event | message |
|---|---|
| PLAY from 00:00 (start position < 10 ms) | Start `FA` |
| PLAY from elsewhere | Song Position `F2` (the next 16th note) + Continue `FB` |
| playing | Clock `F8`, 24 per quarter note, at the project tempo |
| FWD / REW / locate while playing | Stop `FC`, then Start (back at 00:00) or Song Position + Continue |
| STOP | Stop `FC` |

USB MIDI is **Off at every power-on** (like SYNC REC) and is never saved:
turn it On in SYSTEM each session. It sends nothing while stopped.

**SYSTEM > MIDI OFFSET** (0–50 ms, 0 at power-on, not saved): every clock
leaves that many ms early, to cancel the receiver's own latency. Changing it
while playing applies at once. The first clock after PLAY cannot leave before
PLAY, so the first beat keeps the latency. With an offset bigger than a clock
period, the clocks due before PLAY go out with the Start (e.g. 3 at 50 ms /
120 BPM).

**Start is sent from the transport start itself.** A detour at the end of
`0x800599EE` (`0x80059A3C`: after the DSP start command, the start record and
the playing bit) runs `um_on_start` in the transport task (priority 0x34).
Start (or Song Position + Continue) and clock 0 leave at that moment instead
of up to an OS tick later, when the task would poll. The transport task gets
there after its own pre-delay: PLAY starts at once (`0x8001F100`), REC
(`0x8001F7D0`) waits 16 ms first (5 ms with the SYNC REC flag `0x801EAA64`
bit `0x80`), via message code 1 (`delay, start`) / 3 (15 ms, start) to the
transport task (`0x80020964`). The clock state is shared with the patch's own
task: `um_busy` (set with IRQs off, `0x800BE75C` / `0x800BE770`) says who is
inside; if the hook finds it taken, it sets `um_kick` and the task sends at
once.

**Timing.** The clock follows the transport's hardware timer (`0x80000C80`,
64 × fs per second, the source of the published position, read without
touching `0x80059744`'s state). When a clock falls before the task's next
wake, the task busy-waits for its exact moment (at most 1.5 ms per clock).
An option MIDI TIMING once offered "R24" as well: following the whole-ms
position the firmware publishes (`0x81220A90+0x10`, refreshed per OS tick).
On the device (2026-11-20, stereo takes, L = synth, R = the R24's pattern,
offset 0) it measured worse, and was removed:

| | R24 (removed) | timer (kept) |
|---|---|---|
| L − R median | 18.68 ms | **16.53 ms** |
| std hit to hit | 0.94 ms | **0.33 ms** |
| synth quarter notes | 498.7–501.5 ms | **499.4–500.5 ms** |

With that synth, MIDI OFFSET ≈ 17 ms puts it on the R24's pattern. An
earlier take (first working build, the same synth) measured 4.97 ms; the
~12 ms difference is under investigation (`build/v1/R24SYS.BIN`).

**SYSTEM > MIDI CALIBRATE** measures the latency on a take:

1. With USB MIDI On, record the synth on a **mono** track **from 00:00**. The
   synth should play something with a clear attack on beat 1 (and on later
   beats, for the jitter figure).
2. SYSTEM > MIDI CALIBRATE opens the file browser in the project's `AUDIO`;
   choose the take.
3. The screen shows `FIRST HIT` (the silence before the first sound),
   `JITTER` (later hits, peak to peak around the 16th grid of the project
   tempo; `-` with fewer than 4 hits) and `OFFSET old > new`. ENTER sets
   MIDI OFFSET = the silence rounded to 1 ms (0..50).

The first clock leaves with the Start, and no offset can move it earlier, so
the silence before the first hit is the whole chain: clock, USB, cable, synth
and the R24's input. Analysis (`um_cal_analyze`): a 1 ms envelope,
threshold = max/4, an onset = rising crossing ≥ 60 ms after the previous one,
moved back to envelope < threshold/10 (≤ 10 ms). Mono 16- or 24-bit PCM, any
rate. `test_usb_midi.py` runs the device code on synthetic takes and on each
channel of `STE-001.WAV`, and compares it with the same algorithm in Python.
Messages: `Need Mono WAV`, `No Sound`, `File Access Error`.

Start vs Continue is decided from the transport's **start record**
`0x81220A80` (`[0]` start position in ms, `[1]` hardware timer at the start;
written at PLAY by `0x800599EE` and at a locate by `0x800595F0`), not from the
live position. The live position is already 1–2 ms on when the task first
sees PLAY, so an earlier build sent Continue for a play from 00:00 (a synth
that ignores Continue then stayed stopped). A change of the record while
playing (FWD/REW, even one never seen as a stop) restarts the clock.

* **SYNC REC** (Master/Slave) uses the USB port itself. While SYNC REC is not
  Off, USB MIDI leaves the port alone (Master runs the same host mode, so the
  port stays on).
* **USB menu** (AUDIO I/F, STORAGE): while the port is in device mode, USB
  MIDI waits. When the port is switched off again, USB MIDI takes it back
  after 1 s. The same happens after LOOP[USB] or IMPORT USB, which turn USB off
  at the end.
* A USB stick is **not** mounted while USB MIDI holds the port. LOOP[USB] and
  IMPORT USB still work: they switch to mass-storage host themselves (after
  closing the project), and USB MIDI takes the port back when they finish.
* Tested adapter descriptor: QinHeng **CH345** `1a86:752d`, full speed,
  Audio/MIDIStreaming, bulk OUT `0x02`, 32 bytes. Any interface with
  bInterfaceClass 1 / subclass 3 (MIDIStreaming) is taken.

## How (RAM addresses)

### USB host stack (`PUH_*`)

| what | where |
|---|---|
| port mode used | **`0x8005B066(2, 2)`**: host, "Cstm" app (`0x80023D10` → `0x8005C268`: Cstm task, `0x800817C6` host stack, Cstm driver for VID 0x1686 only). No file-system calls. **Never `(1, 2)`** (mass storage): its app task `0x800826AA` unmounts all volumes (`0x80000156`) and remounts the SD (`0x80000050`). LOOP[USB] closes the project first; doing it under an open project (first device build, 2026-11-20) made every later STOP ("Please Wait...", `0x80008A28` → `0x8005AA1C` reopens each track file) take seconds until power-off |
| port mode | `0x8005B066(kind, dir)`: dir 1 = device (kind 1 storage, 2 audio I/F, 3 SYNC REC slave), dir 2 = host (kind 1 mass storage → `0x8007A350`, kind 2 "Cstm" = SYNC REC master, VID 0x1686 PID 0x00DE). State `0x801EC650` = {on, dir, kind}. Off: `0x8005AFC6` (dir, kind ← 0) |
| host stack running | `0x801ED690 + 0x14` (set by `0x800817C6`, which the MSC app task runs) |
| class driver | 16 bytes `{u16 vid, pid; u8 dev_class, if_class, match, 0; attach(iface); detach(iface)}`. match bits: 1 vid, 2 pid, 4 device class, 8 interface class. Registered by `0x800818E0` (copied into a list node by `0x80094290`; existing unclaimed interfaces are probed at once by `0x80094170`). Matching per interface in `0x800943EA`; `attach` returns 0 to claim. Examples: MSC `0x801ED5F8` (if class 8), hub `0x801EDF0C`, Cstm `0x801EC658` |
| interface object | `+0x08` device, `+0x10` class, `+0x11` subclass, `+0x12` protocol, `+0x13` number, `+0x14` alternate (`0x8009360A`) |
| open pipe | `0x80081BAC(iface, 0, dir 0x00/0x80, type 2 bulk / 3 interrupt, 0, 0, &pipe)`, message `0x02000013` |
| transfer | sync `0x800819D6(pipe, buf, len, res[2], timeout, 0)` → async `0x80081952(pipe, buf, len, cb, arg, timeout, flags)`, message `0x02000007`; `res` = {status, actual}. The MSC CBW goes out this way (`0x8007C862`) |
| API context | every call is a message to the USBD task, and the synchronous ones wait for its reply (`0x80081898`). attach/detach run *in* that task, so they only record the interface; the patch's own task opens the pipe and sends |

### Transport and tempo

| what | where |
|---|---|
| transport | `0x81220A90`: `+4` bit 0 = playing (play `0x800599EE`, stop `0x8005A040`), `+8` fs, `+0x10` position in **milliseconds** (`0x80059744`: start + timer × 1000 / (64 × fs), the timer counting 64 × fs per second) |
| position update | `0x800597DA` (start pos + elapsed hardware timer `0x01C21410`, `0x80059744`), registered by `0x800598B0` into the cyclic-handler table `0x80419BB0` (`0x80001324`, run by `0x8000134C` every OS tick) |
| tempo | `0x801EB744` = BPM × 10 (rhythm engine; the bar/beat counter `0x8002D4EC` converts with it: `ticks = ms × tempo / 12500`, 48 ticks per beat) |

Clock: `acc += Δms × tempo × 24`, and one `F8` each time `acc` reaches
`600000` (20.83 ms at 120 BPM).

**The unit is ms, not samples.** The first build that reached a MIDI device
(sha256 `dac2b369…`) divided by `600 × fs` as if the position were in
samples, so its clock ran ~44× slow (2026-11-20). `test_usb_midi.py` now runs
`0x80059744` to pin the unit. Because the clock is accumulated, a tempo change bends it instead
of making it jump. The task runs every OS tick (`0x800547B8(1)`), so a clock
leaves at most one tick late. It never drifts, because it follows the
transport position.

### Menu and setting

| what | where |
|---|---|
| SYSTEM menu | header `0x800D4AE4` (count 8, items at `+0x20` = `0x800D4AC4`: LIGHT, CONTRAST, DATE/TIME, VERSION, SYNC REC, BATTERY, PAD SENSE, PHANTOM). Patched: count 9, array → `um_items` with USB MIDI last |
| item | 80-byte list item modelled on BATTERY `0x800D4A24`: 2 values, variable `um_on` (the patch's own, 0 at power-on), max 1, a change callback that does nothing. BATTERY's `0x80019B56` would set the settings-dirty flag `0x801EB418` and cause a NOR write on leaving SYSTEM |
| system settings | `0x804C6DD4`, 0x40 bytes, loaded from NOR `0x7D0000` at boot (`0x800228B8`), saved by `0x80022886` when SYSTEM is left with something changed (exit callback `0x80019AC2`). Used offsets: 4, 8, 0xC, 0x10, 0x14, 0x28, 0x38, 0x3C (SYNC REC, forced 0 on load and save). `+0x20` is unused (0 in the device dump `DUMP1`); USB MIDI does **not** use it (an earlier build did, which saved the setting) |

### Boot

A detour at `0x80008288` in the main init (`bl 0x8006C868`, which boots the
DSP; then `movs r5,#0 ; b 0x800082A2`) runs those instructions and
`um_boot`. That creates the task (`0x800545F8`, descriptor as the MSC app's:
entry, priority 0x39, 4 KiB stack of its own) and starts it (`0x80054678`).
The task waits 3 s before touching USB.

**The detour must come after `0x8000827E`** (`bl 0x8002D888`, the boot copy
of the 5 MiB bank to `0x87540000`, `07_pcm_samples.md` §2.1). All SDK code
lives inside that bank, so before the copy its RAM is empty. The first build
put the detour at `0x80008218` (right after the settings load), jumped into
nothing and **did not power on** (2026-11-20; the boot loader still worked).
`test_usb_midi.py` now checks the order.

## Debug

`build/R24SYS.BIN.syms.json` lists the patch variables. They can be read in
a DUMP 2 DDR dump. Timer values are in ticks of 64 × fs: 2.8224 per µs at
44.1 kHz.

| variable | meaning |
|---|---|
| `um_state` | 0 off, 1 waiting for the port, 2 host + driver registered, 3 pipe open |
| `um_mode_rc`, `um_reg_rc`, `um_pipe_rc` | return codes of host mode, register, open pipe (1 = never called) |
| `um_attaches`, `um_detaches` | MIDIStreaming interfaces seen |
| `um_sent`, `um_errors`, `um_last_status` | packets sent, failed transfers, last status |
| `um_task_id` | RTOS task id (−1 = not created) |
| `um_late_us`, `um_late_max_us` | lateness of the last clock / worst since PLAY, µs, against its exact time (in R24 timing: against the published position) |
| `um_xfer_ticks`, `um_xfer_max_ticks` | duration of one USB transfer (submit → done), timer ticks |
| `um_wake_ticks` | measured period of the task's 1-tick sleep, timer ticks |
| `um_spins` | clocks sent after a busy wait (EXACT) |
| `um_cal_lag`, `um_cal_jitter`, `um_cal_hits` | last calibration, 1/100 ms |
| `um_hook_starts`, `um_kick` | starts sent from the transport start hook; a start left to the task |

## Not proven / open

* On a device: host-mode power to the cable, enumeration of the CH345, and
  the bulk OUT transfer. LOOP[USB] already proves host mode and bulk
  transfers to a stick.
* Units of `0x800547B8` and of the transfer timeout are assumed to be ms.
  If the OS tick is longer, clock jitter grows to one tick, but the clock
  still does not drift.
* RHYTHM mode playback that does not run the recorder transport is not
  followed. Only the recorder's PLAY/STOP is.
* The 5-item SYSTEM menu variant (`0x800D4B28`, opened by `0x80019B5E`) is
  unchanged.
