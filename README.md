# Nginx HTTP/2 16 KiB

Custom Nginx stable image for Cresset. The TLS HTTP/2 frontend explicitly advertises
`SETTINGS_MAX_FRAME_SIZE = 16384` and rejects larger inbound frames with connection
error `FRAME_SIZE_ERROR` (6). No production configuration is bundled or changed.

## Build and source provenance

- Nginx: **1.30.6**, patch revision **16k-r1**.
- Platforms: `linux/amd64` and `linux/arm64`, validated separately on native CI runners.
- Official runtime: `nginx:1.30.6-alpine`, pinned by multi-platform manifest digest in
  `Dockerfile`. Its entrypoint, nginx user, configuration paths, log symlinks,
  certificates/mount conventions and shipped dynamic modules are retained.
- Source: <https://nginx.org/download/nginx-1.30.6.tar.gz>.
- SHA-256: `3cf46f5ed7504d460125866b7bea99fb357f31d887e2e63b35503310e9b8b6c8`.
- The source archive was verified against its detached PGP signature using the
  official Sergey Kandaurov key from <https://nginx.org/keys/pluknet.key>, fingerprint
  `D6786CE303D9A9022998DC6CC8464D549AF75C0A`, listed at
  <https://nginx.org/en/pgp_keys.html>. Every build checks the pinned archive hash.

The builder uses the exact configure arguments reported by the pinned official
binary, including TLS, HTTP/2, HTTP/3, real IP, stream, mail and compatibility flags.
Both stripped executables (`nginx` and `nginx-debug`) are patched and copied into the official runtime. Compilers and
headers stay in the builder. All shipped dynamic modules are loaded by their matching release/debug binary in
integration tests to detect ABI incompatibilities.

The base and source are immutable inputs. Alpine build dependencies come from the
base's release repositories; builds are reproducible in procedure, not promised to
be byte-identical as those package repositories receive updates. Record the final
image digest. Runtime dependencies remain those of the pinned official image.

```sh
docker buildx build --platform linux/amd64 --load -t cresset-nginx:test .
scripts/test.sh cresset-nginx:test
# On an arm64 host:
docker buildx build --platform linux/arm64 --load -t cresset-nginx:arm64 .
PLATFORM=linux/arm64 scripts/test.sh cresset-nginx:arm64
```

## Patch rationale

`patches/nginx-http2-16k.patch` changes the value serialized by
`ngx_http_v2_send_settings()` from `NGX_HTTP_V2_MAX_FRAME_SIZE` (16777215) to
`NGX_HTTP_V2_DEFAULT_FRAME_SIZE` (16384). Both constants retain their upstream
values. The peer's SETTINGS validation and its outgoing frame-size preference
remain unchanged.

In this release, `ngx_http_v2_state_head()` parses the 24-bit frame length without
a general receive-size limit. The patch checks that length against 16384 before
frame dispatch, including unknown frame types, and returns the existing
`NGX_HTTP_V2_SIZE_ERROR`. This enforces the advertised limit consistently, including
the protocol's initial default. It needs no SETTINGS-ACK transition because the
initial and advertised limits are identical. Flow control windows, body handling,
HPACK, TLS and upstream modules are unchanged. See RFC 9113 sections
[4.2](https://www.rfc-editor.org/rfc/rfc9113.html#section-4.2) and
[6.5.2](https://www.rfc-editor.org/rfc/rfc9113.html#section-6.5.2).

Patch application uses `--fuzz=0` and a dry run. A source/hash/patch mismatch fails
the build. Review the two call sites on every upgrade rather than redefining the
global maximum constant.

Advertising 16 KiB is intended to limit buffers chosen by compliant clients. These
tests do not measure Xray or iOS Network Extension memory usage.

## Validation

`scripts/test.sh IMAGE` first runs the unmodified runtime's `nginx -t`. It then
builds an ephemeral test image containing the exact same executables plus Python,
OpenSSL and pinned hyper-h2 libraries. No test tools enter the published image.
The protocol harness tests each executable with a local HTTP upstream and HTTP/2 gRPC
mock upstreams on TCP and Unix sockets. It checks:

- Verified local TLS certificate, ALPN `h2`, explicit SETTINGS value and GET.
- A multi-megabyte upload forwarded before END_STREAM with buffering disabled.
- Large streamed responses, 15 concurrent streams, small windows and a slow reader.
- Binary gRPC messages and trailers through TCP and Unix sockets.
- HTTP upstream truncation produces an isolated `RST_STREAM INTERNAL_ERROR`, while
  a gRPC upstream closing before headers produces HTTP 502, as in the official image.
  A subsequent request on the same frontend connection must succeed.
- No unexpected RST_STREAM, GOAWAY or PROTOCOL_ERROR during valid transfers.
- A clean client close; an oversized unknown frame must produce GOAWAY error 6.
- Loading every shipped release/debug dynamic module with its matching binary and
  checking Nginx's clean process exit.

Compare the official image using the same suite:

```sh
EXPECTED_FRAME_SIZE=16777215 scripts/test.sh \
  nginx:1.30.6-alpine@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311
```

Only the oversized-frame enforcement test is skipped for that baseline. Real Xray
clients, CDN routing, production mounts and iOS memory require deployment-specific
validation. The production configuration was not supplied for this build.

## Publication and maintenance

Repository: <https://github.com/trykof/nginx-http2-16k>.
Registry: `ghcr.io/trykof/nginx-http2-16k`.

Push to `main` or dispatch `Build, validate and publish`. CI builds and validates
both architectures, compares the official image, and uploads the tested image
archives. Publication depends on both validation jobs. It pushes those archives,
then creates a multi-platform manifest. The publish job alone gets
`packages: write` and authenticates with `GITHUB_TOKEN`.

Tags include the entire Git commit:

```text
1.30.6-16k-r1-<full-git-commit>
sha-<full-git-commit>
1.30.6-16k-r1-<full-git-commit>-amd64
1.30.6-16k-r1-<full-git-commit>-arm64
```

The script refuses to overwrite existing architecture tags. Use the manifest digest
for deployment; registry tag names are not a substitute for digest pinning. The
workflow summary and `published-digest` artifact record it. CI independently pulls
the published digest and reruns protocol tests on amd64. Release verification also
pulls and tests arm64 before delivery. If publication is interrupted after a tag
was pushed, inspect it and publish a new commit instead of overwriting that tag.
GHCR package visibility can require a separate public setting even for a public
repository; authenticated pulls work with `read:packages` access.

For upgrades, inspect <https://nginx.org/en/security_advisories.html>, update the
base digest, source version and verified hash together, re-review/rebase the patch,
bump the patch revision and tag prefix, and run both architecture suites. Update
pinned Actions periodically. Maintaining this image means tracking upstream Nginx
and Alpine security releases; the current digest never updates itself.

## Production deployment and rollback

Run these commands from the existing production Compose directory. The service
name below is `nginx`; substitute its actual name if different. Keep the same
Compose project name, base file, environment and profiles used by the deployment.
Do not copy the test configuration to production.

Set `NEW_IMAGE` to the verified `ghcr.io/trykof/nginx-http2-16k@sha256:...` reference
from the workflow artifact or release report. Download the overlay from the tested
commit, replacing `RELEASE_COMMIT` with its full hash:

```sh
export RELEASE_COMMIT='<full-git-commit>'
export NEW_IMAGE='ghcr.io/trykof/nginx-http2-16k@sha256:<published-manifest-digest>'
curl -fL "https://raw.githubusercontent.com/trykof/nginx-http2-16k/$RELEASE_COMMIT/compose.http2-16k.yml" \
  -o compose.http2-16k.yml
export CRESSET_NGINX_IMAGE="$NEW_IMAGE"
dc() { docker compose -f docker-compose.yml -f compose.http2-16k.yml "$@"; }

# Save the exact running local image, including when its original tag was mutable.
previous_container=$(docker compose -f docker-compose.yml ps -q nginx)
test -n "$previous_container"
previous_image=$(docker inspect --format '{{.Image}}' "$previous_container")
printf '%s\n' "$previous_image" > nginx-rollback-image.txt
docker image tag "$previous_image" cresset-nginx-rollback:before-16k

docker pull "$NEW_IMAGE"
dc config --quiet
dc run --rm --no-deps nginx nginx -t
dc up -d --no-deps --force-recreate nginx
dc exec -T nginx nginx -t
dc logs --tail=100 nginx
```

For private GHCR packages, authenticate before pulling using a token with
`read:packages` via `docker login ghcr.io --password-stdin`; never put it in Compose.

Verify the public endpoint and advertised settings:

```sh
curl --fail --show-error --http2 https://de.cresset.digital/ -o /dev/null
nghttp -nv https://de.cresset.digital/ 2>&1 | tee nginx-http2-verification.log
python3 - <<'PY'
from pathlib import Path
text = Path('nginx-http2-verification.log').read_text()
assert 'SETTINGS_MAX_FRAME_SIZE:16384' in text.replace(' ', ''), text
assert 'The negotiated protocol: h2' in text, text
PY
```

Install `nghttp` using the server distribution's nghttp2 client package if needed.
The successful GET and nghttp negotiation verify the website and TLS endpoint.
For XHTTP direct, XHTTP through the CDN and gRPC, use three existing Xray clients
configured for those actual routes. Set each variable below to its running local
SOCKS proxy URL (for example `socks5h://127.0.0.1:1080`). A successful HTTPS transfer
through each client validates its transport end to end:

```sh
: "${XHTTP_DIRECT_SOCKS:?Set the direct XHTTP client proxy URL}"
: "${XHTTP_CDN_SOCKS:?Set the CDN XHTTP client proxy URL}"
: "${GRPC_SOCKS:?Set the gRPC client proxy URL}"
for proxy in "$XHTTP_DIRECT_SOCKS" "$XHTTP_CDN_SOCKS" "$GRPC_SOCKS"; do
  curl --fail --show-error --max-time 30 --proxy "$proxy" https://example.com/ -o /dev/null
done
```

No server names, transport paths, sockets or certificate mounts are changed by the
overlay. Real production compatibility remains conditional on `nginx -t` and these
transport checks because its configuration and clients were not provided.

Rollback immediately, using the same shell function and saved local image:

```sh
export CRESSET_NGINX_IMAGE="$(cat nginx-rollback-image.txt)"
dc up -d --no-deps --force-recreate --pull never nginx
dc exec -T nginx nginx -t
curl --fail --show-error --http2 https://de.cresset.digital/ -o /dev/null
```

Retain the rollback image until the deployment is accepted. To return to the
original image declaration later, use only the original Compose file, with its
image pinned to the previous known digest to avoid pulling a changed stable tag.
