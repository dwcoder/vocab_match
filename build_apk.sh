#!/usr/bin/env bash
# Build a signed release APK for arm64 Android phones.
#
# Usage: ./build_apk.sh
#
# The version comes from pyproject.toml. Before a new release, change it
# there and commit. If a git tag v<version> already exists on an earlier
# commit, that version has been released and the script stops. If the tag is
# on the current commit, this is a rebuild of that release and it goes ahead.
# After a build of a new version it prints the commands to tag the release.
#
# The build number (Android's versionCode) is worked out from the version as
# major * 10000 + minor * 100 + patch, so 0.1.0 -> 100 and 2.1.0 -> 20100.
# Android only installs an APK as an update if this number is higher than
# the installed one, which holds as long as each release has a higher
# version. Minor and patch must stay below 100 for this to work.
#
# The keystore defaults to ~/keys/vocab-match.jks with the alias "upload".
# Set KEYSTORE or KEY_ALIAS to use something else. The password is asked for
# without echoing it; set FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD beforehand
# to skip the prompt (for example from a password manager).

set -euo pipefail

if [[ $# -ne 0 ]]; then
    echo "Usage: $0   (the version is read from pyproject.toml)" >&2
    exit 1
fi

cd "$(dirname "$0")"

keystore=${KEYSTORE:-$HOME/keys/vocab-match.jks}
key_alias=${KEY_ALIAS:-upload}

# ---------- Version ----------

version=$(uv run --quiet python -c \
    'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')

if [[ ! $version =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo "The version in pyproject.toml must look like 1.2.3, got '$version'" >&2
    exit 1
fi
# 10# reads the parts as decimal, so a part like "08" isn't taken as octal.
major=$((10#${BASH_REMATCH[1]}))
minor=$((10#${BASH_REMATCH[2]}))
patch=$((10#${BASH_REMATCH[3]}))
if (( minor > 99 || patch > 99 )); then
    echo "Minor and patch must be 99 or less, got '$version'" >&2
    exit 1
fi
build_number=$((major * 10000 + minor * 100 + patch))
if (( build_number < 1 )); then
    echo "The version must be above 0.0.0" >&2
    exit 1
fi

# ---------- Git checks ----------

tag=v$version
head=$(git rev-parse HEAD)
new_release=true
if tag_commit=$(git rev-parse --verify --quiet "refs/tags/$tag^{commit}"); then
    if [[ $tag_commit != "$head" ]]; then
        echo "$tag is already tagged on an earlier commit ($(git log -1 --format='%h %s' "$tag_commit"))." >&2
        echo "Change the version number in pyproject.toml first." >&2
        exit 1
    fi
    new_release=false
    echo "$tag is already tagged on this commit; rebuilding that release."
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "Warning: there are uncommitted changes. They will be in the APK but not in the tagged commit." >&2
fi

unpushed=0
if git rev-parse --verify --quiet '@{upstream}' >/dev/null; then
    unpushed=$(git rev-list --count '@{upstream}..HEAD')
fi

if [[ ! -f $keystore ]]; then
    echo "Keystore not found: $keystore" >&2
    exit 1
fi

# ---------- Build ----------

echo "Building version $version (build number $build_number)."

if [[ -z ${FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD:-} ]]; then
    read -rsp "Keystore password: " FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD
    echo
fi
export FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD

# arm64-v8a covers nearly every Android phone from the last several years.
# Legacy packaging stores the native libraries compressed, which roughly
# halves the download size.
uv run flet build apk \
    --arch arm64-v8a \
    --android-legacy-packaging \
    --build-version "$version" \
    --build-number "$build_number" \
    --android-signing-key-store "$keystore" \
    --android-signing-key-alias "$key_alias"

output=build/apk/vocab-match-$version.apk
cp build/apk/vocab-match.apk "$output"

echo
echo "Built $output ($(du -h "$output" | cut -f1)), build number $build_number."
echo
echo "Next steps:"
if (( unpushed > 0 )); then
    echo "  git push"
fi
if $new_release; then
    echo "  git tag -a $tag -m \"Version $version\""
    echo "  git push origin $tag"
fi
echo "  Then upload $output to the $tag release on GitHub."
echo "  To install on a phone connected by USB: adb install -r $output"
