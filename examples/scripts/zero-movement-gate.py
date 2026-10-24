#!/usr/bin/env python3
"""zero-movement-gate.py LABEL [options]  --  Ceph "nothing moved" gate for an in-place cephadm -> Rook adoption.

Run it where the `ceph` CLI, python3 and `osdmaptool` are available: a `cephadm shell` before Rook exists, the Rook toolbox
afterwards (kubectl -n rook-ceph exec -i deploy/rook-ceph-tools -- env GATE_EVID=/tmp/gate python3 - LABEL ... < zero-movement-gate.py).

It collects a summary of the cluster state (hashes and names only, never keys) into <evid>/<LABEL>/summary.json and, unless
LABEL == B0, compares it with <base>/summary.json. Exit 0 = every check PASS (WARN allowed); exit 1 = at least one FAIL = HOLD.
A FAIL means "a person looks at it", never "revert automatically".

What is compared (see the guide, Appendix B):
  placement hash  sha256 of every PG's raw/up/acting line from `osdmaptool --test-map-pgs-dump-all`
                  (NOT --test-map-pgs-dump: acting only and hidden by pg_temp). Includes OSD up state: only meaningful when all OSDs are up.
  crush/rules/tree hashes, a pools hash over stable pool fields, the mon. key hash, per-entity sha256(key):sha256(caps),
  monmap epoch and address vectors, cipher fields, OSD counts/flags/blocklist count, mgr state, MDS states, the config dump
  (values of secret-looking rows are hashed), and the cephadm orchestrator state while it exists.

Options:
  --evid DIR              evidence root (default $GATE_EVID or ./gate-evidence); --base defaults to <evid>/B0
  --quick                 health, quorum, monmap, PG state only (cheap; for loops such as the mon rename)
  --allow-health A,B      health check codes expected in this step (added to --default-health)
  --default-health A,B    standing, explained health codes (default: none)
  --mon-plan FILE         JSON {"<mon ip>": ["<cephadm name>", "<rook letter>"], ...} for the rename phase; without it the
                          mon names and addresses must equal the base exactly
  --renamed a,b,...       letters whose mon must already carry the letter name (others must still carry the cephadm name);
                          use --renamed all after the last rename
  --epoch-delta K         monmap epoch must be exactly base+K (default: >= base)
  --allow-auth-add E1,E2  entities that may appear        --allow-auth-del E1,E2  entities that may vanish
  --allow-cfg RE          regex (full match on "section|mask|name") for config rows that may change; repeatable
  --allow-cipher-change   mon dump cipher fields may differ (Rook's first reconcile on Ceph >= 20.2.4)
  --allow-mgr-change      active mgr / standby set may differ
  --allow-mds-change      MDS daemon names/states may differ (a cephadm standby left, a failover happened)
  --orch-gone             `ceph orch` must be unavailable (after Rook's first reconcile)
  --allow-unmanaged       cephadm service unmanaged flags may differ
  --allow-blocklist       the OSD blocklist count may differ (mgr respawns add entries, entries expire)
  --allow-pool-change P   pool names whose stable fields may differ (e.g. .rgw.root after a CephObjectStore); repeatable.
                          Their crush_rule, size and min_size must still match.
"""
import argparse, hashlib, json, os, re, subprocess, sys

SECRET_NAME = re.compile(r"key|secret|password|token|jwt|multicluster|credential", re.I)
SECRET_VALUE = re.compile(r"^eyJ|AQ[A-Za-z0-9+/]{30,}")   # JWTs and cephx keys, whatever the row is called
POOL_FIELDS = ("pool", "size", "min_size", "crush_rule", "pg_num", "pg_placement_num", "type", "erasure_code_profile",
               "quota_max_bytes", "quota_max_objects", "flags_names", "options", "application_metadata")


def sh(args, tmo=90, js=False):
    r = subprocess.run(args, capture_output=True, text=True, timeout=tmo)
    if r.returncode != 0:
        raise RuntimeError("%s -> rc=%d %s" % (" ".join(args[:4]), r.returncode, r.stderr.strip()[:160]))
    return json.loads(r.stdout) if js else r.stdout


def ceph(*a, js=True, tmo=90):
    return sh(["ceph", *a] + (["-f", "json"] if js else []), tmo, js)


def h(s, n=16):
    return hashlib.sha256(s if isinstance(s, bytes) else str(s).encode()).hexdigest()[:n]


def nm(x):
    return x.get("name") if isinstance(x, dict) else x


def collect(quick):
    S = {}
    hd = ceph("health", "detail")
    S["health"] = {k: bool(v.get("muted")) for k, v in hd.get("checks", {}).items()}
    q = ceph("quorum_status")
    S["quorum"] = sorted(q["quorum_names"])
    S["leader"] = q["quorum_leader_name"]
    S["epoch"] = q["monmap"]["epoch"]
    S["mons"] = {m["name"]: sorted(a["addr"] for a in m["public_addrs"]["addrvec"]) for m in q["monmap"]["mons"]}
    ps = ceph("pg", "stat")
    ps = ps.get("pg_summary", ps)
    S["pg"] = {"num": ps.get("num_pgs"), "states": {s["name"]: s["num"] for s in ps.get("num_pg_by_state", [])},
               "degraded": ps.get("degraded_objects", 0), "misplaced": ps.get("misplaced_objects", 0),
               "unfound": ps.get("unfound_objects", 0)}
    if quick:
        return S
    md = ceph("mon", "dump")
    S["cipher"] = {}
    for k in ("auth_allowed_ciphers", "auth_preferred_cipher", "auth_service_cipher"):
        v = md.get(k)
        S["cipher"][k] = sorted(nm(i) for i in v) if isinstance(v, list) else nm(v)
    od = ceph("osd", "dump")
    S["osd"] = {"flags": od.get("flags"), "n": len(od["osds"]), "up": sum(1 for o in od["osds"] if o["up"]),
                "in": sum(1 for o in od["osds"] if o["in"]), "blocklist": len(od.get("blocklist", {})),
                "release": od.get("require_osd_release")}
    pools = {p["pool_name"]: {k: p.get(k) for k in POOL_FIELDS} for p in od["pools"]}
    S["pools"] = {n: h(json.dumps(v, sort_keys=True)) for n, v in pools.items()}
    S["pools_core"] = {n: [v["crush_rule"], v["size"], v["min_size"]] for n, v in pools.items()}
    S["pools_n"] = len(pools)
    S["nodelete"] = sum(1 for p in pools.values() if "nodelete" in (p["flags_names"] or ""))
    S["crush_hash"] = h(ceph("osd", "crush", "dump", js=False))
    S["rules_hash"] = h(ceph("osd", "crush", "rule", "dump", js=False))
    S["tree_hash"] = h(ceph("osd", "tree", js=False))
    sh(["ceph", "osd", "getmap", "-o", "/tmp/om.gate"])
    out = sh(["osdmaptool", "/tmp/om.gate", "--test-map-pgs-dump-all"], 300)
    lines = sorted(l for l in out.splitlines() if re.match(r"^[0-9]+\.[0-9a-f]+ raw", l))
    S["pgmap_n"] = len(lines)
    S["pgmap_hash"] = h("\n".join(lines))
    mg = ceph("mgr", "dump")
    S["mgr"] = {"active": mg.get("active_name"), "avail": mg.get("available"),
                "standbys": sorted(s["name"] for s in mg.get("standbys", [])), "modules": sorted(mg.get("modules", []))}
    try:
        fs = ceph("fs", "status")
        S["fs_clients"] = {str(c.get("fs")): c.get("clients") for c in fs.get("clients", [])}
        S["fs_mds"] = sorted("%s:%s" % (m.get("name"), m.get("state")) for m in fs.get("mdsmap", []))
        S["fs_names"] = sorted(str(c.get("fs")) for c in fs.get("clients", []))
    except Exception as e:
        S["fs_err"] = str(e)[:80]
    a = ceph("auth", "ls")["auth_dump"]
    S["auth"] = {e["entity"]: h(e.get("key", "")) + ":" + h(json.dumps(e.get("caps", {}), sort_keys=True), 8) for e in a}
    S["monkey"] = h(sh(["ceph", "auth", "get-key", "mon."]))
    S["config"] = {}
    for c in ceph("config", "dump"):
        v = str(c["value"])
        k = "%s|%s|%s" % (c["section"], c.get("mask", ""), c["name"])
        S["config"][k] = ("#" + h(v, 8)) if (SECRET_NAME.search(c["name"]) or SECRET_VALUE.search(v)) else v
    try:
        st = ceph("orch", "status")
        ol = ceph("orch", "ls", tmo=120)
        op = ceph("orch", "ps", tmo=120)
        S["orch"] = {"status": {k: st.get(k) for k in ("available", "backend", "paused")},
                     "svc": {s["service_name"]: bool(s.get("unmanaged")) for s in ol},
                     "ps_ids": sorted("%s@%s" % (d["daemon_name"], d["hostname"]) for d in op if d["daemon_type"] != "mon"),
                     "ps_bad": sorted(d["daemon_name"] for d in op if d.get("status_desc") != "running"),
                     "mon_running": sum(1 for d in op if d["daemon_type"] == "mon" and d.get("status_desc") == "running")}
    except Exception as e:
        S["orch"] = "UNAVAILABLE: " + str(e)[:80]
    return S


def compare(S, B, A):
    R = []

    def rec(ok, name, detail="", warn=False):
        R.append(("PASS" if ok else ("WARN" if warn else "FAIL"), name, detail))

    allowed = set(filter(None, (A.default_health + "," + A.allow_health).split(",")))
    bad = sorted(set(S["health"]) - allowed)
    rec(not bad, "health", "unexpected=%s present=%s" % (bad, sorted(S["health"])))

    # mons: every mon has two addresses (v2 + v1); names per IP as planned (or as in the base); full quorum
    got = {}
    for name, addrs in S["mons"].items():
        rec(len(addrs) == 2, "mon-addrvec %s" % name, str(addrs))
        for ip in {x.rsplit(":", 1)[0] for x in addrs}:
            got[ip] = name
    if A.mon_plan:
        plan = json.load(open(A.mon_plan))
        renamed = set(filter(None, A.renamed.split(",")))
        exp = {ip: (letter if ("all" in renamed or letter in renamed) else old) for ip, (old, letter) in plan.items()}
    else:
        exp = {}
        for name, addrs in B["mons"].items():
            for ip in {x.rsplit(":", 1)[0] for x in addrs}:
                exp[ip] = name
    rec(got == exp, "mon-names/IPs", "got=%s expected=%s" % (sorted(got.items()), sorted(exp.items())))
    rec(S["quorum"] == sorted(S["mons"]), "quorum (complete)", "quorum=%s leader=%s" % (S["quorum"], S["leader"]))
    d = S["epoch"] - B["epoch"]
    rec((d == A.epoch_delta) if A.epoch_delta is not None else (d >= 0), "monmap-epoch",
        "base=%s now=%s delta=%s" % (B["epoch"], S["epoch"], d))

    pg = S["pg"]
    notclean = {k: v for k, v in pg["states"].items() if not k.startswith("active+clean")}
    rec(not notclean and not pg["degraded"] and not pg["misplaced"] and not pg["unfound"], "pg-clean",
        "notclean=%s degraded=%s misplaced=%s" % (notclean, pg["degraded"], pg["misplaced"]))
    rec(pg["num"] == B["pg"]["num"], "pg-count", "base=%s now=%s (autoscaler? new pools?)" % (B["pg"]["num"], pg["num"]), warn=True)
    if A.quick:
        return R

    for k in ("pgmap_hash", "crush_hash", "rules_hash", "tree_hash", "pgmap_n", "nodelete", "pools_n", "monkey"):
        rec(S[k] == B[k], k, "base=%s now=%s" % (B[k], S[k]))
    # pools: every base pool unchanged (POOLRULE); allowed pools may change except rule/size/min_size
    ap = set(A.allow_pool_change)
    for p in sorted(set(B["pools"]) | set(S["pools"])):
        if p not in S["pools"] or p not in B["pools"]:
            rec(False, "pool %s" % p, "present in only one side")
        elif p in ap:
            rec(S["pools_core"][p] == B["pools_core"][p], "pool %s (rule,size,min_size)" % p,
                "base=%s now=%s" % (B["pools_core"][p], S["pools_core"][p]))
        elif S["pools"][p] != B["pools"][p]:
            rec(False, "pool %s" % p, "stable fields changed; core base=%s now=%s" % (B["pools_core"][p], S["pools_core"][p]))
    rec(True, "pools checked", "%d pools" % len(S["pools"]))

    o, ob = dict(S["osd"]), dict(B["osd"])
    if A.allow_blocklist:
        o.pop("blocklist"); ob.pop("blocklist")
    rec(o == ob, "osd-state/flags", "base=%s now=%s" % (ob, o))
    m, mb = S["mgr"], B["mgr"]
    if A.allow_mgr_change:
        rec(m["avail"], "mgr available", "base=%s now=%s" % (mb, m))
        rec(m["modules"] == mb["modules"], "mgr modules", "base=%s now=%s" % (mb["modules"], m["modules"]), warn=True)
    else:
        rec(m == mb, "mgr", "base=%s now=%s" % (mb, m))
    rec(S["cipher"] == B["cipher"] or A.allow_cipher_change, "cipher", "base=%s now=%s" % (B["cipher"], S["cipher"]))

    aa = set(filter(None, A.allow_auth_add.split(","))); ad = set(filter(None, A.allow_auth_del.split(",")))
    add = set(S["auth"]) - set(B["auth"]); dele = set(B["auth"]) - set(S["auth"])
    chg = sorted(e for e in set(S["auth"]) & set(B["auth"]) if S["auth"][e] != B["auth"][e])
    rec(not chg, "auth-changed (keys/caps)", str(chg))
    rec(add <= aa, "auth-added", "added=%s allowed=%s" % (sorted(add), sorted(aa)))
    rec(dele <= ad, "auth-removed", "removed=%s allowed=%s" % (sorted(dele), sorted(ad)))

    rx = [re.compile(r) for r in A.allow_cfg]
    keys = set(S["config"]) | set(B["config"])
    cdiff = sorted(k for k in keys if S["config"].get(k) != B["config"].get(k))
    badc = [k for k in cdiff if not any(r.fullmatch(k) for r in rx)]
    rec(not badc, "config-diff", "unexpected=%s allowed=%s" % (badc, sorted(set(cdiff) - set(badc))))

    if "fs_err" not in S and "fs_err" not in B:
        rec(S.get("fs_names") == B.get("fs_names"), "fs-names", "base=%s now=%s" % (B.get("fs_names"), S.get("fs_names")))
        rec(S["fs_mds"] == B["fs_mds"] or A.allow_mds_change, "mds-states", "base=%s now=%s" % (B["fs_mds"], S["fs_mds"]))
        for fsn, c in B["fs_clients"].items():
            rec(abs((S["fs_clients"].get(fsn) or 0) - (c or 0)) <= 3, "fs-clients %s" % fsn,
                "base=%s now=%s" % (c, S["fs_clients"].get(fsn)), warn=True)

    so, bo = S["orch"], B["orch"]
    if A.orch_gone:
        rec(isinstance(so, str), "orch-unavailable", str(so)[:100])
    elif isinstance(so, str) and isinstance(bo, str):
        rec(True, "orch", "unavailable on both sides (after Rook's first reconcile)")
    elif isinstance(so, dict) and isinstance(bo, dict):
        rec(so["ps_ids"] == bo["ps_ids"], "orch-daemons(non-mon)", "diff=%s" % sorted(set(so["ps_ids"]) ^ set(bo["ps_ids"])))
        rec(so["ps_bad"] == bo["ps_bad"], "orch-not-running", "base=%s now=%s" % (bo["ps_bad"], so["ps_bad"]))
        rec(so["mon_running"] == bo["mon_running"], "orch-mons-running", "base=%s now=%s" % (bo["mon_running"], so["mon_running"]))
        if not A.allow_unmanaged:
            rec(so["svc"] == bo["svc"], "orch-services/unmanaged",
                "diff=%s" % sorted(k for k in set(so["svc"]) | set(bo["svc"]) if so["svc"].get(k) != bo["svc"].get(k)))
    else:
        rec(False, "orch", "now=%s base=%s" % (str(so)[:60], str(bo)[:60]))
    return R


def main():
    p = argparse.ArgumentParser()
    p.add_argument("label")
    p.add_argument("--evid", default=os.environ.get("GATE_EVID", "./gate-evidence"))
    p.add_argument("--base", default=None)
    p.add_argument("--quick", action="store_true")
    p.add_argument("--allow-health", default="")
    p.add_argument("--default-health", default="")
    p.add_argument("--mon-plan", default=None)
    p.add_argument("--renamed", default="")
    p.add_argument("--epoch-delta", type=int, default=None)
    p.add_argument("--allow-auth-add", default="")
    p.add_argument("--allow-auth-del", default="")
    p.add_argument("--allow-cfg", action="append", default=[])
    p.add_argument("--allow-pool-change", action="append", default=[])
    p.add_argument("--allow-cipher-change", action="store_true")
    p.add_argument("--allow-mgr-change", action="store_true")
    p.add_argument("--allow-mds-change", action="store_true")
    p.add_argument("--orch-gone", action="store_true")
    p.add_argument("--allow-unmanaged", action="store_true")
    p.add_argument("--allow-blocklist", action="store_true")
    A = p.parse_args()
    A.base = A.base or os.path.join(A.evid, "B0")
    S = collect(A.quick)
    d = os.path.join(A.evid, A.label)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "summary.json"), "w") as f:
        json.dump(S, f, indent=1, sort_keys=True)
    if A.label == "B0":
        print("B0 collected: %d mons, %s pgs, %s pools, health=%s" % (len(S["mons"]), S["pg"]["num"], S.get("pools_n"), sorted(S["health"])))
        return 0
    with open(os.path.join(A.base, "summary.json")) as f:
        B = json.load(f)
    if not A.quick and "pgmap_hash" not in B:
        print("base %s is a --quick summary; take a full base first" % A.base)
        return 1
    R = compare(S, B, A)
    for st, name, detail in R:
        print("%-4s %-32s %s" % (st, name, detail[:300]))
    fails = [r for r in R if r[0] == "FAIL"]
    print("GATE %s: %s (%d fail, %d warn)" % (A.label, "HOLD" if fails else "PASS", len(fails), sum(1 for r in R if r[0] == "WARN")))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
