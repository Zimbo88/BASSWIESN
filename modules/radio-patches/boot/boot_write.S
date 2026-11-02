/* Emulator-only wrapper at the original framebuffer write call site.
 * Keep fd, buffer, count, stack alignment, and original write return behavior.
 * No new file opens, syscalls, thread, timer, or radio control path.
 */
.syntax unified
.thumb
.section .start,"ax",%progbits
.global boot_write
.type boot_write,%function
.thumb_func
boot_write:
    push {r0,r1,r2,lr}
    mov r0,r1
    mov r1,r2
    bl basswiesn_boot_mark
    pop {r0,r1,r2,lr}
    movw r3,#0x8578
    bx r3
.size boot_write,.-boot_write
.section .note.GNU-stack,"",%progbits
