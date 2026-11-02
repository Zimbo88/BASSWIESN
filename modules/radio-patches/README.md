# Radio display and boot payload sources

Authored code for the private standard presentation and separate LAB patches. See
[Radio patches](../../docs/RADIO_PATCHES.md) for compatibility, limitations and
independent rollback ownership.

`native/` contains the bounded display state model, exact-image ARM bridge,
text layout, trampoline and linker script. `boot/` contains the authored
lettering mask and bounded wrapper for the original progress writer.

These are **not** generic shared libraries and are not installable by copying
them to a radio. Addresses and ABI assumptions apply only to the inspected
27.0.6.46330.5043500 images and require exact input hashes and patch-site checks.
Unknown images must fail closed. The runtime linker and driver boundary need
separate verification. Never execute an arbitrary firmware image on the host.

Do not add full Bose executables, root filesystems, firmware archives or private
installation packages here. The compiler input is our code; firmware-derived
candidates and deployment evidence remain in private storage. The original
application's bytes outside the owned patch ranges must be preserved, including
existing unrelated modifications.
