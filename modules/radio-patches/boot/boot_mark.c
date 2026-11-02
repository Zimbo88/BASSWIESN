/* Authored, freestanding pixel compositor. No I/O, daemon or firmware installer.
 * Caller must supply the exact known 128x100 gray8 frame and own its lifetime.
 * Only logo pixels are set; native boot status and all other bytes are retained.
 */
#include "boot_mask.h"

int basswiesn_boot_mark(unsigned char *frame, unsigned size) {
    if (!frame || size != 12800u) return -1;
    for (unsigned i=0; i < sizeof(mark_pixels)/sizeof(mark_pixels[0]); ++i)
        frame[mark_pixels[i]] = 255;
    return 0;
}
