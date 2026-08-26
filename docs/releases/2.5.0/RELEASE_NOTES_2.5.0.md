# BASSWIESN 2.5.0

BASSWIESN 2.5.0 makes the normal SoundTouch experience simpler while keeping
the safety and diagnostics developed for 2.0.

## Highlights

- Easy Mode is now the default for new installations. It shows Setup, Radios,
  Remote Control, Presets, Multiroom, Alarm & Timer and Device Settings.
- Offline/stale-address recovery can rediscover a radio by stable device ID,
  verify a changed IP and reset stale backoff state.
- Preset Checker uses radio read-back plus provider, account, stream and local
  mapping evidence instead of trusting a database row.
- The Radio Browser dual-stack fix prefers validated IPv4 when the host has an
  unusable IPv6 route, while retaining SSRF and protected-target protection.
- BMX audio responses contain a bounded confirmed stream contract instead of a
  large repeated list.
- Multiroom supports optional per-radio pre-zone volumes and bounded verified
  member removal.

## Safety

Protected IPs and stable device IDs are checked before network transport.
Protected devices are not offered in network-active UI selectors. Unknown
provider contracts, malformed Orion descriptors and unverified preset or zone
changes fail closed.

## Hardware observations

SoundTouch firmware may resume the last source, clear mute and normalize
volume while forming a zone. BASSWIESN reports observed before/after values and
does not silently fight the firmware. Preset and playback results likewise use
radio read-back rather than treating command acceptance as success.

## Known limitations

- Critical setup writes remain restricted to exact validated firmware/model/
  product/variant/platform profiles.
- Physical preset-button validation can require a person.
- Some third-party Bose-era providers are unsupported or experimental.
- “Offline” is feature-specific; local streams, Internet services, Bose
  services and the BASSWIESN host have different dependencies.
- AirPlayReadiness is diagnostic and includes no authentication bypass or
  firmware modification.
- Multiroom firmware behavior can override requested volume or mute state.

See the README and setup guide for installation and first-use instructions.
