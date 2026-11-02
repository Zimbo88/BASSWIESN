ENTRY(boot_write)
SECTIONS {
    . = 0xa000;
    .text : { KEEP(*(.start)) *(.text*) }
    .rodata : { *(.rodata*) }
    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) *(.note*) }
}
