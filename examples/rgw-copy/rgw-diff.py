# Compare two sanitized snapshots from rgw-snap.py; PASS/FAIL per check; exit 1 on any FAIL.
#   python3 -I rgw-diff.py snap-old.json snap-new.json [--skip-users u1,u2] [--skip-buckets b1,b2] [--blank-placement u1,u2]
# Expected, not failures: users/buckets you chose not to copy (pass them as skips); Rook's own users present only in the new
# zone; stats_objects/bytes differing on versioned buckets when only current versions were copied.
import argparse, json, sys

ap = argparse.ArgumentParser()
ap.add_argument("old"); ap.add_argument("new")
ap.add_argument("--skip-users", default="")
ap.add_argument("--skip-buckets", default="")
ap.add_argument("--blank-placement", default="", help="users whose default_placement was blanked on copy")
A = ap.parse_args()
old, new = (json.load(open(p, encoding="utf-8")) for p in (A.old, A.new))
SKIP_USERS = set(filter(None, A.skip_users.split(",")))
SKIP_BUCKETS = set(filter(None, A.skip_buckets.split(",")))
BLANK = set(filter(None, A.blank_placement.split(",")))
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    fails += 0 if ok else 1


for uid, u in sorted(old["users"].items()):
    if uid in SKIP_USERS:
        continue
    n = new["users"].get(uid)
    if n is None:
        check(f"user {uid} present", False)
        continue
    for k in ("display_name", "email", "suspended", "max_buckets", "system", "admin", "caps", "op_mask",
              "user_quota", "bucket_quota", "key_fps", "secret_fps", "swift_keys", "subusers"):
        check(f"user {uid} {k}", u[k] == n[k], f"old={u[k]} new={n[k]}")
    exp_pl = "" if uid in BLANK else u["default_placement"]
    check(f"user {uid} default_placement", n["default_placement"] == exp_pl, f"new={n['default_placement']!r}")

for b, o in sorted(old["buckets"].items()):
    if b in SKIP_BUCKETS:
        continue
    n = new["buckets"].get(b)
    if n is None:
        check(f"bucket {b} present", False)
        continue
    for k in ("owner", "flags", "acl_owner", "acl_grants", "lifecycle", "quota"):
        check(f"bucket {b} {k}", o[k] == n[k], f"old={o[k]} new={n[k]}")
    check(f"bucket {b} placement default", n["placement"] in ("default-placement", ""), f"new={n['placement']}")
    check(f"bucket {b} attrs", set(o["attrs"]) <= set(n["attrs"]), f"old={o['attrs']} new={n['attrs']}")

extra = sorted(set(new["buckets"]) - set(old["buckets"]))
check("no unexpected buckets in new", not extra, f"extra={extra}")
check("old zone is still the realm-less legacy zone", not old["zone_get"]["realm_id"], str(old["zone_get"]))
print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
