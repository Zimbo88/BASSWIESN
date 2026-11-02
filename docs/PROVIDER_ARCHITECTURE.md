# Provider catalogs and optional Music Assistant bridge

Status: **software-tested catalog framework; Music Assistant remains disabled-by-default LAB**.
No real Music Assistant server, SoundTouch radio, Pi, router, private network,
stream delivery or audible playback was exercised for this implementation.
This is not a streaming-service login, Spotify credential extraction, remote
queue controller, or a claim of Music Assistant audio playback support.

## Contracts and existing behavior

`basswiesn/app/services/providers/` adds an independent, typed catalog domain:

| Contract | Saved stations | Optional Music Assistant |
| --- | --- | --- |
| Health | Database read | Version check and authenticated root browse |
| Search | Existing station names, escaped literal search | Bounded track/radio/album/artist/playlist preview |
| Browse | Existing stations with bounded offset | Provider folders with bounded offset |
| Metadata | Existing station identity/name | Projected title, artist, album, duration and identity |
| Resolve | Public, credential-free saved HTTP(S) stream candidate | Explicitly unsupported |
| Playback commands / reporting | Neither | Neither |

`ProviderAdapter` is the extension contract. Frozen DTOs carry descriptors,
capabilities, health, media items, pages and playable-URI candidates. Unsupported,
disabled, invalid, unavailable, timeout, authentication, target-policy and
protocol-version errors have fixed codes. No raw transport exception, URL,
credential, upstream response body or arbitrary provider metadata becomes an error.

The local adapter reads the existing `Station` table; it creates no sources or
accounts and excludes internal/LAB-only stations. Existing Bose source IDs,
`SERVICE_MANIFEST`, persistence XML, Orion playback, presets and keepalive paths
remain unchanged. A provider name in that legacy manifest is not evidence that
a new executable adapter or usable service login exists.

The manager is lazy and process-local. Inventory and cached-health reads do not
probe anything. There are no startup probes, background jobs, retries, recovery
writes or optional-service dependencies. Each provider has one in-flight request
per process; competing work receives `PROVIDER_BUSY`, without an unbounded queue.
An operation has a ten-second total deadline; each optional HTTP exchange has an
eight-second deadline and five-second HTTP timeouts. Caller cancellation closes
the transport and releases the in-flight slot. This is not distributed throttling.

Provider availability, playback and reporting are independent fields. A catalog
success can establish provider health, never audible playback. A timeout,
revoked token or bad response changes only that provider's state. Playback stays
`UNKNOWN` and reporting stays `NOT_APPLICABLE`, unless a future separate source
of evidence implements those planes. The framework does not overwrite existing
device/playback/reporting reducers or durable observations.

## Verified upstream protocol, not inferred streaming routes

The adapter was checked against official Music Assistant server **2.7.4**, commit
`00fa04213156b5a2825bf4f83bcd05370a4b1bee`; that server pins
`music-assistant-models==1.1.86`. This is a narrow protocol verification target,
not a recommendation to deploy an older release or a claim about the latest
version's security. Other server versions are refused before sending the token.

Primary references:

- [Official API documentation](https://www.music-assistant.io/api/) describes
  bearer-authenticated `POST /api` commands.
- [Pinned web controller](https://github.com/music-assistant/server/blob/00fa04213156b5a2825bf4f83bcd05370a4b1bee/music_assistant/controllers/webserver/controller.py)
  defines `GET /info`, `POST /api`, authentication and the raw JSON result
  response. HTTP is not the WebSocket result-envelope protocol.
- [Pinned music controller](https://github.com/music-assistant/server/blob/00fa04213156b5a2825bf4f83bcd05370a4b1bee/music_assistant/controllers/music.py)
  defines `music/search`, `music/browse` and `music/item_by_uri`. Search uses
  `search_query`, `media_types`, `limit`, `library_only`; the radio result field
  is singular `radio`. Search has no offset in this contract.
- [Pinned stream controller](https://github.com/music-assistant/server/blob/00fa04213156b5a2825bf4f83bcd05370a4b1bee/music_assistant/controllers/streams/streams_controller.py)
  resolves streams internally against player queues and sessions, not through
  an independent public command. The bridge does not invent `/stream` routes,
  synthesize queue/session URLs or mutate a queue to obtain one.
- [Pinned dependency manifest](https://github.com/music-assistant/server/blob/00fa04213156b5a2825bf4f83bcd05370a4b1bee/pyproject.toml)
  identifies the upstream model version; no upstream package is installed here.

Search results explicitly have `offset_supported=false`, `has_more=null` and a
`truncated` flag when the bounded preview may be incomplete. Browse offsets are
local slices of a bounded upstream list, not an invented upstream pagination API.
Identifiers are conservative opaque provider URIs. HTTP/file/builtin URIs,
embedded URLs, traversal and encoded URL indirection are refused; unusual
provider-specific IDs outside the documented grammar may require a later
verified extension. Metadata identity rewriting is also refused.

## Configuration and security boundary

Only an explicitly configured optional service is contacted, and only after a
session-authorized request. The four process environment variables are separate
from general/public Settings dumps:

```dotenv
MUSIC_ASSISTANT_ENABLED=false
MUSIC_ASSISTANT_URL=
MUSIC_ASSISTANT_TOKEN=
MUSIC_ASSISTANT_ALLOWED_IPS=
```

For a later authorized LAB acceptance, supply an exact **HTTPS origin** without
path, query, fragment or userinfo; an explicitly generated token belonging to a
dedicated non-administrator Music Assistant user; and a comma-separated list of
exact approved peer IPv4/IPv6 addresses (not subnets). The default plaintext
Music Assistant HTTP endpoint is intentionally unsupported. Use an operator-
controlled TLS endpoint with a normally validated certificate; there is no
`verify=false` or environment-proxy escape hatch. No new container, privileged
mode, host network, system dependency or external service is required by core
BASSWIESN. Configure the environment through the existing deployment mechanism;
the adapter does not read files or scrape another application's credentials.
Protect this dedicated token as a deployment secret, never paste it into browser
queries, shell-history commands, stations, presets or support reports. Restart
BASSWIESN to apply credential/configuration changes to its lazy manager.

Outbound policy deliberately tightens the existing generic LAN URL policy:

- Use the central protected-device registry and current `Device` inventory;
  every known radio is excluded, even if not specially protected. Failed or
  malformed inventory/protection lookup fails closed.
- Resolve all addresses before each request; every answer must pass. Reject
  loopback, link-local, metadata, multicast, unspecified, special-purpose and
  IPv6 transition/NAT64 peers. Normalize IPv4-mapped IPv6 before comparison.
- MA private RFC1918/ULA peers are permitted only by exact configured address.
  Saved-stream candidates require global addresses; this does not change the
  existing independent station playback path.
- Reuse `network_security.pinned_http_target`: connect to the validated numeric
  address, preserve original HTTP Host and HTTPS SNI/certificate identity, and
  never re-resolve a hostname inside the HTTP connection. Redirects are refused.
  Cookies, environment proxies and upstream cookies are not reused.
- Send a dedicated bearer only to the exact `/api` origin after a matching
  `/info` version result. No provider/Spotify/account credentials are copied.
- Bound HTTP JSON to 512 KiB, depth 16 and 20,000 values; reject encoded bodies,
  invalid types, excessive result lists and unexpected payloads. Request JSON
  is bounded to 4 KiB and result pages to 100 items/offset 1000.

A local `PlayableURI` is only a candidate: `playback_verified=false` and
`policy_recheck_required=true`. It has not been fetched, decoded, redirected,
transcoded or sent to a radio. Its original hostname is not a durable safety
grant; any subsequent consumer must revalidate/pin its own connection or use
the existing reviewed playback path. Signed/query-bearing saved URLs are not
exposed by this catalog resolver. It is not a general HTTP relay.

## Usable API, no automatic player control

The router is mounted on the web listener only. **Required-mode administrator
session authentication is mandatory**, including when legacy mode is enabled;
legacy mode receives `AUTH_REQUIRED` for these new routes. No existing bearer
scope grants provider access. Every outbound-capable operation is an explicit
POST with the existing same-origin and CSRF boundary. Responses are `no-store`.

| Route | Behavior / JSON body |
| --- | --- |
| `GET /api/providers` | Descriptors and cached observations; no network |
| `GET /api/providers/{id}/health` | Cached health; no network |
| `POST /api/providers/{id}/health` | Explicit probe, `{}` |
| `POST /api/providers/{id}/search` | `{"query":"artist","limit":25,"offset":0}` |
| `POST /api/providers/{id}/browse` | `{"parent":"","limit":25,"offset":0}` |
| `POST /api/providers/{id}/metadata` | `{"item_id":"library://track/12"}` for MA, numeric station ID for local |
| `POST /api/providers/{id}/resolve` | Numeric saved-station ID only; MA returns unsupported |

Provider IDs are `local_stations` and `music_assistant`. After ordinary login to
the required-mode web UI, this same-origin browser-console example exercises the
local catalog without contacting a provider, stream or radio:

```javascript
const session = await (await fetch('/auth/session', {cache: 'no-store'})).json();
const response = await fetch('/api/providers/local_stations/browse', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-BASSWIESN-CSRF': session.csrf_token},
  body: JSON.stringify({parent: '', limit: 25, offset: 0}),
});
console.log(await response.json());
```

Substituting `music_assistant` explicitly contacts the separately enabled service.
There is no new dashboard/player UI or automatic catalog import. The callable
API and station-backed adapter are wired into the real application, not an
unregistered example implementation.

## Acceptance and remaining manual checks

`tests/test_provider_framework*.py` uses injected DNS and `httpx.MockTransport`;
the shared no-real-network fixture remains active. Coverage includes local DB
filtering, actual web auth/CSRF/bearer boundaries, disabled and malformed optional
configuration, exact MA command payloads, version mismatch, health-plane failure
isolation, concurrency/cancellation/deadlines, response/request limits, IPv4/IPv6
SSRF and DNS rebinding, Host/SNI pinning, redirects and credential non-disclosure.
Run the tests on supported Python 3.11 and the locked development interpreter;
strict mypy covers the new package and router. No test exemption allows LAN I/O.

Software acceptance covers the typed framework and the optional catalog bridge
against the verified protocol fixture. **Real upstream interoperability, token
issuance/revocation on a real MA server, TLS deployment and real device/audio
acceptance are NOT RUN.** They require separate explicit authorization. Before
supporting another MA version, inspect its pinned primary-source contract and
update fixtures; do not relax the version gate speculatively. Audible MA playback
would be a different future integration with an independently verified stream
and device-capability contract, not merely changing `resolve` to true.
