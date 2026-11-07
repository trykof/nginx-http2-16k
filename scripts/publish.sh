#!/bin/sh
set -eu
: "${REPOSITORY:?Set REPOSITORY to owner/repository}"
: "${REVISION:?Set REVISION to the full Git commit}"
image="ghcr.io/$(printf '%s' "$REPOSITORY" | tr '[:upper:]' '[:lower:]')"
tag="1.30.6-16k-r1-${REVISION}"
# Refuse to overwrite an existing tag, including on workflow reruns.
for arch in amd64 arm64; do
    if docker buildx imagetools inspect "$image:$tag-$arch" > /dev/null 2>&1; then
        echo "Image tag already exists: $image:$tag-$arch" >&2
        exit 1
    fi
done
for arch in amd64 arm64; do
    docker load -i "artifacts/validated-$arch/image.tar"
    docker tag "cresset-nginx:$arch" "$image:$tag-$arch"
    docker push "$image:$tag-$arch"
done
docker buildx imagetools create -t "$image:$tag" -t "$image:sha-$REVISION" \
    "$image:$tag-amd64" "$image:$tag-arm64"
digest=$(docker buildx imagetools inspect "$image:$tag" --format '{{.Manifest.Digest}}')
test -n "$digest"
printf '%s@%s\n' "$image" "$digest" | tee artifacts/published.txt
if test -n "${GITHUB_STEP_SUMMARY:-}"; then
    cat artifacts/published.txt >> "$GITHUB_STEP_SUMMARY"
fi
# Pull the registry artifact and test it independently after publication.
docker pull --platform linux/amd64 "$image@$digest"
scripts/test.sh "$image@$digest"
