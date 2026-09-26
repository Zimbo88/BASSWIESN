# DLNA media library

Experimental LAB-only functionality in 3.0.0, disabled by default. Protocol and
synthetic relay tests passed; audible radio acceptance remains open. Switch to
LAB before enabling, connecting, browsing or importing. Disabling and forgetting
a server remain possible outside LAB; a mode change does not stop an active relay.

The library reads an explicitly selected UPnP **ContentDirectory** server. It
browses folders, reads track/artist/album information, and adds an MP3 or AAC
item to BASSWIESN's Stations list. Starting that item uses the existing radio
selection, safe-volume, identity-check and readback workflow. Browsing and
importing alone never start a radio or change its presets.

## Connect and play

1. Open **Media/NAS** in Standard or LAB mode and enable the music library.
2. Enter your server's HTTP device-description URL, for example
   `http://192.0.2.42:8200/rootDesc.xml`. This is an example address, not a
   suggested address for your home network. The path is server-dependent; a
   NAS administration page or playlist URL is not a device description.
3. Select **Connect this server**. Open a folder and choose a supported item.
4. Select **Add to stations**. Then select that item and a radio in **Stations**.
   Review the playback preview before starting it.

Enable UPnP media sharing on the server itself first. BASSWIESN does not mount
SMB/NFS shares, ask for NAS passwords, or scan the network. Technical native-radio
media probes and saved collection records remain in the collapsed LAB section;
they are separate from this browser.

The selected server's UDN identity is stored. Before each browse, import or
audio request, its description is read again and compared with that identity.
If the address now belongs to another server, the action stops. Reconnect only
after verifying the server address yourself.

## What the relay does

```text
Explicit server URL -> description + identity check -> Browse
                                      |
                             selected object identity
                                      |
Stations -> radio playback -> BASSWIESN audio relay -> same-server MP3/AAC
```

Imports store an object identity and a stable BASSWIESN audio URL, not an arbitrary
client-provided fetch URL. Each audio request resolves the object again. Only a
resource on the approved description's origin (same host and port) is used.
The relay streams bytes without decoding, transcoding or keeping an audio copy.
Single byte-range requests are supported; multi-range and suffix-range requests
are not. Metadata displayed in the library comes from the server's DIDL-Lite
catalogue; this does not prove that every radio display will show those fields.

This is a single-item path, not an album queue, playlist scheduler or generic
UPnP AVTransport controller. Seeking and finite-track behavior remain subject
to the radio's existing playback implementation. Importing an item is not proof
that a particular radio can decode it.

## Boundaries and troubleshooting

| Symptom or requirement | Behavior |
|---|---|
| DNS name, HTTPS or authenticated NAS | Not supported by this path; only literal LAN IPv4 HTTP endpoints without credentials |
| Different host/port for control or audio | Rejected rather than followed automatically |
| Registered radio or protected address | Blocked before transport; radios are not media-server targets |
| Missing/ambiguous ContentDirectory service | Connection rejected; no alternate-service scan |
| Catalogue changes between pages | UpdateID mismatch stops pagination; reopen the folder |
| Valid entries with an unknown total | Accepted; Next page requests another bounded page, even after a short page. No automatic catalogue scan |
| FLAC, Ogg, Opus, WMA or an unrecognized resource | Shown as unsupported; not silently advertised as playable |
| MP3/AAC advertised but resource changes format | Relay stops instead of serving mismatched content |
| Redirect, timeout, malformed or oversized XML | Bounded failure, no redirect to another target |
| Disable the library | Prevents new browse/import/relay requests; it does not send STOP to radios or terminate an already established stream |
| Forget the server | Removes its local mapping, not NAS files; imported station entries remain but must be removed or imported again after reconnecting |

Limits: 16 server records, 1,000 imported objects, 100 objects per page, 2 MiB XML,
four concurrent relay streams per application process, 512 MiB per resource,
eight-second transport inactivity timeout, and three-hour stream duration.
The library is intended for a trusted local installation, not an Internet-facing
anonymous media proxy. Server identity is a consistency check, not cryptographic
authentication.

In ContentDirectory, `TotalMatches=0` with a nonzero `NumberReturned` means the
total is not available, not that the folder is empty. BASSWIESN exposes that as
`total: null`. It still verifies the DIDL object count, checks positive totals,
stops at an empty page and never offers a starting index above 1,000,000.
`BrowseMetadata` remains a one-object operation with a total of one.

Adding more native codecs would require device evidence. Server-side conversion
is possible without changing radio firmware, but needs a separately bounded
decoder/encoder, CPU limits and playback tests. This implementation does not
claim that conversion already exists.

## Verification

Offline tests exercise description parsing, SOAP Browse, escaped DIDL-Lite,
pagination, identity changes, protected-target rejection, import, relay,
byte ranges, errors and resource cleanup. Chromium and WebKit tests use actual
button clicks in German and English against a synthetic server.

A separate disposable Pi container has also served synthetic silence over real
HTTP to the ContentDirectory client and relay. Byte-for-byte and range checks
passed; its server/container/network were removed, with the production container
unchanged. This is **protocol/transport evidence**, not audible-radio or broad
NAS-vendor compatibility certification. No household media was used.

An independent ReadyMedia 1.3.3 server (Debian package `1.3.3+dfsg-1.1+b1`)
passed the same isolated Pi checks: description, real SOAP folder browsing,
artist/album, import, byte-exact MP3 relay, byte ranges and forgetting the server.
This test exposed and verified the correction for a non-empty page with an
unknown total. It used only generated digital silence; neither radios nor
household media were involved. Temporary server and network resources were
removed after the test.

```sh
.venv/bin/python -m pytest -q tests/test_dlna_library_300.py tests/test_dlna_status_300.py
.venv/bin/python -m pytest -q tests/test_dlna_browser_300.py
```

Protocol references: [UPnP Device Architecture](https://upnp.org/specs/arch/UPnP-arch-DeviceArchitecture-v1.0.pdf)
and [ContentDirectory service](https://upnp.org/specs/av/UPnP-av-ContentDirectory-v1-Service.pdf).
This is a deliberately restricted implementation, not a UPnP/DLNA certification claim.
