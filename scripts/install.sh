#!/bin/sh
# Install or upgrade from source. This script installs no prerequisite tools.
set -eu

fail() { printf '%s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] ||
    fail 'Use an Apple Silicon Mac and run this command from a native terminal.'
xcode-select -p >/dev/null 2>&1 ||
    fail 'Install the Command Line Tools with xcode-select --install, then run this command again.'
command -v python3 >/dev/null 2>&1 &&
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1 ||
    fail 'Install Python 3.12 or newer from python.org and put python3 on PATH, then run this command again.'
curl -fsIL --connect-timeout 10 --max-time 30 https://github.com >/dev/null 2>&1 ||
    fail 'Allow HTTPS access to GitHub, then run this command again.'
curl -fsIL --connect-timeout 10 --max-time 30 https://pypi.org/simple/ >/dev/null 2>&1 ||
    fail 'Allow HTTPS access to PyPI, then run this command again.'

repository=https://github.com/Simon-AI-coding/GPT-VoiceCoding
printf '%s\n' 'Preparing the source checkout…'
product=${repository##*/}
source_directory="$HOME/Library/Application Support/$product/source"
mkdir -p "$(dirname "$source_directory")"
if [ -d "$source_directory/.git" ]; then
    git -C "$source_directory" pull --ff-only
else
    git clone "$repository.git" "$source_directory"
fi

"$source_directory/scripts/build-app.sh"
bundle_name=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleName' "$source_directory/shell/Resources/Info.plist")
built_app="$source_directory/shell/.build/$bundle_name.app"
bundle_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$built_app/Contents/Info.plist")
installed_app="/Applications/$bundle_name.app"

app_is_running() {
    # Query running processes, not Launch Services: a first install has no registered app.
    running=$(osascript -l JavaScript -e '
        ObjC.import("AppKit");
        function run(argv) {
            return $.NSRunningApplication.runningApplicationsWithBundleIdentifier(argv[0]).count > 0;
        }
    ' "$bundle_id") || fail 'Could not check the running app. Try the installation again.'
    [ "$running" = true ]
}

printf '%s\n' 'Checking whether an earlier version is running…'
if app_is_running; then
    printf '%s\n' 'Closing the running app before upgrading…'
    osascript -e "tell application id \"$bundle_id\" to quit" >/dev/null 2>&1 ||
        fail 'Quit GPT-VoiceCoding, then run this command again.'
    attempts=0
    while app_is_running; do
        attempts=$((attempts + 1))
        [ "$attempts" -lt 30 ] || fail 'Quit GPT-VoiceCoding, then run this command again.'
        sleep 1
    done
fi

# Move the old bundle aside; ditto must not merge obsolete files into a new build.
staging=$(mktemp -d "/Applications/.$product.XXXXXX")
printf '%s\n' 'Installing the app in Applications…'
ditto "$built_app" "$staging/$bundle_name.app"
if [ -e "$installed_app" ]; then
    mv "$installed_app" "$staging/previous.app"
fi
if ! mv "$staging/$bundle_name.app" "$installed_app"; then
    if [ -e "$staging/previous.app" ]; then mv "$staging/previous.app" "$installed_app"; fi
    fail 'The app could not be placed in Applications; check its permissions and run this command again.'
fi
# This directory is created by mktemp above and holds only this install's bundles.
rm -rf "$staging"
printf '%s\n' 'Installation complete. Opening GPT-VoiceCoding…'
open "$installed_app"
