# Standard radio presentation and patch safety (private LAB)

Display layout and BASSWIESN boot lettering are **optional** in the integrated
radio setup. The default **Normal** leaves firmware files and existing patches
unchanged. **With patches** adds the display/boot package only for exact qualified
profiles. Its software path is implemented; end-to-end hardware
acceptance of this new orchestration remains required. No public release is
being made. Opening the interface and application startup must not install a patch or
contact a radio to inspect it. Existing live installations are not silently
upgraded by this policy change.

The owner reports continued correct operation of the installed display,
boot lettering, language and standby clock, including normal display actions.
This is acceptance of existing installations by observation, not a fresh
automated setup, removal, battery-composition or recovery test. It does not
expand the exact-image allowlist or mark other models as hardware-verified.

The LAB patch selector saves independent **plans**, not installation receipts.
New plans default all components to **Keep unchanged**. Previously saved choices remain intact,
including removal plans needed for recovery. This independent selector remains
plan-only; the standard setup owns the actual presentation installation and
its explicit recovery action. A saved selection is never
reported as installed, hardware-tested or production-ready.

## Multiroom station prefix

The new display revision prefixes the station header with `[M] ` for the local
Bose Zone master and `[G] ` for a member. Standalone and not-yet-observed roles
have no prefix. Sender metadata, presets, track/artist fields and the centred,
raised clock are not rewritten. Moderation still shows the station once and a
large clock. Long station labels repeat the prefix on each display page.

Role comes from the existing local `ZoneStateInfo` display callback, not a
provider string shared by all group members. Invalid role bytes clear the
indicator. There is no new polling thread, file injector or networking. Apple
AirPlay grouping alone does not establish a Bose Zone role.

The authored ARM hook, state transitions, text paging and raster comparisons
are software-tested; the callback completion and final raster backend are
explicit emulator doubles. **This revision needs real-radio visual and
join/leave acceptance.** Boot-logo code and BatteryMonitor remain unchanged;
the separate known 28-byte application modification is preserved by composition.

Existing accepted display installations remain installed, without this new
prefix: updating BASSWIESN or choosing Normal does not update radio firmware.
Setup reports whether it installed the new revision or preserved an existing
one. A separate explicitly supervised patch upgrade is required for existing
radios; the setup must not claim the new indicator is present merely because an
older display patch was adopted.

## Integrated standard setup

The optional patched setup includes identity/protection checks, exact
image compatibility, verified off-device backups, non-overlapping composition,
cold activation and actual post-reboot verification of both display and boot
lettering. A plain routing success must not be re-labelled as verified standard
presentation. Unknown images must block patch installation, not receive an
approximate firmware match. **SoundTouch 10 has no display:** neither display nor
boot lettering is offered. Normal routing setup remains subject to its own exact
write-profile gate; omitting patches does not authorize an unsupported model.
The current setup profile catalogue has no ST10 write profile, so ST10 remains
blocked for setup until that independent path is qualified. Playback support or
an old feature-list statement is not a substitute for setup qualification.

The coordinator now executes this contract. The catalogue reports
`IMPLEMENTED_HARDWARE_ACCEPTANCE_REQUIRED`, not universal production support.
Previously supervised installs qualify the patch images, not this new complete
setup orchestration. The UI requires an explicit firmware-change/reboot
acknowledgement. Pinned root SSH must already be configured; there is no default
password, automatic host-key trust or generic command accepted from the browser.

The sequence is read-only presentation preflight, ordinary HTTP backup,
private firmware backup and hash validation, candidate composition, routing,
confirmed standalone/standby, cold activation, full reboot, stable boot-guard
readback, and display preference setup. Account/preset/audio verification follows.
Select **Normal** to skip all presentation firmware operations and SSH access.
Normal still backs up state, configures local routing, reboots and verifies
language and the native standby clock on display-equipped models. It preserves
provider display preferences and all existing firmware patches; it is **not** an
uninstaller. Patch removal remains an explicit recovery action. Unsupported
models, image hashes, startup hooks or missing pinned SSH stop before route writes.

Known current installations are adopted and verified after reboot without
rewriting their binaries. Fresh installations preserve original BoseApp bytes:
an exact-image candidate is bound read-only before application startup. Kernel,
bootloader and BatteryMonitor are not written. Two boot writers are replaced
with verified temporary files; the root filesystem is remounted read-only.
The native clock is enabled without replacing timezone/brightness attributes.
Each explicitly started setup also sets the radio language to the configured
BASSWIESN system language (`web_language`). Only established firmware language
IDs are used; unsupported UI languages fall back to English. The previous radio
language, including the uninitialized factory value, is backed up privately.
The requested language and standby-clock flag are read back from the radio;
a lost reply is never retried blindly. A mismatch blocks setup completion.
Names, presets, volume and existing battery modifications are not reset to
generic defaults. This is not a factory reset or an automatic startup action.
Provider metadata uses separate title/artist and does not append a duplicate clock.

Receipts and private firmware backups live under the configured data directory,
`setup-rebuild/<run>/<device>/baseline/presentation/`. They must remain private.
The distributed recipe contains authored payloads and short owned patch ranges,
not a complete vendor executable. No firmware compiler is needed on the Pi.
Every remote mutation has durable intent; a timeout never triggers a replay.
The offline backup is verified before the radio receives its backup receipt.

Explicit setup recovery restores preferences and removes **only this job's**
new display/boot installation, then verifies original hashes after full reboot.
An adopted pre-existing installation is retained. Independent LAB plans remain
available, but do not execute changes. Interrupted staging, an unknown startup
file, corrupt backup or pending unconfirmed boot requires manual reconciliation;
it is never called a successful rollback. Completed removal retains backups and
package files for inspection. Reinstallation after such removal requires review.
Routing/account recovery retains its existing documented partial-restoration
limitations: the entire radio is not claimed to be factory-restored.

Optional presentation does not enable the battery patch and does not
remove the recovery path. Display, boot writers and battery firmware still own
separate files and must remain independently reversible. The setup preview must
disclose firmware changes before explicit execution; ordinary discovery or
first page load must never trigger them.

## Components and ownership

| Option | Changes | Must preserve | Status |
| --- | --- | --- | --- |
| Display | Owned BoseApp display hooks, separate read-only cold-boot image | Existing non-display BoseApp changes, BatteryMonitor, presets, routing | LAB; private SCM/SM2 trials, with human acceptance and screenshot limitations recorded separately |
| Boot lettering | Both native `splash-write` executables | Native progress handling, battery/display choices, kernel and bootloader | LAB; lettering observed partway through boot, not confirmed on the earliest frame |
| Replacement battery | Existing exact-image BatteryMonitor patch | BoseApp and splash writers | Separate Portable-only LAB option; unnecessary for original batteries |
| Wi-Fi recovery | Not enabled | Working network configuration | Experimental; channel-change recovery not qualified |
| Long-press reboot | Not enabled | Existing power-button behavior | Experimental; not qualified |

LAB is a maturity label, not permission to bypass identity, protection, backup,
readback or firmware checks. Protected devices remain excluded by both address
and stable identity. A similar version number is insufficient for eligibility.

## Display behavior

During music playback the intended layout is station name, song title, artist,
and a centred clock. The accepted footer clock remains raised by five pixels
with its larger font. A new moderation layout handles absent song information:
when title repeats the station and no distinct artist exists, the station is
shown once and the real clock is larger in the middle. The bottom row is empty.
A real artist/title restores the normal music layout. No time or song is
invented when its source is missing. The native standby path is not changed by
this moderation rule.

The renderer operates only on recognized native display objects. It delegates
unsupported states/overlays to the existing renderer. Offline ARM instruction
tests use explicit raster/driver doubles: they are not full-device emulation,
audible-output evidence or a guarantee for every source/model.

### Current private acceptance

Cold-start application/API, exact installed hashes and unchanged presets/zone
were checked on one SoundTouch 30 SCM, two Portable SCM units and one
SoundTouch 20 SM2. The original BatteryMonitor remained unchanged on the
original-battery unit; existing custom battery/application changes remained
unchanged on the previously modified Portable. This verifies preservation,
not a new qualification of the historical battery modification.

The operator accepted the real music layout and the larger moderation clock,
including an ANTENNE BAYERN interval without song data. The feedback about
Portable displays did not identify each individual unit, so it must not be
turned into four independent visual approvals. The SM2 screenshot observation
remains inconclusive. No all-source, long-duration or first-boot-frame approval
is implied by these short checks.

For this layout, separate title/artist metadata is supplied rather than a single
composed title string. BASSWIESN's additional title-line clock is disabled to
avoid duplicating the native clock. The device clock configuration remains
enabled. Save these server-side preferences too: restoring firmware alone does
not restore the previous BASSWIESN metadata presentation.

### Screenshot interpretation

A 128-by-100, 8-bit framebuffer is not sufficient evidence that its bytes are
linear grayscale. Some raw conversions produced apparently missing text while
the operator could see a correct physical display. The inspected firmware's
native screenshot utility obtains the framebuffer color map and produced useful
captures on SCM devices. Some SM2 captures remain incomplete or ambiguous.
Do not infer a broken renderer, or claim visual acceptance, from those captures
alone. Physical observation and reproducible model-specific capture validation
remain separate checks; experimental nibble conversions are not verified screen
images.

## Battery compatibility is preservation, not an extra installation

Display and boot lettering never patch BatteryMonitor. The known stock,
BASSWIESN replacement-battery and previously observed custom BatteryMonitor
hashes are separate compatibility states. An original battery must not receive
the replacement-battery option merely because display or boot lettering was
selected.

Some radios already have a modified BoseApp as well as a modified BatteryMonitor.
These are distinct facts. A display candidate must be derived from the exact
existing application, changing only owned ranges. Existing changes outside
those ranges are retained byte-for-byte. Overlap, unknown hashes, a partial
boot-writer pair, changed startup hooks or an unexpected framebuffer block the
operation. Never copy a whole stock BoseApp over a previously modified image.

Known compatibility does not certify the behavior of an unrelated historical
patch. No identity cloning, authentication bypass or additional AirPlay unlock
is introduced by the display composition.

## Install and recovery contract

1. Identify the exact device, model, variant, platform, firmware and panel.
2. Capture current application, BatteryMonitor, both splash writers, startup
   script and relevant configuration. Verify private off-device SHA-256 backups.
3. Build a candidate from the exact base; prove the owned byte ranges and
   preservation of unrelated changes. Inspect any existing startup script.
4. Require suitable power, standby, enough space, no concurrent mutation and
   a confirmed recovery path. Never kill or replace a running BoseApp inode.
5. Stage a separate candidate. Install the cold hook before the native service
   supervisor, retaining an approved existing nonblocking startup script.
6. Activate with a complete device reboot, not a warm application restart.
7. Check stable identity/API/process, actual hashes, read-only root filesystem,
   unchanged battery hash, preserved logger, presets and zone; inspect the real
   framebuffer and request listening acceptance separately when relevant.

Boot probation requires repeated stable API/PID observations. An incomplete
probation disables the next binding; a failed initial boot can request one
whole-device reset. This is not a permanent health watchdog and is not a promise
that every possible hardware failure can recover remotely.

Display removal disables its own hook and uses a full reboot to drop the bind.
The exact pre-patch application remains underneath, including earlier unrelated
modifications. Boot-logo removal restores only the two verified splash writers.
Battery rollback restores only its own verified backup. Removing one option
must not restore another component to factory state. Existing logger startup
must be restored byte-for-byte. Diverged files are not silently overwritten.

Do not use factory reset as a substitute for patch rollback: it does not
necessarily restore root-filesystem executables. Keep all original backups.

## ANTENNE BAYERN and missing metadata

Observed main AAC and MP3 ICY feeds returned a station slogan rather than a
song. The station's [web player](https://www.antenne.de/webradio/live) uses a
separate programme metadata endpoint. The optional collector now recognizes
only the exact main-feed addresses and reads explicit artist/title fields from
that endpoint. Freshness, channel and programme class are checked; moderation
clears old song fields. Requests are public-address-validated, DNS-pinned,
time/size-limited, without credentials and without following redirects.

If that source fails, ordinary bounded ICY collection remains the fallback.
No preset URL is rewritten, no audio is proxied and metadata failure cannot
stop playback. Programme timestamps are not proof of sample-accurate alignment
with delayed or personalized stream audio. Other stations and subchannels are
not mapped by name or guessed from this feed.

In the private AAC trial, actual `now_playing` readback and a native display
capture contained separate track and artist fields. The operator also confirmed
the station/large-clock layout when no track or artist was provided. This is
not a claim that every ANTENNE stream variant or song transition is validated.

## Source and publication boundary

Authored renderer and boot payload sources are in `modules/radio-patches/`.
Firmware, full patched executables, local device identities, backups, SSH host
keys and private installation packages must remain outside version control.
There is no public firmware distribution or universal install script.

Software tests, exact-image inspection, on-radio runtime checks and human
display/audio acceptance are separate evidence classes. The LAB UI and this
document must not turn a saved plan or a simulator result into hardware proof.
