#!/bin/sh
set -eu
image=${1:-cresset-nginx:test}
platform=${PLATFORM:-linux/amd64}
expected=${EXPECTED_FRAME_SIZE:-16384}
docker run --rm --platform "$platform" --entrypoint nginx "$image" -t
# Use the daemon builder so FROM can resolve the locally loaded runtime.
docker buildx build --builder "$(docker context show)" --platform "$platform" --load --build-arg "IMAGE=$image" -f tests/Dockerfile -t cresset-nginx-protocol:test .
docker run --rm --platform "$platform" -e "EXPECTED_FRAME_SIZE=$expected" cresset-nginx-protocol:test
