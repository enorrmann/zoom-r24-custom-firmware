/* custom_kit.h — what sdk/patches/custom_kit.c offers to other patches. */
#ifndef CUSTOM_KIT_H
#define CUSTOM_KIT_H

#define CUST_SLOTS 16

/* reload the CUSTOM kit from the current project's KIT folder */
void cust_reload(void);
/* last load result of a slot (0..15): > 0 loaded, 0 default, < 0 error
 * (-1 not a WAV, -2 format, -3 no data, -4 no room, -5 empty, -6 no kit) */
int cust_slot_result(int slot);
/* "A:\ZOOM_R24\PROJECT\<prj>\KIT\SAMPLEnn.WAV" (slot 0..15) — older kits */
void cust_make_path(char *out, const char *prj, int slot);
/* "A:\ZOOM_R24\PROJECT\<prj>\KIT\<file>" */
void cust_file_path(char *out, const char *prj, const char *file);

/* PROJECT\<prj>\KIT.TXT ("01=KICK.WAV" lines) maps pads to files in KIT\.
 * cust_pad_file: the file of a pad ("" = none; "SAMPLEnn.WAV" for older kits).
 * cust_set_pad: assign a file already in KIT\ ("" clears), rewrite KIT.TXT,
 * delete the pad's previous file if no other pad uses it. 0 ok. Call
 * cust_reload() afterwards. */
const char *cust_pad_file(int slot);
int cust_set_pad(int slot, const char *file);

/* Per-pad settings of the CUSTOM kit, KIT.TXT "S01=level,pan,group[,pitch[,poly]]".
 * cust_get/set_settings take v[CUST_NSET] in the order below; set clamps to
 * cust_set_min/max and applies at once (live); cust_save_settings writes
 * KIT.TXT (0 ok). level 0..15, pan 0..126 (64 = centre), group 0..7 (0 =
 * none), pitch -24..+19 semitones (pad byte 5, drum_pitch.c;
 * above +19 the DSP reads outside the sample), poly 0..1
 * (0 = a new hit of the pad cuts the previous one: MONO bit 4 of byte 4). */
#define CUST_LEVEL_MAX 15
#define CUST_PAN_MAX   126
#define CUST_GROUP_MAX 7
#define CUST_PITCH_MIN (-24)
#define CUST_PITCH_MAX 19             /* DSP limit, device-verified: drum_pitch.c */
#define CUST_POLY_MAX  1
enum { CUST_LEVEL, CUST_PAN, CUST_GROUP, CUST_PITCH, CUST_POLY, CUST_NSET };
extern const int8_t cust_set_min[CUST_NSET], cust_set_max[CUST_NSET];
void cust_get_settings(int slot, int v[CUST_NSET]);
void cust_set_settings(int slot, const int v[CUST_NSET]);
int cust_save_settings(void);

#endif
