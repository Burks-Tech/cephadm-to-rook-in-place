# Zero-movement gate

`zero-movement-gate.py` takes a hashed summary of the cluster state and compares it with a baseline. It is a generalized version of the gate we ran at every step of the migration. It prints one PASS/WARN/FAIL line per check and exits 1 on any FAIL (meaning: hold and look, not revert).

It needs `ceph`, `python3` and `osdmaptool`, so run it in a `cephadm shell` (before Rook) or the Rook toolbox (after). It never writes keys: auth entities are stored as `sha256(key):sha256(caps)` prefixes, and config values whose name or content looks secret are hashed.

```bash
# in the Rook toolbox
TB="kubectl -n rook-ceph exec -i deploy/rook-ceph-tools --"
$TB env GATE_EVID=/tmp/gate python3 - B0 < zero-movement-gate.py                       # baseline
$TB env GATE_EVID=/tmp/gate python3 - B0b < zero-movement-gate.py                      # 15 min later: must PASS (gate stability)

# a host window, after adoption (OSDs all up again), allowing the expected noise
$TB env GATE_EVID=/tmp/gate python3 - H7 --base /tmp/gate/B5 \
    --allow-health OSD_FLAGS --allow-blocklist --allow-mgr-change --allow-mds-change \
    --allow-cfg 'osd\|host:ceph-4\|osd_memory_target' < zero-movement-gate.py

# during the mon renames (before Rook exists, in a cephadm shell with the plan file present)
python3 zero-movement-gate.py R3 --mon-plan mon-plan.example.json --renamed a,b,c --epoch-delta 6

# after a CephObjectStore was created (new s3.rgw.* pools and rules are expected; .rgw.root pg_num changes)
... --allow-pool-change .rgw.root  (new pools still FAIL as "present in only one side": take a fresh baseline after review)
```

The toolbox `/tmp` is lost when the pod restarts: copy the summaries out (they contain no secrets) after each step, and keep the evidence somewhere other than the cluster you are changing.

Things the gate cannot see: anything outside Ceph (CSI pod restarts, client errors, kernel `libceph` messages, host daemon restarts). We checked those separately at each gate.
