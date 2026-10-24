# Sanitized JSON snapshot of one RGW zone: users (access and secret keys as sha256[:12] FINGERPRINTS only), quotas, caps,
# flags; buckets (owner, flags, placement, ACL grants, lifecycle rules, object counts/bytes from bucket stats).
# Diff two snapshots with rgw-diff.py. Never prints a secret.
#
# Run INSIDE the Rook toolbox:
#   kubectl -n rook-ceph exec -i deploy/rook-ceph-tools -- python3 - old < rgw-snap.py > snap-old.json
#   kubectl -n rook-ceph exec -i deploy/rook-ceph-tools -- python3 - new < rgw-snap.py > snap-new.json
# Adjust the zone selectors below.
import hashlib, json, subprocess, sys

ZONE = sys.argv[1] if len(sys.argv) > 1 else "old"
SELECTORS = {
    "old": ["--rgw-zonegroup=default", "--rgw-zone=default"],          # realm-less legacy zone
    "new": ["--rgw-realm=s3", "--rgw-zonegroup=s3", "--rgw-zone=s3"],  # the Rook store
}
if ZONE not in SELECTORS:
    sys.exit("usage: old|new")
SEL = SELECTORS[ZONE]


def rgw(*args, ok_fail=False):
    p = subprocess.run(["radosgw-admin", *args, *SEL], capture_output=True, text=True)
    if p.returncode != 0:
        if ok_fail:
            return None
        sys.exit(f"radosgw-admin {' '.join(args)} failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def fp(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


out = {"zone": ZONE, "zone_get": None, "users": {}, "buckets": {}}
z = rgw("zone", "get")
out["zone_get"] = {"id": z["id"], "name": z["name"], "realm_id": z.get("realm_id", "")}

for uid in rgw("user", "list"):
    u = rgw("user", "info", f"--uid={uid}")
    out["users"][uid] = {
        "display_name": u["display_name"],
        "email": u.get("email", ""),
        "suspended": u["suspended"],
        "max_buckets": u["max_buckets"],
        "system": u.get("system"),
        "admin": u.get("admin"),
        "caps": u["caps"],
        "default_placement": u["default_placement"],
        "op_mask": u.get("op_mask"),
        "user_quota": u["user_quota"],
        "bucket_quota": u["bucket_quota"],
        "key_fps": sorted(fp(k["access_key"]) for k in u["keys"]),
        "secret_fps": sorted(fp(k["secret_key"]) for k in u["keys"]),   # fingerprint only: proves the secret was copied unchanged
        "swift_keys": len(u.get("swift_keys", [])),
        "subusers": len(u.get("subusers", [])),
    }

for b in rgw("bucket", "list"):
    st = rgw("bucket", "stats", f"--bucket={b}")
    usage = st.get("usage", {})
    md = rgw("metadata", "get", f"bucket.instance:{b}:{st['id']}")
    bi = md["data"]["bucket_info"]
    attrs = sorted(a["key"] for a in md["data"].get("attrs", []))
    pol = rgw("policy", f"--bucket={b}", ok_fail=True) or {}
    grants = sorted((g.get("id", ""), g["grant"]["permission"]["flags"]) for g in pol.get("acl", {}).get("grant_map", []))
    lc = rgw("lc", "get", f"--bucket={b}", ok_fail=True)
    rules = None
    if lc:
        rules = sorted(
            [r["rule"]["id"], r["rule"]["status"], r["rule"]["prefix"], r["rule"]["expiration"]["days"],
             r["rule"]["noncur_expiration"]["days"], r["rule"]["mp_expiration"]["days"], r["rule"]["dm_expiration"]]
            for r in lc.get("rule_map", []))
    out["buckets"][b] = {
        "owner": st["owner"],
        "flags": bi.get("flags"),                    # 1 suspended, 2 versioned, 4 versioning suspended, 0x20 object lock
        "placement": st.get("placement_rule"),
        "attrs": attrs,
        "acl_owner": pol.get("owner", {}).get("id"),
        "acl_grants": grants,
        "lifecycle": rules,
        "quota": st.get("bucket_quota", {}).get("enabled"),
        "stats_objects": sum(v.get("num_objects", 0) for v in usage.values()),   # includes noncurrent versions; gate on rclone size instead
        "stats_bytes": sum(v.get("size_actual", 0) for v in usage.values()),
    }

json.dump(out, sys.stdout, indent=1, sort_keys=True)
