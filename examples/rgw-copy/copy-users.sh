#!/bin/bash
# Copy RGW users (uid, display name, access/secret keys, quotas, caps, max_buckets, suspended flag) from a realm-less legacy
# zone into a new Rook zone, with `metadata get` piped straight into `metadata put`. The JSON is never printed or written to disk.
#
# Run INSIDE the Rook toolbox:
#   kubectl -n rook-ceph exec -i deploy/rook-ceph-tools -- bash -s -- user1 user2 ... < copy-users.sh
#
# Adjust OLD/NEW to your zone names. Users whose default_placement does not exist in the new zonegroup must have it blanked
# (see the python filter; list them in BLANK_PLACEMENT).
set -euo pipefail
OLD=(--rgw-zonegroup=default --rgw-zone=default)
NEW=(--rgw-realm=s3 --rgw-zonegroup=s3 --rgw-zone=s3)
BLANK_PLACEMENT="${BLANK_PLACEMENT:-}"           # space-separated uids
USERS=("$@")
[ ${#USERS[@]} -gt 0 ] || { echo "usage: copy-users.sh uid [uid ...]"; exit 2; }

for u in "${USERS[@]}"; do
  if radosgw-admin user info --uid="$u" "${NEW[@]}" >/dev/null 2>&1; then
    echo "SKIP $u: already exists in the new zone"; continue
  fi
  radosgw-admin metadata get "user:$u" "${OLD[@]}" \
    | BLANK="$BLANK_PLACEMENT" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
if d["data"]["user_id"] in os.environ.get("BLANK", "").split():
    d["data"]["default_placement"] = ""
    d["data"]["placement_tags"] = []
json.dump(d, sys.stdout)' \
    | radosgw-admin metadata put "user:$u" "${NEW[@]}"
  echo "PUT  $u"
done

echo "--- check: every copied user resolves BY ACCESS KEY in the new zone (key index written); fingerprints only"
for u in "${USERS[@]}"; do
  ak=$(radosgw-admin user info --uid="$u" "${OLD[@]}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["keys"][0]["access_key"])')
  got=$(radosgw-admin user info --access-key="$ak" "${NEW[@]}" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["user_id"])' || echo MISSING)
  fp=$(printf %s "$ak" | sha256sum | cut -c1-12)
  [ "$got" = "$u" ] && echo "OK   $u key $fp" || echo "FAIL $u key $fp -> $got"
done
