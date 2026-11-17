#!/bin/sh
# The generated override block owns the tail of the lab homeserver config.
set -eu
config=$1
overrides=$2
temp=$(mktemp "${config}.XXXXXX")
trap 'rm -f "$temp"' 0
# Preserve the existing file mode/ownership when replacing its contents.
cp -p "$config" "$temp"
awk '/^# (interop-lab|Hardware-lab) Synapse overrides([[:space:]]|$)/ {exit} {print}' "$config" >"$temp"
cat "$overrides" >>"$temp"
if ! cmp -s "$config" "$temp"; then
	mv -f "$temp" "$config"
	echo "overrides reapplied"
fi
