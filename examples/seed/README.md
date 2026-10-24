# Seed objects

Rook reads its cluster identity from two objects in the `rook-ceph` namespace. For an adoption they must exist, with the live cluster's values, **before** the CephCluster CR is created. If Rook cannot read them, it bootstraps a brand-new cluster with a new fsid.

- `rook-ceph-mon-endpoints.yml`: the mon letters, addresses and node mapping. Safe to keep in Git (no secrets), but it must match the live monmap exactly.
- The `rook-ceph-mon` Secret: **never** commit it, never put its values on a command line. Create it from files, as below.

```bash
umask 077
d=$(mktemp -d)
# fetch each value from a cephadm admin host into a file, stripping newlines; never echo
ssh <admin-host> "sudo cephadm shell -- ceph fsid"                       | tr -d '\r\n' > "$d/fsid"
ssh <admin-host> "sudo cephadm shell -- ceph auth get-key mon."          | tr -d '\r\n' > "$d/mon-secret"
ssh <admin-host> "sudo cephadm shell -- ceph auth get-key client.admin"  | tr -d '\r\n' > "$d/ceph-secret"
printf %s client.admin > "$d/ceph-username"
wc -c "$d"/*                                                     # check lengths only
sha256sum "$d/mon-secret" | cut -c1-16                           # compare with your baseline's mon. key hash

kubectl -n rook-ceph create secret generic rook-ceph-mon --type=kubernetes.io/rook \
  --from-file=fsid="$d/fsid" --from-file=mon-secret="$d/mon-secret" \
  --from-file=ceph-username="$d/ceph-username" --from-file=ceph-secret="$d/ceph-secret"

kubectl -n rook-ceph get secret rook-ceph-mon -o go-template='{{range $k,$v := .data}}{{$k}} {{len $v}}{{"\n"}}{{end}}'
shred -u "$d"/* 2>/dev/null || rm -f "$d"/*; rmdir "$d"
```

Note: `mon-secret` is the `mon.` key, not the admin key (Rook `cluster_info.go`).

Before applying the ConfigMap, compare it with the live monmap:

```bash
ceph mon dump -f json | python3 -c 'import json,sys; m=json.load(sys.stdin)
print(",".join(sorted("%s=%s:6789" % (x["name"], x["public_addrs"]["addrvec"][0]["addr"].rsplit(":",1)[0]) for x in m["mons"])))'
```

The output must equal the `data` value (sorted), the mapping must have one entry per mon, and `maxMonId` must be the 0-based index of the highest letter.
