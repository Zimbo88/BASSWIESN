# BASSWIESN 2.5.1

BASSWIESN 2.5.1 is a focused bugfix release for the real 2.5.0 user
experience. It repairs Easy Mode discovery, desktop radio removal, bilingual
UI consistency and station artwork without expanding the normal interface.

## Highlights

- Easy Mode Setup and Radios now use the same explicit, bounded discovery path
  as Standard and LAB mode and render verified results visibly.
- About BASSWIESN is available in Easy Mode.
- Desktop radio cards keep **Remove from BASSWIESN** within the viewport; the
  action remains local and does not reset the radio.
- English and German end-user text is translated consistently across Easy,
  Standard and LAB workflows. Firmware/API tokens remain unchanged.
- Online Station Search displays available station logos again. Untrusted
  catalogue URLs are fetched only through BASSWIESN's guarded, DNS-pinned
  raster cache and failures use a neutral placeholder.
- Radio Display adds **No station logo**. Preset synchronization previews and
  verifies an artwork-only change while preserving source, account, location
  and item identity.
- Experimental single-member Multiroom removal is visible only in LAB.

## LAB Factory Reset

LAB now provides an exact-profile Factory Reset card using the confirmed
SoundTouch CLI 17000 `sys factorydefault` operation. It requires a backup,
current identity and firmware read-back, an approved exact model/build profile,
a checkbox, typed confirmation and a final browser confirmation. Protected
devices cannot be selected or reset.

Factory Reset erases radio configuration and can remove the radio from the
current Wi-Fi network. BASSWIESN therefore does not perform an automatic probe
after sending it, and the user may need to reconnect the radio manually. It is
never an automatic recovery action.

## Preset integrity

The Preset Checker treats a legacy local-radio `sourceAccount` as stale and
uses the confirmed empty value. It reports the same station served by another
BASSWIESN origin as a warning rather than falsely classifying it as a different
station. Artwork synchronization never recreates a missing or unmapped slot
and reports partial/skipped work explicitly.

## Upgrade note

The running package version is now authoritative. A preserved `.env` from an
older installation can no longer make a 2.5.1 server advertise an old version.
Existing data and `.env` files remain preserved during normal installation.

## Safety

This release retains central IP/device-ID protection, DNS/redirect checks,
exact-build write profiles, backup-before-write and radio read-back before
success. It also retains the 2.5.0 dual-stack Radio Browser fix that prefers a
validated IPv4 address when IPv6 is present but unusable.

## Known limitations

- Critical writes remain limited to exact validated firmware/model/product/
  variant/platform profiles.
- Factory Reset is LAB-only and may require manual Wi-Fi reconnection.
- Physical preset-button and OLED appearance checks can require a person.
- Single-member Multiroom removal remains firmware-dependent and LAB-only.
- AirPlayReadiness is diagnostic only; no authentication bypass or firmware
  modification is included.
- HLS support is not claimed by this release.
