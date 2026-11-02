.syntax unified
.thumb
.text
.align 2
.global bw_original_selection
.thumb_func
bw_original_selection:
 /* Exactly the displaced, non-PC-relative instructions. The continuation
  * retains the original stack frame, exception handling and return path. */
 push {r4,r5,r6,r7,lr}
 sub sp, #76
 ldr.w pc, [pc, #0]
 .word 0x00459a21
.align 2
.global bw_original_zone
.thumb_func
bw_original_zone:
 /* ZoneHandler entry: these displaced instructions are not PC-relative. */
 push {r4,r5,r7,lr}
 sub sp, #8
 ldr.w pc, [pc, #0]
 .word 0x00454c69
.section .note.GNU-stack,"",%progbits
