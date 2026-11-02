# Third-party notices

## Optional LAB AirPlay receiver observer

The isolated receiver experiment builds Shairport Sync commit
`7bad231c18368dbd26f298577f6210e36e4b0797` (tag `5.5.2`) from an archive verified by SHA-256
`eab1fa095e34676d05f68e38d86501d5afa3fc46f83f044859d4d125d526daec`.
Source: <https://github.com/mikebrady/shairport-sync>.

The small RTSP hook patch is in `modules/airplay-bridge/native/`. That directory's
`SHAIRPORT-NOTICE` retains the original file's MIT-style notice, also copied into
the optional image. The new observer header is MIT-licensed BASSWIESN code.
Pairing, decryption, timing and audio logic are not replaced. Other upstream
files and dependencies retain their respective licenses. A distributable image
still requires the full dependency/source and license inventory; these private
experiments are not release artifacts.

## Optional isolated AirPlay test sender

`tools/ap2_buffered_test_sender.py` can invoke the separately obtained
[cliairplay v0.5.8](https://github.com/music-assistant/airplay-cli/tree/v0.5.8)
Linux ARM64 executable. Its SHA-256 is
`73cfae9328d8882114cc788bd15676fc1dec844ff87ca832fb602818cc1b409d`.
The executable is GPL-3.0; retain its upstream `LICENSE` and
`THIRD_PARTY_NOTICES.md` with any separately downloaded distribution. Neither
its code nor binary is vendored into BASSWIESN. This test-only subprocess is not
a runtime dependency, an Apple certification, or a production installation.

The alternative `tools/ap2_test_sender.py` diagnostic uses separately installed
[pyatv 0.18.0](https://github.com/postlund/pyatv/tree/v0.18.0), MIT licensed.
Its NTP session setup did not interoperate with the pinned receiver fixture;
it must not be presented as a successful AP2 audio test.

## OpenSSH portable client

The container builds the unmodified OpenSSH portable 10.6p1 client from
[official upstream sources](https://www.openssh.org/portable.html). The source,
detached signature and release-key hashes, and the exact signer fingerprint,
are pinned in `tools/openssh-source.json`. The build verifies all of them before
compilation; no source archive or proprietary firmware is vendored here.

The runtime retains upstream's complete `LICENCE` file at
`/usr/local/share/doc/openssh/LICENCE`, with its individual BSD/ISC-style notices,
and a source/binary-hash manifest alongside it. No SSH server, compiler or
setuid SSH helper is installed. This separately built upstream client is not a
claim that Debian has patched its stable OpenSSH package. Future client updates
require explicit source-lock review and compatibility/security checks.

## QR rendering dependency

The LAB workbench uses the unmodified Python `qrcode` package, version 8.2,
installed through `requirements.txt`. No package source is vendored here.
Its distributed license is BSD-3-Clause with retained MIT-origin attribution.
The package's copyright and license remain in installed distributions.

> Copyright (c) 2011, Lincoln Loop. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice,
  this list of conditions and the following disclaimer.
- Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.
- Neither the package name nor the names of its contributors may be used to
  endorse or promote products derived from this software without specific
  prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The package also retains attribution to original QR algorithm work:
Copyright (c) 2009 Kazuhiko Arase, MIT license; JavaScript port by Sam Curren.
"QR Code" is a registered trademark of DENSO WAVE INCORPORATED.

Other runtime dependencies retain their own distributed licenses and notices.
This document does not replace or remove those notices.
