ENTRY(bw_port_frame)
SECTIONS {
 . = 0x841000;
 .text : { *(.text*) *(.rodata*) }
 . = 0x800000;
 .data : { *(.data*) }
 .bss (NOLOAD) : { *(.bss*) *(COMMON) }
 ASSERT(SIZEOF(.data) == 0, "Initialized globals need an explicit file-backed data segment")
 ASSERT(. <= 0x801000, "Private display state exceeds its RW page")
 /DISCARD/ : { *(.comment) *(.ARM.attributes) *(.ARM.exidx*) *(.ARM.extab*) }
}
