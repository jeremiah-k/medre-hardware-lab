#!/bin/sh
# The generated override block owns the tail of the lab homeserver config.
# Only the generated lab config is supported; use a regular file, not a symlink.
set -eu
config=$1
overrides=$2
if [ ! -f "$config" ] || [ -L "$config" ]; then
	echo "expected a regular, non-symlink lab homeserver config" >&2
	exit 1
fi
# Refuse a malformed override source before touching the destination. Exactly
# one current marker, on the first line, makes the replacement idempotent.
if ! awk '
    NR == 1 { first = /^# Hardware-lab Synapse overrides([[:space:]]|$)/ }
    /^# (interop-lab|Hardware-lab) Synapse overrides([[:space:]]|$)/ { count++ }
    END { exit !(first && count == 1 && NR > 1) }
' "$overrides"; then
	echo "expected one current Synapse override block" >&2
	exit 1
fi
temp=$(mktemp "${config}.XXXXXX")
trap 'rm -f "$temp"' 0
# Preserve the existing file mode/ownership when replacing its contents.
cp -p "$config" "$temp"
awk '/^# (interop-lab|Hardware-lab) Synapse overrides([[:space:]]|$)/ {exit} {print}' "$config" >"$temp"
cat "$overrides" >>"$temp"
if cmp -s "$config" "$temp"; then
	exit 0
else
	rc=$?
	# cmp status 1 means different; other errors are not permission to replace.
	if [ "$rc" -ne 1 ]; then
		echo "unable to compare Synapse configuration" >&2
		exit "$rc"
	fi
fi
mv -f "$temp" "$config"
echo "overrides reapplied"
