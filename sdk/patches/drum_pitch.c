/*
 * drum_pitch.c — per-pad pitch for the drum machine (test: pitch = mute group).
 *
 * Every drum voice is started by fw_dsp_voice (0x8006C9F6, ch 0..7; ch 8 is
 * the metronome click), which writes 11 words into the DSP mailbox slot
 * 0x11F07970 + ch * 0x40. Word 0 is the DSP's playback STEP in Q29: the
 * firmware sends 1.0 (0x20000000), or 44100/48000 = 0x1D666666 in a 48 kHz
 * project. The DSP voice loop (L2 0x11805568..0x11805668) adds it to a Q29
 * phase per output sample and interpolates linearly; nothing clamps it, the
 * integer part has 3 bits (step < ~3.0 is safe). So pitch needs no DSP change:
 * docs/19_drum_pitch.md.
 *
 * This patch re-implements fw_dsp_voice and, for the drum voices, multiplies
 * the step by 2^(n/12), n = drum_pitch_semi(ch) = -24..+19: the pad's PITCH,
 * byte 5 of the pad (struct r24_slot reserved[0], signed; never read by the
 * stock firmware, 0 in every factory kit), copied into the voice (+5) at
 * note-on. Set per pad in KIT SELECT > CUSTOM > SETTINGS (page 2), KIT.TXT
 * "Snn=level,pan,group,pitch". The sequencer will override it per step.
 */
#include "r24.h"

#define DP_MBOX   ((volatile uint32_t *)0x11F07970u)
#define DP_VOICES ((const volatile uint8_t *)0x80537FB4u)   /* 8 x 0x38 */
#define DP_VSIZE  0x38
#define DP_ONE    0x20000000u                              /* 1.0 in Q29 */

/* 2^(n/12) in Q16, n = -24..+24 (index n + 24) */
#define DP_SEMI_MAX 24
static const uint32_t dp_semi_q16[2 * DP_SEMI_MAX + 1] = {
    16384, 17358, 18390, 19484, 20643, 21870, 23170, 24548,
    26008, 27554, 29193, 30929, 32768, 34716, 36781, 38968,
    41285, 43740, 46341, 49097, 52016, 55109, 58386, 61858,
    65536, 69433, 73562, 77936, 82570, 87480, 92682, 98193,
    104032, 110218, 116772, 123715, 131072, 138866, 147123, 155872,
    165140, 174960, 185364, 196386, 208064, 220436, 233544, 247431,
    262144,
};

/* The DSP keeps the phase in Q29 with a 3-bit integer part and shifts it
 * arithmetically (L2 0x11805568..): step + fraction must stay below 4.0.
 * Device test 2026-11-19: above +19 st (step 2.997 at 44.1 kHz) the voice
 * reads outside the sample (garbage; +20..+22 sometimes play backwards — the
 * phase overflows negative). So every read is validated: the semitone is
 * clamped to -24..+19 and the final step to just under 3.0, whatever the
 * source (pad byte, later the sequencer). */
#define DP_SEMI_MIN (-24)
#define DP_SEMI_UP  19
#define DP_STEP_MAX 0x5FFF0000u

/* semitones for drum voice ch (0..7): the pad's PITCH byte, copied to voice+5 */
static int drum_pitch_semi(int ch)
{
    return (int8_t)DP_VOICES[ch * DP_VSIZE + 5];
}

uint32_t drum_pitch_step(uint32_t step, int semi)
{
    uint64_t r;
    if (!semi)
        return step;
    if (semi > DP_SEMI_UP) semi = DP_SEMI_UP;
    if (semi < DP_SEMI_MIN) semi = DP_SEMI_MIN;
    r = ((uint64_t)step * dp_semi_q16[semi + DP_SEMI_MAX]) >> 16;
    return r > DP_STEP_MAX ? DP_STEP_MAX : (uint32_t)r;
}

void dp_dsp_voice(int ch, const uint32_t *p);
R24_REPLACE(0x8006C9F6, "f3b591b00c00d14ffff7", dp_dsp_voice);

/* Same as the original (0x8006C9F6..0x8006CA7C), plus the pitch. */
void dp_dsp_voice(int ch, const uint32_t *p)
{
    uint32_t w[11];
    uint32_t step;

    if ((unsigned)ch >= 9)
        return;
    step = fw_audio_rate == 48000 ? 0x1D666666u : DP_ONE;
    if (ch == 8) {                          /* metronome: 6 words, no pitch */
        w[0] = step; w[1] = p[0]; w[2] = 0;
        w[3] = p[1]; w[4] = p[2]; w[5] = 0;
        fw_dsp_write(DP_MBOX + ch * 16, w, 6);
        return;
    }
    w[0] = drum_pitch_step(step, drum_pitch_semi(ch));
    w[1] = p[0]; w[2] = 0;
    w[3] = p[1]; w[4] = p[2]; w[5] = 0;
    w[6] = p[3]; w[7] = p[4]; w[8] = p[5]; w[9] = p[6]; w[10] = p[7];
    fw_dsp_write(DP_MBOX + ch * 16, w, 11);
}
