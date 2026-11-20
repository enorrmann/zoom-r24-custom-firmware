/* kit_assign.h — what sdk/patches/kit_assign.c offers to other patches. */
#ifndef KIT_ASSIGN_H
#define KIT_ASSIGN_H

/* "KICK", "SNARE", ... for the 16 pads */
extern const char *const ka_pad_name[];
/* copy A:\ZOOM_R24\PROJECT\<prj>\AUDIO\<name> to KIT\<name>, record the pad
 * in <prj>\KIT.TXT, reload the CUSTOM kit; returns "Complete!" or an error
 * text. Shows the firmware's "Copying..." box while copying. */
const char *ka_assign(const char *name, int slot);

#endif
