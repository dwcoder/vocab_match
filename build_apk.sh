#!/usr/bin/env bash
# Build a signed release APK for arm64 Android phones.
#
# Usage:   ./build_apk.sh VERSION
# Example: ./build_apk.sh 0.1.0
#
# VERSION is what users see (match it to the git tag, v0.1.0).
# The build number (Android's versionCode) is worked out from it as
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

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 VERSION   (for example: $0 0.1.0)" >&2
    exit 1
fi

version=$1
keystore=${KEYSTORE:-$HOME/keys/vocab-match.jks}
key_alias=${KEY_ALIAS:-upload}

if [[ ! $version =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo "VERSION must look like 1.2.3, got '$version'" >&2
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
    echo "Version must be above 0.0.0" >&2
    exit 1
fi
if [[ ! -f $keystore ]]; then
    echo "Keystore not found: $keystore" >&2
    exit 1
fi

cd "$(dirname "$0")"

project_version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
if [[ $project_version != "$version" ]]; then
    echo "Warning: pyproject.toml says version $project_version, building $version." >&2
fi

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
echo "Upload that file to the v$version release."
