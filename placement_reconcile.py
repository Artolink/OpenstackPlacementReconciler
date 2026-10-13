#!/usr/bin/env python3
"""
placement_reconcile.py - reconcile Placement vs Nova vs libvirt, hypervisor by hypervisor.

Source of truth: virsh > Nova DB (cells + mappings) > Placement.

Pre-flight check (all commands): the script stops if it finds
  - open migrations not stuck for more than --stale-hours (queued/preparing/running/post-migrating...),
  - cold migrations in 'finished' state with the instance in VERIFY_RESIZE: they must be confirmed
    (openstack server resize confirm) or reverted before going on, whatever their age.
    Orphaned 'finished' migrations (instance not in VERIFY_RESIZE) block only while recent; after
    --stale-hours they become stuck migrations,
  - instances with a task_state that moves/creates/frees allocations (build, resize, migrate, shelve,
    delete, rebuild...) not stuck for more than --stale-hours.
  The report repeats the check at the end (if something started in the meantime it is marked INVALID)
  and stops if the virsh inventory is older than migrations that finished after it was collected.
  --ignore-activity skips the check: the report becomes indicative only and every generated command
  is commented out.

Commands:
  collect-virsh  Asks for SSH user and password and collects the libvirt domain inventory of every
                 hypervisor -> virsh_inventory.txt  (uuid host state). Queries the hosts in parallel
                 (--parallel, default 10) after checking the credentials on the first one.
  report         READ-ONLY (default). For every hypervisor compares allocations and instances,
                 checks the deletion candidates against the cell DB and the mappings,
                 and with --virsh compares everything with libvirt.
                 Writes report.json, backup_allocations.json, fix_commands.sh (to review).
  delete         Removes ALL allocations of the given consumers (confirmed A2 only,
                 or NO_HOST with --no-host).
                 Re-checks Nova API, migrations, cell DB, build_requests and virsh.
                 Requires DB access and a virsh inventory that covers every hypervisor.
  restore        Restores the allocations of one or more consumers from a backup file.

Classes:
  A1            allocation on this host, instance alive and running elsewhere -> heal --force
  A2            instance doesn't exist: API 404, deleted/shadow in the DB, no domain -> delete
  NO_HOST       existing instance without a hypervisor (SHELVED_OFFLOADED or ERROR,
                no task_state, no libvirt domain) with a leaked allocation
                                                                              -> delete --no-host
  B             instance on the host without an allocation on its RP          -> heal
  B_ERROR       like B but in ERROR                                           -> virsh, by hand
  A1_CHECK / B_CHECK  Nova and virsh disagree on the host, instance without a host
                in an unexpected state, or stuck task_state                   -> by hand
  DB_ALIVE      API 404 but alive in the cell DB: if global.mapping reports it as
                mapping MISSING -> map_instances; if WRONG (wrong cell) -> fix_mappings.sql;
                if the mapping is correct the cause of the 404 must be found by hand
  VIRSH_ALIVE   unknown to Nova but the domain exists                         -> by hand
  UNKNOWN       found neither in the API nor in the DB                        -> by hand
  NOVA_VIRSH    Nova and Placement agree but virsh doesn't                    -> by hand
  STUCK_MIGRATION  allocation of an open migration stuck for more than
                --stale-hours (default 24), orphaned 'finished' included (instance not in
                VERIFY_RESIZE): the record must be closed in the DB (close_migrations.sql),
                then the allocation becomes A2
  UNCERTAIN     GET /servers answered with an error other than 404 (500, 503...)
                                                                              -> retry / by hand
  STUCK_OPERATION  instance with a task_state stuck for more than --stale-hours -> by hand
  INCOMPLETE_DELETE  instance with terminated_at set and a failed delete in its history
                (owner's delete never completed)                              -> openstack server delete

DB access: the script runs a small Python program inside the nova_api container
(which already has the configuration and the drivers), so it never handles DB passwords. Default:
  --db-exec "docker exec -i nova_api python3 -"
From another machine:  --db-ssh <controller>   (asks for user and password, uses sudo if not root)
Without the DB (not recommended): --no-db

Examples:
  python3 placement_reconcile.py collect-virsh            # asks for user and password
  python3 placement_reconcile.py collect-virsh --user myuser
  python3 placement_reconcile.py collect-virsh --hosts-file hypervisors.txt --ignore-activity  # no openstack CLI
  python3 placement_reconcile.py report --virsh virsh_inventory.txt
  python3 placement_reconcile.py report --virsh virsh_inventory.txt --db-ssh controller01
  python3 placement_reconcile.py report --host 'compute0[38]' --virsh virsh_inventory.txt
  python3 placement_reconcile.py delete --virsh virsh_inventory.txt @reconcile-<ts>/a2_consumers.txt
  python3 placement_reconcile.py delete --no-host --virsh virsh_inventory.txt <uuid>
  python3 placement_reconcile.py restore placement-backup/alloc-<uuid>-<ts>.json <uuid>
"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

PLACEMENT_MV = "placement 1.28"
COMPUTE_MV = "2.59"
DONE_MIGRATION = {"completed", "confirmed", "reverted", "error", "failed", "done", "cancelled"}
HYP = "OS-EXT-SRV-ATTR:hypervisor_hostname"
CELL0 = "00000000-0000-0000-0000-000000000000"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PY = "python3 placement_reconcile.py"
DEFAULT_DB_EXEC = "docker exec -i nova_api python3 -"
# States in which an instance without a hypervisor must not have allocations in placement.
NOHOST_OK = {"SHELVED_OFFLOADED", "ERROR"}


def cli_opts(args):
    """DB and virsh access options used by the report, to repeat in the generated commands."""
    o = []
    if getattr(args, "no_db", False):
        o.append("--no-db")
    elif getattr(args, "db_ssh", None):
        o += ["--db-ssh", args.db_ssh]
        if args.db_user:
            o += ["--db-user", args.db_user]
        if args.db_key:
            o.append("--db-key")
        if args.db_sudo is True:
            o.append("--db-sudo")
        elif args.db_sudo is False:
            o.append("--no-db-sudo")
        if args.nova_container != "nova_api":
            o += ["--nova-container", args.nova_container]
    elif getattr(args, "db_exec", DEFAULT_DB_EXEC) != DEFAULT_DB_EXEC:
        o += ["--db-exec", args.db_exec]
    if getattr(args, "virsh", None):
        o += ["--virsh", args.virsh]
    return " ".join(shlex.quote(x) for x in o)


MANUAL = {"B_ERROR", "A1_CHECK", "B_CHECK", "DB_ALIVE", "VIRSH_ALIVE", "UNKNOWN",
          "NOVA_VIRSH", "UNCERTAIN"}


def now():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def log(msg):
    print(msg, file=sys.stderr)


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except FileNotFoundError:
        sys.exit(f"Command '{cmd[0]}' not found. report/delete/restore need the openstack CLI "
                 "with admin credentials (e.g. kolla-ansible venv + admin-openrc.sh).\n"
                 "collect-virsh can run without it: pass the hosts with --hosts or --hosts-file.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"'{' '.join(cmd)}' failed: {e.stderr.strip()[:500]}")


def endpoint(service, interface):
    cmd = ["openstack", "endpoint", "list", "--service", service, "--interface", interface,
           "-f", "value", "-c", "URL"]
    region = os.environ.get("OS_REGION_NAME")
    if region:
        cmd += ["--region", region]
    out = sorted({l.strip() for l in sh(cmd).splitlines() if l.strip()})
    if not out:
        sys.exit(f"No '{interface}' endpoint for service '{service}'"
                 + (f" in region {region}" if region else ""))
    if len(out) > 1:
        sys.exit(f"Found {len(out)} '{interface}' endpoints for '{service}': {', '.join(out)}\n"
                 "Set OS_REGION_NAME (or PLACEMENT_URL / COMPUTE_URL) to pick the right one.")
    return re.sub(r"/[%$]\(tenant_id\)s$", "", out[0]).rstrip("/")


# ---------------------------------------------------------------- API

class API:
    def __init__(self, interface):
        self.token = os.environ.get("OS_TOKEN") or sh(
            ["openstack", "token", "issue", "-f", "value", "-c", "id"])
        self.placement = os.environ.get("PLACEMENT_URL") or endpoint("placement", interface)
        self.compute = os.environ.get("COMPUTE_URL") or endpoint("compute", interface)
        log(f"Endpoints: compute={self.compute}  placement={self.placement}")

    def _call(self, method, url, headers, body=None):
        h = {"X-Auth-Token": self.token, "Accept": "application/json", **headers}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw.decode(errors="replace")
            return e.code, payload

    def placement_call(self, method, path, body=None):
        return self._call(method, self.placement + path,
                          {"OpenStack-API-Version": PLACEMENT_MV}, body)

    def compute_call(self, method, path, body=None):
        return self._call(method, self.compute + path,
                          {"OpenStack-API-Version": f"compute {COMPUTE_MV}",
                           "X-OpenStack-Nova-API-Version": COMPUTE_MV}, body)

    def compute_list(self, path, key, marker_field="id", limit=500):
        items, marker = [], None
        sep = "&" if "?" in path else "?"
        while True:
            q = f"{path}{sep}limit={limit}" + (f"&marker={marker}" if marker else "")
            st, d = self.compute_call("GET", q)
            if st != 200:
                sys.exit(f"GET {q} -> HTTP {st}: {d}")
            page = d[key]
            items += page
            if len(page) < limit:
                return items
            marker = page[-1][marker_field]


def all_migrations(api):
    return api.compute_list("/os-migrations", "migrations", marker_field="uuid")


def is_open(m):
    return (m.get("status") or "").lower() not in DONE_MIGRATION


def is_finished(m):
    """Cold migration (migrate/resize) in 'finished' state: copy done, record still open."""
    return (m.get("status") or "").lower() == "finished"


def awaiting_confirm(m, servers_by_id):
    """'finished' with the instance really in VERIFY_RESIZE: it must be confirmed or reverted
    (resize confirm/revert). A 'finished' whose instance is NOT in VERIFY_RESIZE (or no longer
    exists) is an orphaned record: resize confirm would be rejected, and the record must be closed
    like the other stuck migrations."""
    s = servers_by_id.get(m.get("instance_uuid"))
    return is_finished(m) and s is not None and s.get("status") == "VERIFY_RESIZE"


def active_migrations(api, migs=None):
    """Migrations not closed (any age, 'finished' included)."""
    migs = all_migrations(api) if migs is None else migs
    return {m["uuid"]: m for m in migs if is_open(m)}


def parse_ts(v):
    if not v:
        return None
    try:
        return datetime.datetime.strptime(str(v).replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def utc_stamp():
    return utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def local_to_utc(stamp):
    """'YYYYmmdd-HHMMSS' in this machine's local time -> naive UTC datetime."""
    try:
        t = datetime.datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return t.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def mig_src(m):
    return m.get("source_node") or m.get("source_compute")


def mig_dst(m):
    return m.get("dest_node") or m.get("dest_compute")


def mig_time(m):
    return parse_ts(m.get("updated_at")) or parse_ts(m.get("created_at"))


def stale_migrations(migs, stale_hours, servers_by_id):
    """Stuck migrations: open and stuck for more than stale_hours. Includes orphaned 'finished'
    migrations (instance not in VERIFY_RESIZE); excludes 'finished' migrations awaiting
    confirmation, which are closed with resize confirm/revert and never via SQL."""
    cut = utcnow() - datetime.timedelta(hours=stale_hours)
    out = {}
    for m in migs:
        t = mig_time(m)
        if not is_open(m) or t is None or t >= cut or awaiting_confirm(m, servers_by_id):
            continue
        out[m["uuid"]] = m
    return out


# ---------------------------------------------------------------- pre-flight check

# task_states that move, create or free allocations: while one is in progress,
# placement, Nova and libvirt can legitimately disagree.
ALLOC_TASK_STATES = {
    "scheduling", "block_device_mapping", "networking", "spawning",
    "rebuilding", "rebuild_block_device_mapping", "rebuild_spawning",
    "resize_prep", "resize_migrating", "resize_migrated", "resize_finish",
    "resize_reverting", "resize_confirming", "migrating",
    "shelving", "shelving_image_pending_upload", "shelving_image_uploading", "shelving_offloading",
    "unshelving", "deleting", "soft-deleting", "restoring",
}


def check_activity(api, stale_hours, migs=None, servers=None):
    """Migrations and operations in progress. Those stuck for more than stale_hours don't count
    as active (migrations become 'stuck', operations 'stuck operations')."""
    if migs is None:
        migs = all_migrations(api)
    if servers is None:
        servers = api.compute_list("/servers/detail?all_tenants=1", "servers")
    cut = utcnow() - datetime.timedelta(hours=stale_hours)
    by_id = {s["id"]: s for s in servers}
    act = {"migrations": [], "to_confirm": [], "operations": [], "stuck_operations": []}
    for m in migs:
        if not is_open(m):
            continue
        if awaiting_confirm(m, by_id):
            act["to_confirm"].append(m)
            continue
        # the other open ones (orphaned 'finished' included) block only while recent:
        # after stale_hours they become stuck migrations, handled by the report
        t = mig_time(m)
        if t is None or t >= cut:
            act["migrations"].append(m)
    for s in servers:
        if s.get("OS-EXT-STS:task_state") in ALLOC_TASK_STATES:
            t = parse_ts(s.get("updated"))
            act["operations" if (t is None or t >= cut) else "stuck_operations"].append(s)
    return act


def activity_blocking(act):
    return bool(act["migrations"] or act["to_confirm"] or act["operations"])


def describe_activity(act):
    L = []
    for m in act["migrations"]:
        L.append(f"MIGRATION     id={m.get('id')} {m.get('instance_uuid')} {m.get('migration_type')} "
                 f"{m.get('status')} {mig_src(m)} -> {mig_dst(m)} (created {str(m.get('created_at'))[:16]} UTC)")
    for m in act["to_confirm"]:
        L.append(f"TO_CONFIRM    id={m.get('id')} {m.get('instance_uuid')} {m.get('migration_type')} finished "
                 f"{mig_src(m)} -> {mig_dst(m)}: openstack server resize confirm {m.get('instance_uuid')}"
                 "  (or resize revert)")
    for s in act["operations"]:
        L.append(f"OPERATION     {s['id']} {s.get('status')} task_state={s.get('OS-EXT-STS:task_state')} "
                 f"on {s.get(HYP) or '-'} (updated {str(s.get('updated'))[:16]} UTC)")
    return L


def describe_stuck(act, stale_hours):
    return [f"STUCK_OPERATION {s['id']} {s.get('status')} task_state={s.get('OS-EXT-STS:task_state')} "
            f"on {s.get(HYP) or '-'} stuck since {str(s.get('updated'))[:16]} UTC (over {stale_hours}h)"
            for s in act["stuck_operations"]]


def preflight(api, args, phase, migs=None, servers=None):
    """Pre-flight check: exits if migrations or operations are in progress (unless --ignore-activity)."""
    act = check_activity(api, args.stale_hours, migs, servers)
    for l in describe_stuck(act, args.stale_hours):
        log("Note (does not block, check by hand): " + l)
    if not activity_blocking(act):
        log(f"Activity check ({phase}): no migration or operation in progress.")
        return act
    log(f"\nActivity check ({phase}): operations in progress:")
    for l in describe_activity(act):
        log("  " + l)
    if args.ignore_activity:
        log("\n--ignore-activity: going on anyway. The results are NOT reliable"
            + (" and the generated commands will be commented out." if phase == "report" else "."))
        return act
    sys.exit("\nStopping: with migrations or operations in progress the state of placement, Nova and\n"
             "libvirt changes during the analysis. Wait for them to finish (TO_CONFIRM ones must be\n"
             "confirmed or reverted) and run again, or use --ignore-activity (not recommended).")


def nohost_problems(s):
    """Reasons why an instance without a hypervisor is NOT a clean NO_HOST case ([] = ok)."""
    p = []
    if s.get(HYP):
        p.append(f"has a hypervisor ({s.get(HYP)})")
    if s.get("status") not in NOHOST_OK:
        p.append(f"status={s.get('status')}")
    if s.get("OS-EXT-STS:task_state"):
        p.append(f"task_state={s.get('OS-EXT-STS:task_state')}")
    return p


# ---------------------------------------------------------------- SSH

def _askpass_env(password):
    """SSH_ASKPASS helper: the password goes through an environment variable, never the command line."""
    import stat
    import tempfile
    fd, path = tempfile.mkstemp(prefix="reconcile-askpass-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$RECONCILE_SSH_PW"\n')
    os.chmod(path, stat.S_IRWXU)
    env = dict(os.environ, SSH_ASKPASS=path, SSH_ASKPASS_REQUIRE="force",
               DISPLAY=os.environ.get("DISPLAY", "reconcile:0"), RECONCILE_SSH_PW=password)
    return env, path


def ssh_setup(user, key, base="ssh -o ConnectTimeout=10", prompt_host="the hosts"):
    """Asks for the credentials (if needed) and prepares ssh command, env and password.
    Returns (cmd, env, askpass_path, password, user)."""
    import getpass
    user = user or input(f"SSH user for {prompt_host}: ").strip()
    password = None if key else getpass.getpass(f"SSH password for {user}: ")
    cmd = shlex.split(base) + ["-l", user, "-o", "StrictHostKeyChecking=accept-new"]
    env, askpass = None, None
    if password is not None:
        env, askpass = _askpass_env(password)
        cmd += ["-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1"]
    return cmd, env, askpass, password, user


# ---------------------------------------------------------------- DB (through the nova_api container)

DB_CODE = r'''
import configparser, json
from sqlalchemy import create_engine, text
CAND = __CANDIDATES__
CELL0 = "00000000-0000-0000-0000-000000000000"
cp = configparser.RawConfigParser(strict=False)
cp.read("/etc/nova/nova.conf")
def q(e, sql):
    with e.connect() as c:
        return [[x if x is None or isinstance(x, (int, str)) else str(x) for x in r]
                for r in c.execute(text(sql))]
api = create_engine(cp.get("api_database", "connection"))
cells = q(api, "SELECT id, uuid, name, database_connection FROM cell_mappings")
out = {"cells": [{"id": c[0], "uuid": c[1], "name": c[2]} for c in cells],
       "mappings": {u: cid for u, cid in q(api, "SELECT instance_uuid, cell_id FROM instance_mappings")},
       "build_requests": [r[0] for r in q(api, "SELECT instance_uuid FROM build_requests")],
       "live": [], "deleted": [], "shadow": [], "migrations": []}
IN = ",".join("'%s'" % u for u in CAND)
# cell0 too: it holds instances that were never scheduled (ERROR, no host) and can have leaked allocations.
for cid, cuuid, name, conn in cells:
    if "{" in (conn or ""):
        raise SystemExit("templated database_connection in cell %s: not supported" % cuuid)
    e = create_engine(conn)
    for u, host, node, vm in q(e, "SELECT uuid, host, node, vm_state FROM instances WHERE deleted = 0"):
        out["live"].append({"uuid": u, "host": host, "node": node, "vm_state": vm, "cell_id": cid})
    if CAND:
        out["deleted"] += [r[0] for r in q(e, "SELECT uuid FROM instances WHERE deleted != 0 AND uuid IN (%s)" % IN)]
        out["shadow"] += [r[0] for r in q(e, "SELECT uuid FROM shadow_instances WHERE uuid IN (%s)" % IN)]
        out["migrations"] += q(e, "SELECT uuid, status, migration_type FROM migrations WHERE uuid IN (%s)" % IN)
    e.dispose()
print("__JSON__" + json.dumps(out))
'''


def fetch_db(args, candidates):
    import base64
    cand = sorted(c for c in candidates if UUID_RE.match(c))
    code = DB_CODE.replace("__CANDIDATES__", json.dumps(cand))
    askpass = None
    try:
        if getattr(args, "db_ssh", None):
            # The code travels on the command line (base64), so stdin stays free
            # for the sudo password.
            ssh, env, askpass, password, user = ssh_setup(
                args.db_user, args.db_key, prompt_host=args.db_ssh)
            use_sudo = args.db_sudo if args.db_sudo is not None else (user != "root")
            b64 = base64.b64encode(code.encode()).decode()
            boot = f"import base64;exec(base64.b64decode('{b64}').decode())"
            remote = f"docker exec {args.nova_container} python3 -c {shlex.quote(boot)}"
            if use_sudo:
                remote = "sudo -S -p '' " + remote
            stdin = (password + "\n") if (use_sudo and password is not None) else None
            log(f"Querying the Nova DB via ssh {user}@{args.db_ssh} ({args.nova_container})")
            r = subprocess.run(ssh + [args.db_ssh, remote], input=stdin, capture_output=True,
                               text=True, timeout=600, env=env, start_new_session=True)
        else:
            log("Querying the Nova DB through: " + args.db_exec)
            r = subprocess.run(shlex.split(args.db_exec), input=code, capture_output=True,
                               text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.exit(f"DB access failed ({e}). Use --db-ssh <controller> (asks for credentials) "
                 "or --db-exec, or --no-db.")
    finally:
        if askpass:
            os.unlink(askpass)
    line = next((l for l in r.stdout.splitlines() if l.startswith("__JSON__")), None)
    if r.returncode != 0 or line is None:
        sys.exit(f"DB access failed (rc={r.returncode}):\n{r.stderr.strip()[-2000:]}\n"
                 "Use --db-ssh <controller> (asks for credentials) or --db-exec, or --no-db.")
    db = json.loads(line[len("__JSON__"):])
    db["live_by_uuid"] = {x["uuid"]: x for x in db["live"]}
    db["deleted"], db["shadow"] = set(db["deleted"]), set(db["shadow"])
    db["build_requests"] = set(db["build_requests"])
    db["mig_by_uuid"] = {m[0]: m for m in db["migrations"]}
    db["cell0_ids"] = {c["id"] for c in db["cells"] if c["uuid"] == CELL0}
    return db


def mapping_issues(db):
    """Instances alive in a cell DB whose instance_mapping is missing (type MISSING: fixed with
    map_instances) or points to another cell / to NULL (type WRONG: map_instances skips them,
    it must be fixed by hand in the nova_api DB)."""
    main = {c["id"] for c in db["cells"] if c["uuid"] != CELL0}
    cell = {c["id"]: c for c in db["cells"]}
    issues = []
    for x in db["live"]:
        cid = db["mappings"].get(x["uuid"], "MISSING")
        base = {**x, "cell_uuid": cell.get(x["cell_id"], {}).get("uuid"),
                "cell_name": cell.get(x["cell_id"], {}).get("name")}
        if cid == "MISSING":
            issues.append({**base, "type": "MISSING", "mapped_cell_id": None,
                           "problem": "instance_mapping missing"})
        elif cid is None or cid != x["cell_id"]:
            issues.append({**base, "type": "WRONG", "mapped_cell_id": cid,
                           "problem": f"mapping on cell_id={cid}, instance in cell_id={x['cell_id']}"})
    return issues, main


# ---------------------------------------------------------------- virsh

class Virsh:
    """libvirt inventory. Header written by collect-virsh:
         # virsh inventory <YYYYmmdd-HHMMSS local> utc=<ISO> (user ...)
         # host <name> ok|ERROR <n> utc=<ISO>
       Older inventories without utc= are supported: the local time of the header is used."""

    def __init__(self, path=None):
        self.doms, self.covered, self.host_ts, self.ts = {}, set(), {}, None
        if not path:
            return
        with open(path) as f:
            for l in f:
                l = l.strip()
                utc = next((parse_ts(x[4:]) for x in l.split() if x.startswith("utc=")), None)
                if l.startswith("# virsh inventory"):
                    p = l.split()
                    self.ts = utc or (local_to_utc(p[3]) if len(p) > 3 else None)
                    continue
                if l.startswith("# host "):
                    p = l.split()
                    if len(p) > 3 and p[3] == "ok":
                        self.covered.add(p[2])
                        if utc:
                            self.host_ts[p[2]] = utc
                    continue
                if not l or l.startswith("#"):
                    continue
                u, h, *st = l.split()
                self.doms.setdefault(u, []).append((h, " ".join(st)))

    @property
    def active(self):
        return bool(self.covered)

    def hosts(self, uuid):
        return [h for h, _ in self.doms.get(uuid, [])]

    def ts_for(self, host):
        """Time (UTC) when the host was captured; if unknown, the inventory's time."""
        return self.host_ts.get(host) or self.ts

    @property
    def oldest(self):
        vals = list(self.host_ts.values()) or ([self.ts] if self.ts else [])
        return min(vals) if vals else None

    def describe(self, uuid):
        return ", ".join(f"{h} ({s})" for h, s in self.doms.get(uuid, []))

    def check(self, uuid, nova_host):
        """None = consistent or no information; ('dup', msg) = domain elsewhere too; ('bad', msg)"""
        if not self.active:
            return None
        where = self.doms.get(uuid, [])
        hosts = [h for h, _ in where]
        desc = self.describe(uuid)
        if not hosts:
            return ("bad", f"no libvirt domain (Nova says {nova_host})") if nova_host in self.covered else None
        if nova_host not in hosts:
            return ("bad", f"Nova says {nova_host}, domain on {desc}")
        if len(hosts) > 1:
            return ("dup", f"domain on more than one host: {desc}")
        return None


def collect_targets(args, api=None):
    names = []
    if args.hosts:
        names += [h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.hosts_file:
        with open(args.hosts_file) as f:
            names += [l.split()[0] for l in f if l.strip() and not l.startswith("#")]
    if not names:
        api = api or API(args.interface)
        names = [h["hypervisor_hostname"]
                 for h in api.compute_list("/os-hypervisors/detail", "hypervisors")]
    if args.host:
        names = [n for n in names if re.search(args.host, n)]
    if not names:
        sys.exit("No host selected.")
    return sorted(set(names))


def cmd_collect_virsh(args):
    api = None
    if args.ignore_activity:
        log("--ignore-activity: skipping the check for migrations in progress.")
    else:
        api = API(args.interface)
        preflight(api, args, "before collection")
    targets = collect_targets(args, api)
    log("Hosts: " + ", ".join(targets))

    ssh, env, askpass, password, user = ssh_setup(args.user, args.key, args.ssh, "the hypervisors")
    use_sudo = args.sudo if args.sudo is not None else (user != "root")

    inner = ("for d in $(virsh list --all --uuid); do "
             "echo \"$d $(virsh domstate $d | head -n1)\"; done")
    remote = f"docker exec {args.libvirt_container} sh -c {shlex.quote(inner)}"
    if use_sudo:
        # sudo reads the password from stdin (-S), without a prompt (-p '')
        remote = f"sudo -S -p '' sh -c {shlex.quote(remote)}"

    stdin = (password + "\n") if (use_sudo and password is not None) else None

    def one(name):
        """Inventory of one host: (ok, error, 'uuid state' lines, UTC time at the end of collection)."""
        try:
            r = subprocess.run(ssh + [name, remote], input=stdin, capture_output=True,
                               text=True, timeout=180, env=env,
                               start_new_session=True)  # no tty: ssh uses SSH_ASKPASS
            ok, err, stdout = r.returncode == 0, r.stderr.strip()[:200], r.stdout
        except subprocess.TimeoutExpired:
            ok, err, stdout = False, "timeout", ""
        lines = [l.split(None, 1) for l in (stdout.splitlines() if ok else []) if l.strip()]
        return ok, err, lines, utc_stamp()

    def auth_denied(res):
        return not res[0] and "Permission denied" in res[1]

    started = utcnow()
    header = f"# virsh inventory {now()} utc={utc_stamp()} (user {user})"
    results = {}
    try:
        # First host alone: with wrong credentials we stop after a single attempt,
        # instead of sending dozens in parallel and getting the account locked.
        first, rest = targets[0], targets[1:]
        results[first] = one(first)
        log(f"  {first}: " + (f"{len(results[first][2])} domains" if results[first][0]
                              else f"ERROR {results[first][1]}"))
        if auth_denied(results[first]) and not args.keep_going:
            sys.exit("Authentication rejected on the first host: stopping so the account doesn't get "
                     "locked (use --keep-going to go on anyway).")
        workers = max(1, args.parallel)
        log(f"Collecting from the other {len(rest)} hosts, {workers} at a time...")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, n): n for n in rest}
            stop = False
            for f in as_completed(futs):
                n = futs[f]
                if f.cancelled():
                    continue
                results[n] = f.result()
                ok, err, lines, _ = results[n]
                log(f"  {n}: " + (f"{len(lines)} domains" if ok else f"ERROR {err}"))
                if auth_denied(results[n]) and not args.keep_going and not stop:
                    stop = True
                    for other in futs:  # cancel the ones not started yet
                        other.cancel()
            if stop:
                sys.exit("Authentication rejected: stopping so the account doesn't get locked "
                         "(use --keep-going to go on anyway). Inventory NOT written.")
    finally:
        if askpass:
            os.unlink(askpass)
    out = [header]
    for name in targets:  # stable order, independent of completion order
        ok, err, lines, ts = results[name]
        out.append(f"# host {name} {'ok' if ok else 'ERROR'} {len(lines)} utc={ts}")
        out += [f"{p[0]} {name} {p[1] if len(p) > 1 else '?'}" for p in lines if UUID_RE.match(p[0])]
    errs = [n for n in targets if not results[n][0]]
    log(f"Collection done in {int((utcnow() - started).total_seconds())}s: "
        f"{len(targets) - len(errs)} hosts ok, {len(errs)} in error"
        + (f" ({', '.join(errs[:10])}{' ...' if len(errs) > 10 else ''})" if errs else ""))
    with open(args.out, "w") as f:
        f.write("\n".join(out) + "\n")
    log(f"Wrote {args.out}. Hosts in ERROR will be excluded from the virsh checks.")
    if api:
        act = check_activity(api, args.stale_hours)
        if activity_blocking(act):
            log("\nWARNING: migrations/operations started during the collection:")
            for l in describe_activity(act):
                log("  " + l)
            log("The inventory may not be consistent: once they are done, run collect-virsh again.")


# ---------------------------------------------------------------- report

def evaluate_blocked(api, stale, by_id, rp_to_name, V, db, db_live, active_by_instance, backup):
    """For every stuck migration: where the instance really is (Nova, virsh) and whether the record can be closed."""
    blocked = []
    for u, m in sorted(stale.items(), key=lambda x: str(x[1].get("updated_at"))):
        iid = m.get("instance_uuid")
        e = {"mig_id": m.get("id"), "uuid": u, "instance": iid, "type": m.get("migration_type"),
             "status": m.get("status"), "updated_at": m.get("updated_at"),
             "orphan_finished": is_finished(m),
             "source": m.get("source_node") or m.get("source_compute"),
             "dest": m.get("dest_node") or m.get("dest_compute")}
        st, d = api.placement_call("GET", f"/allocations/{u}")
        if st == 200 and d.get("allocations"):
            backup[u] = d
        e["mig_alloc"] = [rp_to_name.get(rp, rp) for rp in (d.get("allocations") or {})] if st == 200 else []
        s = by_id.get(iid)
        if s is None and iid:
            st2, body = api.compute_call("GET", f"/servers/{iid}")
            s = body["server"] if st2 == 200 else None
        reasons = []
        if s is None:
            if db and iid in db_live:
                reasons.append("API 404 but instance alive in the DB (broken mapping)")
            else:
                e["note"] = "the instance no longer exists"
        else:
            e.update(nova_host=s.get(HYP), inst_status=s.get("status"),
                     task_state=s.get("OS-EXT-STS:task_state"))
            st3, d3 = api.placement_call("GET", f"/allocations/{iid}")
            e["inst_alloc"] = [rp_to_name.get(rp, rp) for rp in (d3.get("allocations") or {})] if st3 == 200 else []
            if e["task_state"]:
                reasons.append(f"task_state={e['task_state']}")
            if e["inst_status"] == "ERROR":
                reasons.append("instance in ERROR")
            if not e["nova_host"]:
                reasons.append(f"instance without a hypervisor (status={e['inst_status']})")
            if iid in active_by_instance:
                reasons.append("the instance has another active migration")
            if not V.active:
                reasons.append("no virsh inventory (--virsh)")
            else:
                missing = sorted({h for h in (e["nova_host"], e["source"], e["dest"]) if h and h not in V.covered})
                if missing:
                    reasons.append("virsh not collected on " + ", ".join(missing))
                v = V.check(iid, e["nova_host"])
                if v:
                    reasons.append(v[1])
                else:
                    e["virsh"] = V.describe(iid)
        e["outcome"] = "CHECK" if reasons else "CLOSABLE"
        e["reasons"] = reasons
        blocked.append(e)
    return blocked


def virsh_missing(V, hyp_names):
    """Hypervisors not covered by the virsh inventory (all of them, if there is no inventory)."""
    return sorted(h for h in hyp_names if h not in V.covered)


def virsh_age(V, args):
    """Age in minutes of the first host captured (None if unknown)."""
    if not V.active or not V.oldest:
        return {"virsh_age_min": None, "max_virsh_age": args.max_virsh_age}
    return {"virsh_age_min": int((utcnow() - V.oldest).total_seconds() // 60),
            "max_virsh_age": args.max_virsh_age}


def classify_existing(c, s, V):
    """Classifies a consumer that matches an instance existing in Nova but not on this host."""
    vm_host = s.get(HYP)
    if not vm_host:
        # No hypervisor: SHELVED_OFFLOADED, ERROR from a failed build, etc.
        # heal_allocations does nothing on instances without a node: the allocation must be removed.
        e = {"cls": "NO_HOST", "vm_host": None, "status": s.get("status"),
             "vm_state": s.get("OS-EXT-STS:vm_state"), "task_state": s.get("OS-EXT-STS:task_state"),
             "updated": s.get("updated"),
             "note": f"instance {s.get('status')} without a hypervisor: leaked allocation"}
        probs = nohost_problems(s)
        if V.hosts(c):
            e["cls"] = "A1_CHECK"
            e["virsh"] = "Nova has no host but domain on " + V.describe(c)
        elif probs:
            e["cls"] = "A1_CHECK"
            e["note"] = "instance without a hypervisor in an unexpected state: " + ", ".join(probs)
        elif not V.active:
            e["note"] += " (NOT checked against virsh)"
        return e
    e = {"cls": "A1", "vm_host": vm_host, "status": s.get("status")}
    v = V.check(c, vm_host)
    if v:
        e["virsh"] = v[1]
        if v[0] == "bad":
            e["cls"] = "A1_CHECK"
    return e


def find_incomplete_deletes(api, servers, V):
    """Instances that still exist with terminated_at set: Nova sets it during deletion, so a live
    instance with terminated_at is almost always a delete that started and never completed
    (the record is only removed at the end). Confirmed from the action history."""
    out = {}
    for s in servers:
        term = s.get("OS-SRV-USG:terminated_at")
        if not term:
            continue
        iid = s["id"]
        e = {"uuid": iid, "name": s.get("name"), "project": s.get("tenant_id"), "status": s.get("status"),
             "task_state": s.get("OS-EXT-STS:task_state"), "host": s.get(HYP), "terminated_at": term,
             "updated": s.get("updated")}
        reasons = []
        st, d = api.compute_call("GET", f"/servers/{iid}/os-instance-actions")
        acts = (d or {}).get("instanceActions") or [] if st == 200 else []
        dels = sorted((a for a in acts if a.get("action") == "delete"),
                      key=lambda a: str(a.get("start_time")), reverse=True)
        if st != 200:
            reasons.append(f"action history not readable (HTTP {st})")
        elif not dels:
            reasons.append("terminated_at set but no delete in the history")
        else:
            a = dels[0]
            e.update(delete_request=a.get("request_id"), delete_time=a.get("start_time"),
                     delete_project=a.get("project_id"), delete_user=a.get("user_id"))
            st2, d2 = api.compute_call("GET", f"/servers/{iid}/os-instance-actions/{a.get('request_id')}")
            events = ((d2 or {}).get("instanceAction") or {}).get("events") or [] if st2 == 200 else []
            failed = [ev for ev in events if (ev.get("result") or "").lower() == "error"]
            e["delete_events_error"] = [ev.get("event") for ev in failed]
            if not failed and (a.get("message") or "").lower() != "error":
                reasons.append("no event in error in the delete: cause to be found")
            if a.get("project_id") != s.get("tenant_id"):
                reasons.append(f"delete requested by project {a.get('project_id')}, "
                               f"owner {s.get('tenant_id')}")
            later = [x.get("action") for x in acts if str(x.get("start_time")) > str(a.get("start_time"))]
            if later:
                e["later_actions"] = sorted(set(later))
        if e["task_state"]:
            reasons.append(f"task_state={e['task_state']}")
        if not V.active:
            reasons.append("no virsh inventory: presence of the domain not checked")
        elif V.hosts(iid):
            reasons.append("libvirt domain still present on " + V.describe(iid))
        e["outcome"] = "CHECK" if reasons else "DELETABLE"
        e["reasons"] = reasons
        out[iid] = e
    return out


def inventory_outdated(V, migs):
    """Migrations that finished AFTER the virsh collection of one of the hosts involved (among the
    inventoried ones): if there are any, the inventory is out of date and the comparison with
    virsh is not reliable."""
    if not V.active:
        return []
    out = []
    for m in migs:
        if is_open(m):
            continue
        t = parse_ts(m.get("updated_at"))
        if not t:
            continue
        hosts = {m.get(k) for k in ("source_node", "dest_node", "source_compute", "dest_compute")}
        snaps = [V.ts_for(h) for h in hosts if h in V.covered and V.ts_for(h)]
        if snaps and t > min(snaps):
            out.append(m)
    return out


def check_after(api, args, report):
    """Re-check at the end of the report: if something started in the meantime, the report is invalid."""
    if report.get("forced"):
        return
    post = check_activity(api, args.stale_hours)
    if activity_blocking(post):
        report["invalid"] = describe_activity(post)
        log("\nWARNING: migrations/operations started during the report: "
            "report NOT valid, the generated commands are commented out.")


def analyze(api, args):
    log("Loading hypervisors, instances and migrations from Nova...")
    hyps = api.compute_list("/os-hypervisors/detail", "hypervisors")
    servers = api.compute_list("/servers/detail?all_tenants=1", "servers")
    migs_all = all_migrations(api)
    act = preflight(api, args, "report", migs_all, servers)
    forced = activity_blocking(act)
    V = Virsh(args.virsh)

    late = inventory_outdated(V, migs_all)
    if late:
        log(f"\nThe virsh inventory is older than {len(late)} migrations that have already finished:")
        for m in late[:20]:
            log(f"  id={m.get('id')} {m.get('instance_uuid')} {m.get('migration_type')} {m.get('status')} "
                f"{mig_src(m)} -> {mig_dst(m)} closed at {str(m.get('updated_at'))[:16]} UTC")
        if len(late) > 20:
            log(f"  ... and {len(late) - 20} more")
        if not args.ignore_activity:
            sys.exit("\nStopping: the comparison with virsh would not be reliable.\n"
                     "Run collect-virsh again and report right after (or --ignore-activity, not recommended).")
        forced = True

    open_migs = active_migrations(api, migs_all)
    by_id = {s["id"]: s for s in servers}
    stale = stale_migrations(migs_all, args.stale_hours, by_id)
    by_node = {}
    for s in servers:
        by_node.setdefault(s.get(HYP), []).append(s)
    name_to_rp = {h["hypervisor_hostname"]: h["id"] for h in hyps}
    rp_to_name = {v: k for k, v in name_to_rp.items()}
    # Open migrations that are not stuck: without --ignore-activity there are none (pre-flight check).
    active_by_instance = {}
    for u, m in open_migs.items():
        if u not in stale:
            active_by_instance.setdefault(m.get("instance_uuid"), []).append(m)

    missing_virsh = virsh_missing(V, [h["hypervisor_hostname"] for h in hyps])
    if V.active and missing_virsh:
        log(f"WARNING: {len(missing_virsh)} hypervisors not covered by the virsh inventory: "
            + ", ".join(missing_virsh[:10]) + (" ..." if len(missing_virsh) > 10 else ""))
    base = {"generated": now(), "virsh_check": V.active, "virsh_hosts": sorted(V.covered),
            "virsh_missing": missing_virsh,
            "hosts": [], "global": {}, "stale_hours": args.stale_hours,
            "forced": forced, "activity": describe_activity(act),
            "inventory_outdated": len(late),
            "stuck_operations": describe_stuck(act, args.stale_hours), **virsh_age(V, args)}

    # 0) stuck migrations: if there are any, the rest of the report would have to be redone
    #    after closing them anyway, so (unless --full) we stop here.
    if stale and not args.full:
        log(f"Found {len(stale)} stuck migrations: analysing only those (--full for the whole report).")
        missing = [m.get("instance_uuid") for m in stale.values()
                   if m.get("instance_uuid") and m.get("instance_uuid") not in by_id]
        db = None if args.no_db else fetch_db(args, missing)
        db_live = db["live_by_uuid"] if db else {}
        backup = {}
        report = dict(base, db_check=db is not None, interrupted=True)
        report["global"]["stuck_migrations"] = evaluate_blocked(
            api, stale, by_id, rp_to_name, V, db, db_live, active_by_instance, backup)
        if db:
            report["global"]["mapping"], _ = mapping_issues(db)
        check_after(api, args, report)
        return report, backup

    # 1) placement scan
    scan, cand, uncertain = [], set(), {}
    for h in sorted(hyps, key=lambda x: x["hypervisor_hostname"]):
        name, rp = h["hypervisor_hostname"], h["id"]
        if args.host and not re.search(args.host, name):
            continue
        log(f"  {name}")
        st, d = api.placement_call("GET", f"/resource_providers/{rp}/allocations")
        if st != 200:
            log(f"  ! {name}: RP {rp} -> HTTP {st}, skipping")
            continue
        scan.append((h, d["allocations"]))
        local = {s["id"] for s in by_node.get(name, [])}
        cand |= {c for c in d["allocations"] if c not in local and c not in open_migs and c not in by_id}

    for c in sorted(cand):
        st, body = api.compute_call("GET", f"/servers/{c}")
        if st == 200:
            by_id[c] = body["server"]
        elif st != 404:
            uncertain[c] = st
    cand = {c for c in cand if c not in by_id and c not in uncertain}

    # 2) DB
    db = None if args.no_db else fetch_db(args, cand)
    db_live = db["live_by_uuid"] if db else {}

    report = dict(base, db_check=db is not None)
    backup = {}
    ignored = []
    del_inc = find_incomplete_deletes(api, servers, V)
    if del_inc:
        log(f"Instances with an incomplete delete: {len(del_inc)}")
    map_by_uuid = {x["uuid"]: x for x in mapping_issues(db)[0]} if db else {}

    # 3) classification
    for h, allocs in scan:
        name, rp = h["hypervisor_hostname"], h["id"]
        insts = {s["id"]: s for s in by_node.get(name, [])}
        A, B, C = [], [], []

        for c, a in allocs.items():
            if c in insts:
                continue
            e = {"consumer": c, "resources": a.get("resources", {})}
            if c in stale:
                m = stale[c]
                e.update(cls="STUCK_MIGRATION", instance=m.get("instance_uuid"), mig_id=m.get("id"),
                         mig_status=m.get("status"), mig_type=m.get("migration_type"),
                         updated_at=m.get("updated_at"))
            elif c in open_migs or c in active_by_instance:
                # consumer of an open migration (only with --ignore-activity): cannot be classified
                ignored.append(c)
                continue
            elif c in uncertain:
                e.update(cls="UNCERTAIN", note=f"GET /servers -> HTTP {uncertain[c]}")
            elif c in del_inc:
                e.update(cls="INCOMPLETE_DELETE", note="delete never completed: see section 1c")
            elif c in by_id:
                e.update(classify_existing(c, by_id[c], V))
                if e["cls"] == "NO_HOST" and db:
                    x = db_live.get(c)
                    if x is None:
                        e["cls"] = "A1_CHECK"
                        e["note"] = "instance without a hypervisor in the API but not alive in the cell DB"
                    elif x["host"] or x["node"]:
                        e["cls"] = "A1_CHECK"
                        e["note"] = (f"no hypervisor in the API but in the DB host={x['host']} "
                                     f"node={x['node']}")
                    elif x["cell_id"] in db["cell0_ids"]:
                        e["cell0"] = True
                        e["note"] += " (in cell0: never scheduled)"
            elif db and c in db_live:
                x = db_live[c]
                mi = map_by_uuid.get(c)
                if mi is None:
                    why = ("API 404 but alive in the cell DB with a correct mapping: "
                           "cause of the 404 to be found (NOT a mapping problem)")
                elif mi["type"] == "MISSING":
                    why = "API 404, alive in the cell DB, instance_mapping missing (see section 0)"
                else:
                    why = (f"API 404, alive in the cell DB, mapping on the wrong cell "
                           f"({mi['problem']}; see section 0)")
                e.update(cls="DB_ALIVE", db_node=x["node"], vm_state=x["vm_state"], note=why)
            elif V.hosts(c):
                e.update(cls="VIRSH_ALIVE", note="domain present on " + ", ".join(V.hosts(c)))
            elif db and c in db["build_requests"]:
                e.update(cls="UNKNOWN", note="API 404 but present in build_requests (orphaned build_request?)")
            elif db and c in db["mig_by_uuid"]:
                m = db["mig_by_uuid"][c]
                e.update(cls="A2", note=f"consumer of a closed migration ({m[2]}, {m[1]})")
            elif db and (c in db["deleted"] or c in db["shadow"]):
                e.update(cls="A2", note="deleted in the DB" if c in db["deleted"] else "in shadow_instances")
            elif db:
                e.update(cls="UNKNOWN", note="not found in API, DB, shadow, migrations")
            else:
                e.update(cls="A2", note="NOT checked against the DB (--no-db)")

            if e["cls"] in ("A1", "A1_CHECK", "A2", "NO_HOST", "DB_ALIVE", "VIRSH_ALIVE", "UNKNOWN"):
                st3, full = api.placement_call("GET", f"/allocations/{c}")
                if st3 == 200:
                    backup[c] = full
                    if e["cls"].startswith("A1") and e.get("vm_host"):
                        e["alloc_on_vm_host"] = name_to_rp.get(e["vm_host"]) in full["allocations"]
            A.append(e)

        for iid, s in insts.items():
            v = V.check(iid, name)
            if iid in allocs:
                if iid in del_inc:
                    C.append({"instance": iid, "name": s.get("name"), "status": s.get("status"),
                              "cls": "INCOMPLETE_DELETE", "virsh": v[1] if v else ""})
                elif v:
                    C.append({"instance": iid, "name": s.get("name"), "status": s.get("status"),
                              "cls": "NOVA_VIRSH" if v[0] == "bad" else "DUPLICATE_DOMAIN", "virsh": v[1]})
                continue
            e = {"instance": iid, "name": s.get("name"), "status": s.get("status"),
                 "task_state": s.get("OS-EXT-STS:task_state"),
                 "ram_mb": (s.get("flavor") or {}).get("ram")}
            if v:
                e["virsh"] = v[1]
            if iid in del_inc:
                # a heal would recreate the allocation for a VM the owner deleted
                e["cls"] = "INCOMPLETE_DELETE"
            elif e["task_state"]:
                # without --ignore-activity only operations stuck for more than --stale-hours get here
                e["cls"] = "B_CHECK"
                e["note"] = f"task_state={e['task_state']} (stuck operation)"
            elif v and v[0] == "bad":
                e["cls"] = "B_CHECK"
            elif e["status"] == "ERROR":
                e["cls"] = "B_ERROR"
            else:
                e["cls"] = "B"
            B.append(e)

        report["hosts"].append({"hypervisor": name, "rp": rp, "state": h.get("state"),
                                "status": h.get("status"), "A": A, "B": B, "C": C})

    # 4) stuck migrations
    report["global"]["stuck_migrations"] = evaluate_blocked(
        api, stale, by_id, rp_to_name, V, db, db_live, active_by_instance, backup)
    if ignored:
        report["global"]["ignored_migration_consumers"] = ignored
    report["global"]["incomplete_deletes"] = list(del_inc.values())

    # 5) global checks
    if db:
        issues, _ = mapping_issues(db)
        report["global"]["mapping"] = issues
    if V.active:
        known = set(by_id) | set(db_live)
        report["global"]["orphan_domains"] = [
            {"uuid": u, "where": w} for u, w in V.doms.items()
            if u not in known and any(h in V.covered for h, _ in w)]
    check_after(api, args, report)
    return report, backup


def virsh_too_old(report):
    age = report.get("virsh_age_min")
    return age is not None and age > report.get("max_virsh_age", 30)


def count(lst, *classes):
    return sum(1 for e in lst if e["cls"] in classes)


def print_banner(report):
    if report.get("forced"):
        print("\n!!! FORCED REPORT (--ignore-activity) with operations in progress or an outdated virsh inventory:")
        print("!!! the classifications are NOT reliable and the commands in fix_commands.sh are commented out.")
        for l in report.get("activity") or []:
            print("    " + l)
    if report.get("invalid"):
        print("\n!!! REPORT NOT VALID: migrations/operations started during the analysis.")
        print("!!! The commands in fix_commands.sh are commented out: run again once they are done.")
        for l in report["invalid"]:
            print("    " + l)
    if report.get("stuck_operations"):
        print(f"\nOperations stuck for more than {report['stale_hours']}h (they don't block, check by hand): "
              f"{len(report['stuck_operations'])}")


def print_summary(report, verbose):
    print_banner(report)
    if report.get("interrupted"):
        bl = report["global"]["stuck_migrations"]
        ok = sum(1 for e in bl if e["outcome"] == "CLOSABLE")
        print(f"\nDB: {'yes' if report['db_check'] else 'NO'} | virsh: "
              + (f"{len(report['virsh_hosts'])} hosts" if report["virsh_check"] else "no"))
        if virsh_too_old(report):
            print(f"WARNING: virsh inventory is {report['virsh_age_min']} minutes old: run collect-virsh again.")
        print(f"\nREPORT INTERRUPTED: {len(bl)} stuck migrations (stuck for more than {report['stale_hours']}h)")
        print(f"  closable: {ok}   to check: {len(bl) - ok}")
        for e in bl:
            where = e.get("nova_host") or ("no host" if e.get("inst_status") else "does not exist")
            orph = " [orphaned finished]" if e.get("orphan_finished") else ""
            print(f"  {e['outcome']:10} id={e['mig_id']:<6}{orph} {e['source']} -> {e['dest']}  instance: {where}"
                  f"  migration alloc: {', '.join(e['mig_alloc']) or '-'}"
                  + (f"  [{'; '.join(e['reasons'])}]" if e["reasons"] else ""))
        mp = report["global"].get("mapping") or []
        if mp:
            print(f"Live instances with a missing/wrong mapping: {len(mp)}  <-- fix these too")
        if ok:
            print(f"\nNext steps: review and run close_migrations.sql ({ok} migrations), "
                  "then collect-virsh and report again.")
        if len(bl) > ok:
            print(f"{'N' if not ok else 'Also, n'}o SQL is generated for the {len(bl) - ok} to check: "
                  "they must be analysed by hand (details in report.json and fix_commands.sh).")
        return
    flags = []
    flags.append("DB: yes" if report["db_check"] else "DB: NO (A2 not confirmed!)")
    flags.append(f"virsh: {len(report['virsh_hosts'])} hosts" if report["virsh_check"] else "virsh: no")
    if report.get("virsh_age_min") is not None:
        flags.append(f"virsh inventory: {report['virsh_age_min']} min ago")
    print("\n" + " | ".join(flags))
    if virsh_too_old(report):
        print(f"WARNING: virsh inventory older than {report['max_virsh_age']} minutes: "
              "instances created or deleted in the meantime can distort the comparison. Run collect-virsh again.")
    if report["virsh_check"] and report.get("virsh_missing"):
        vm = report["virsh_missing"]
        print(f"WARNING: incomplete virsh inventory, {len(vm)} hypervisors missing: "
              + ", ".join(vm[:10]) + (" ..." if len(vm) > 10 else ""))
    if not report["virsh_check"] or report.get("virsh_missing") or not report["db_check"]:
        print("  -> the deletion commands (A2, NO_HOST) in fix_commands.sh are commented out.")
    print(f"{'HYPERVISOR':34} {'A1':>4} {'A2':>4} {'A2 GiB':>7} {'NOHOST':>6} {'INCDEL':>6} {'B':>4} "
          f"{'CHECK':>5} {'STUCK':>5}")
    t = {"A1": 0, "A2": 0, "mb": 0, "NH": 0, "B": 0, "V": 0}
    for h in report["hosts"]:
        A, B, C = h["A"], h["B"], h["C"]
        a1, a2, nh, b = count(A, "A1"), count(A, "A2"), count(A, "NO_HOST"), count(B, "B")
        di = count(A, "INCOMPLETE_DELETE") + count(B, "INCOMPLETE_DELETE") + count(C, "INCOMPLETE_DELETE")
        n_check = (count(A, "A1_CHECK", "DB_ALIVE", "VIRSH_ALIVE", "UNKNOWN", "UNCERTAIN")
                 + count(B, "B_ERROR", "B_CHECK") + count(C, "NOVA_VIRSH", "DUPLICATE_DOMAIN"))
        n_stuck = count(A, "STUCK_MIGRATION")
        mb = sum(e["resources"].get("MEMORY_MB", 0) for e in A if e["cls"] == "A2")
        t["A1"] += a1; t["A2"] += a2; t["mb"] += mb; t["NH"] += nh; t["B"] += b; t["V"] += n_check
        if verbose or any((a1, a2, nh, di, b, n_check, n_stuck)):
            print(f"{h['hypervisor']:34} {a1:4} {a2:4} {mb // 1024:7} {nh:6} {di:6} {b:4} {n_check:5} {n_stuck:5}")
    g = report["global"]
    di = g.get("incomplete_deletes") or []
    dok = sum(1 for x in di if x["outcome"] == "DELETABLE")
    print(f"\nTotal: A1={t['A1']}  A2={t['A2']} ({t['mb'] // 1024} GiB)  NO_HOST={t['NH']}  "
          f"INCOMPLETE_DELETE={len(di)} (deletable {dok})  B={t['B']}  to check={t['V']}")
    bl = g.get("stuck_migrations") or []
    if bl:
        ok = sum(1 for e in bl if e["outcome"] == "CLOSABLE")
        print(f"Stuck migrations (stuck for more than {report['stale_hours']}h): {len(bl)} "
              f"-> closable {ok}, to check {len(bl) - ok}  (see close_migrations.sql)")
        print("  WARNING: close them and REGENERATE the report before fixing anything else:")
        print("  the state of instances and allocations changes and the other columns are not final.")
    if g.get("ignored_migration_consumers"):
        print(f"Consumers of migrations in progress ignored (--ignore-activity): {len(g['ignored_migration_consumers'])}")
    if "mapping" in g:
        na = sum(1 for x in g["mapping"] if x["type"] == "MISSING")
        print(f"Live instances with a missing/wrong mapping: {len(g['mapping'])}"
              + (f" (missing {na}, wrong cell {len(g['mapping']) - na})  <-- fix BEFORE anything else"
                 if g["mapping"] else ""))
    if "orphan_domains" in g:
        print(f"libvirt domains unknown to Nova: {len(g['orphan_domains'])}")


def write_fix_commands(report, path):
    heal_force, heal, a2_by_host, manual, a2_seen, nohost = {}, {}, {}, [], {}, {}
    for h in report["hosts"]:
        hn = h["hypervisor"]
        for e in h["A"]:
            if e["cls"] == "A1":
                heal_force[e["consumer"]] = (f"lives on {e['vm_host']}, stale on {hn}"
                                             + (f" - virsh NOTE: {e['virsh']}" if e.get("virsh") else ""))
            elif e["cls"] == "A2":
                if e["consumer"] in a2_seen:
                    a2_seen[e["consumer"]].append(hn)
                    continue
                a2_seen[e["consumer"]] = [hn]
                a2_by_host.setdefault(hn, []).append((e["consumer"], e.get("note", "")))
            elif e["cls"] == "NO_HOST":
                nohost.setdefault(e["consumer"], {"status": e.get("status"),
                                                  "updated": e.get("updated"), "hosts": []})
                nohost[e["consumer"]]["hosts"].append(hn)
            elif e["cls"] in MANUAL:
                manual.append(f"{e['cls']:12} {e['consumer']} on {hn}: {e.get('note') or e.get('virsh', '')}")
        for e in h["B"]:
            if e["cls"] == "B" and e["instance"] not in heal_force:
                heal[e["instance"]] = (f"{e['status']} on {hn}"
                                       + (f" - virsh NOTE: {e['virsh']}" if e.get("virsh") else ""))
            elif e["cls"] in MANUAL:
                manual.append(f"{e['cls']:12} {e['instance']} on {hn}: "
                              f"{e.get('note') or e.get('virsh') or e['status']}")
        for e in h["C"]:
            if e["cls"] != "INCOMPLETE_DELETE":  # handled in section 1c
                manual.append(f"{e['cls']:12} {e['instance']} on {hn}: {e['virsh']}")
    # stuck operations not already listed for another reason
    manual += [l for l in (report.get("stuck_operations") or []) if not any(l.split()[1] in m for m in manual)]

    L = ["#!/bin/bash",
         f"# Generated by placement_reconcile.py on {report['generated']}",
         f"# DB check: {'yes' if report['db_check'] else 'NO'} - virsh check: "
         f"{'yes (' + str(len(report['virsh_hosts'])) + ' hosts)' if report['virsh_check'] else 'NO'}",
         "# Do NOT run blindly: review line by line, following the README.",
         "# Order: 0) mapping  0b) stuck migrations  1) A2  1b) NO_HOST  1c) INCOMPLETE_DELETE  2) A1  3) B",
         "exit 1  # remove after review", ""]
    if virsh_too_old(report):
        L[5:5] = [f"# WARNING: virsh inventory is {report['virsh_age_min']} minutes old "
                  f"(threshold {report['max_virsh_age']}): run collect-virsh right before the report."]
    bl = report["global"].get("stuck_migrations") or []
    ok = [e for e in bl if e["outcome"] == "CLOSABLE"]
    sql_path = os.path.join(os.path.dirname(path) or ".", "close_migrations.sql")
    if ok and not (report.get("forced") or report.get("invalid")):
        ok_std = [e for e in ok if not e.get("orphan_finished")]
        ok_fin = [e for e in ok if e.get("orphan_finished")]
        Q = [f"-- Generated by placement_reconcile.py on {report['generated']}",
             "-- To run on the 'nova' cell DB AFTER backing up the migrations table:",
             "--   mysqldump --single-transaction --no-tablespaces nova migrations > nova-migrations-<date>.sql",
             f"-- {len(ok)} migrations stuck for more than {report['stale_hours']}h, instance consistent between Nova and libvirt.",
             "-- 'finished' migrations awaiting confirmation (instance in VERIFY_RESIZE) are always excluded:",
             "-- they must be confirmed with resize confirm. Only orphaned 'finished' migrations appear here.",
             ""]

        def block(title, rows, status_cond):
            B = [f"-- ===== {title} ({len(rows)}) ====="]
            for e in rows:
                B.append(f"-- id={e['mig_id']} {e['uuid']} {e['type']} {e['source']} -> {e['dest']} "
                         f"({e['status']} since {str(e['updated_at'])[:16]}); instance {e['instance']} "
                         + (f"on {e.get('nova_host')} ({e.get('inst_status')}) [virsh: {e.get('virsh')}]"
                            if e.get("nova_host") else "(no longer exists)")
                         + f"; migration alloc: {', '.join(e['mig_alloc']) or 'none'}")
            B += ["UPDATE migrations SET status = 'error', updated_at = UTC_TIMESTAMP()",
                  "WHERE deleted = 0",
                  f"  AND {status_cond}",
                  f"  AND updated_at < UTC_TIMESTAMP() - INTERVAL {report['stale_hours']} HOUR",
                  "  AND uuid IN (",
                  ",\n".join(f"    '{e['uuid']}'" for e in rows) + ");",
                  f"SELECT ROW_COUNT() AS rows_changed, {len(rows)} AS rows_expected;", ""]
            return B

        Q += ["-- Run in an interactive session (source), check the rows, then COMMIT or ROLLBACK.",
              "START TRANSACTION;", ""]
        if ok_std:
            Q += block("Stuck migrations", ok_std,
                       "status NOT IN ('completed','confirmed','reverted','error','failed','done',"
                       "'cancelled','finished')")
        if ok_fin:
            Q += block("Orphaned 'finished' migrations (instance not in VERIFY_RESIZE)", ok_fin,
                       "status = 'finished'")
        Q += ["-- If in EVERY block rows_changed = rows_expected:   COMMIT;",
              "-- Otherwise:                                      ROLLBACK;  and regenerate the report",
              ""]
        with open(sql_path, "w") as f:
            f.write("\n".join(Q))
    elif os.path.exists(sql_path):
        os.unlink(sql_path)

    opts = report.get("cli_opts", "")
    mp = report["global"].get("mapping") or []
    why = [x for x in (
        "broken mappings" if mp else "",
        "stuck migrations" if bl else "",
        "forced report (--ignore-activity) with operations in progress or an outdated inventory"
        if report.get("forced") else "",
        "migrations/operations started during the report" if report.get("invalid") else "") if x]
    stop = bool(why)
    if stop:
        L += ["# " + "!" * 76,
              "# STOP: " + "; ".join(why) + ".",
              "# Fix what is listed, then REGENERATE THE REPORT (collect-virsh + report): the state of",
              "# instances and allocations changes and the classifications below are no longer valid.",
              "# That is why every command is commented out.",
              "# " + "!" * 76]
        for l in (report.get("activity") if report.get("forced") else []) or []:
            L.append(f"#   {l}")
        for l in report.get("invalid") or []:
            L.append(f"#   {l}")
        L.append("")
    map_sql = os.path.join(os.path.dirname(path) or ".", "fix_mappings.sql")
    if mp:
        L.append("# ===== 0) Broken mappings: fix FIRST, then regenerate the report =====")
        missing = [x for x in mp if x["type"] == "MISSING"]
        wrong = [x for x in mp if x["type"] == "WRONG"]
        if missing:
            L.append(f"# - instance_mapping missing ({len(missing)}): map_instances creates the missing mappings "
                     "of the given cell")
            L += [f"#   {x['uuid']} node={x['node']} cell {x['cell_name']} ({x['cell_uuid']})" for x in missing]
            for cu in sorted({x["cell_uuid"] for x in missing if x["cell_uuid"]}):
                L.append(f"# on the controller: docker exec nova_api nova-manage cell_v2 map_instances --cell_uuid {cu}")
        if wrong:
            L.append(f"# - mapping on the wrong cell ({len(wrong)}): map_instances does NOT fix them "
                     "(it skips instances that are already mapped).")
            L.append(f"#   Review and run {map_sql} on the nova_api DB (procedure in the README).")
            L += [f"#   {x['uuid']} node={x['node']} {x['problem']} (actual cell {x['cell_name']})" for x in wrong]
            Q = [f"-- Generated by placement_reconcile.py on {report['generated']}",
                 "-- Fix for instance_mappings pointing to the wrong cell, on the 'nova_api' DB.",
                 "-- First: check each instance with",
                 "--   docker exec nova_api nova-manage cell_v2 verify_instance --uuid <uuid>",
                 "-- and back up the table:",
                 "--   mysqldump --single-transaction --no-tablespaces nova_api instance_mappings > nova_api-instance_mappings-<date>.sql",
                 "", "START TRANSACTION;", ""]
            for x in wrong:
                old = "NULL" if x["mapped_cell_id"] is None else str(x["mapped_cell_id"])
                Q.append(f"-- {x['uuid']} node={x['node']}: {x['problem']}")
                Q.append(f"UPDATE instance_mappings SET cell_id = {x['cell_id']}, updated_at = UTC_TIMESTAMP() "
                         f"WHERE instance_uuid = '{x['uuid']}' AND cell_id <=> {old};")
            Q += ["",
                  "SELECT COUNT(*) AS rows_fixed, %d AS rows_expected FROM instance_mappings" % len(wrong),
                  "WHERE (instance_uuid, cell_id) IN (",
                  ",\n".join(f"    ('{x['uuid']}', {x['cell_id']})" for x in wrong) + ");",
                  "",
                  "-- If the two numbers match:   COMMIT;",
                  "-- Otherwise:                  ROLLBACK;  and regenerate the report",
                  ""]
            with open(map_sql, "w") as f:
                f.write("\n".join(Q))
        L.append("")
    if not any(x["type"] == "WRONG" for x in mp) and os.path.exists(map_sql):
        os.unlink(map_sql)
    if bl:
        L.append(f"# ===== 0b) Stuck migrations (stuck for more than {report['stale_hours']}h) =====")
        if ok and os.path.exists(sql_path):
            nf = sum(1 for e in ok if e.get("orphan_finished"))
            L.append(f"# {len(ok)} closable" + (f" ({nf} of them orphaned 'finished')" if nf else "")
                     + ": review and run close_migrations.sql on the 'nova' DB")
            L.append("# (back up the migrations table first; procedure in the README).")
        for e in bl:
            if e["outcome"] == "CHECK":
                manual.append(f"STUCK_MIG {e['uuid']} (id {e['mig_id']}, instance {e['instance']}): "
                              + "; ".join(e["reasons"]))
        if len(ok) < len(bl):
            L.append(f"# {len(bl) - len(ok)} to check by hand: see the section at the end.")
        L.append("# Then ALWAYS regenerate the report before going on:")
        L.append(f"#   {PY} collect-virsh")
        L.append(f"#   {PY} report {opts}".rstrip())
        L.append("")
    if report.get("interrupted"):
        L.append("# Report interrupted at the stuck migrations: sections A2/A1/B were not computed.")
        if manual:
            L += ["", "# ===== To handle by hand (README, phase 5) ====="]
            L += [f"# {m}" for m in manual]
        with open(path, "w") as f:
            f.write("\n".join(L) + "\n")
        return
    pre = "# " if stop else ""
    # Deletions rely on the ABSENCE of the instance in the DB and of a libvirt domain:
    # they need the DB check and a virsh inventory that covers every hypervisor.
    del_why = []
    if not report["db_check"]:
        del_why.append("report generated with --no-db")
    if not report["virsh_check"]:
        del_why.append("no virsh inventory (--virsh)")
    elif report.get("virsh_missing"):
        vm = report["virsh_missing"]
        del_why.append(f"incomplete virsh inventory, {len(vm)} hypervisors missing: "
                       + ", ".join(vm[:10]) + (" ..." if len(vm) > 10 else ""))
    pre_del = "# " if (stop or del_why) else ""

    L.append("# ===== 1) A2 - instances that no longer exist (from the machine with the openstack CLI) =====")
    for w in del_why:
        L.append(f"# WARNING: {w}. The deletion commands (1 and 1b) are commented out:")
    if del_why:
        L.append("#   run collect-virsh on every host again (and report with DB access), then regenerate.")
    multi = {u: hs for u, hs in a2_seen.items() if len(hs) > 1}
    if multi:
        L.append("# Note: these consumers have allocations on more than one host; delete removes them all at once:")
        L += [f"#   {u}: {', '.join(hs)}" for u, hs in multi.items()]
    a2_path = os.path.join(os.path.dirname(path) or ".", "a2_consumers.txt")
    if a2_by_host:
        tot = sum(len(cs) for cs in a2_by_host.values())
        F = [f"# A2 consumers - generated by placement_reconcile.py on {report['generated']}",
             f"# {tot} consumers on {len(a2_by_host)} hosts. One UUID per line; lines starting with # are ignored,",
             "# to exclude a consumer just comment out its line."]
        for host, cs in sorted(a2_by_host.items()):
            F.append(f"# ----- {host}: {len(cs)} consumers")
            F += [f"{x}  # {note}" if note else x for x, note in cs]
        with open(a2_path, "w") as f:
            f.write("\n".join(F) + "\n")
        L += [f"#   {host}: {len(cs)} consumers" for host, cs in sorted(a2_by_host.items())]
        L.append(f"# Total {tot} consumers, listed in {a2_path} (one UUID per line, grouped by host)")
        L.append(pre_del + f"{PY} delete {opts} ".replace("  ", " ") + "@" + a2_path)
    elif os.path.exists(a2_path):
        os.unlink(a2_path)

    L += ["", "# ===== 1b) NO_HOST - existing instances without a hypervisor with a leaked allocation =====",
          "# (SHELVED_OFFLOADED or ERROR, no task_state, no libvirt domain: heal_allocations",
          "#  doesn't touch them because they have no node, the allocation must be removed; delete makes a backup)"]
    for u, x in nohost.items():
        L.append(pre_del + f"{PY} delete --no-host {opts} ".replace("  ", " ") + u
                 + f"  # {x['status']}, updated {str(x['updated'])[:16]}, stale on {', '.join(x['hosts'])}")

    di = report["global"].get("incomplete_deletes") or []
    # the evidence here comes from the API and the action history; the complete virsh inventory
    # is still needed to rule out that the domain still exists on some host
    pre_di = "# " if (stop or not report["virsh_check"] or report.get("virsh_missing")) else ""
    L += ["", "# ===== 1c) INCOMPLETE_DELETE - owner's delete that never completed =====",
          "# (terminated_at set, delete in error in the history, no libvirt domain).",
          "# First: check that the disk no longer exists on the hypervisor and that the service",
          "# is really terminated; mind the billing. Procedure in the README."]
    for x in di:
        if x["outcome"] == "DELETABLE":
            extra = (f"; later actions: {', '.join(x['later_actions'])}"
                     if x.get("later_actions") else "")
            L.append(pre_di + f"openstack server delete {x['uuid']}  # {x['name']} ({x['status']}), "
                     f"delete {x.get('delete_request')} of {str(x.get('delete_time'))[:16]} failed in "
                     f"{', '.join(x.get('delete_events_error') or ['?'])}{extra}")
        else:
            manual.append(f"INCOMPLETE_DELETE {x['uuid']} ({x['name']}, {x['status']}): "
                          + "; ".join(x["reasons"]))

    L += ["", "# ===== 2) A1 - live instances with an allocation on the wrong host (on the controller) ====="]
    L += [pre + f"docker exec nova_api nova-manage placement heal_allocations --verbose --force --instance {i}  # {w}"
          for i, w in heal_force.items()]
    L += ["", "# ===== 3) B - instances without an allocation on their own host (on the controller) ====="]
    L += [pre + f"docker exec nova_api nova-manage placement heal_allocations --verbose --instance {i}  # {w}"
          for i, w in heal.items()]
    orph = report["global"].get("orphan_domains") or []
    if manual or orph:
        L += ["", "# ===== To handle by hand (README, phase 5) ====="]
        L += [f"# {m}" for m in manual]
        L += [f"# ORPHAN_DOMAIN {x['uuid']}: " + ", ".join(f"{h} ({s})" for h, s in x["where"]) for x in orph]
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


def cmd_report(api, args):
    out = args.out or f"reconcile-{now()}"
    report, backup = analyze(api, args)
    os.makedirs(out, exist_ok=True)
    report["cli_opts"] = cli_opts(args)
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    with open(os.path.join(out, "backup_allocations.json"), "w") as f:
        json.dump(backup, f, indent=2)
    write_fix_commands(report, os.path.join(out, "fix_commands.sh"))
    print_summary(report, args.verbose)
    print(f"\nFiles written to {out}/: report.json, backup_allocations.json, fix_commands.sh")


# ---------------------------------------------------------------- delete / restore

def expand(items):
    res = []
    for x in items:
        if x.startswith("@"):
            with open(x[1:]) as f:
                res += [l.split()[0] for l in f if l.strip() and not l.startswith("#")]
        else:
            res.append(x)
    return res


def cmd_delete(api, args):
    consumers = [c for c in expand(args.consumers) if UUID_RE.match(c) or log(f"SKIP {c}: not a UUID")]
    consumers = list(dict.fromkeys(consumers))  # remove duplicates, keeping the order
    preflight(api, args, "delete")
    # A deletion relies on the ABSENCE of the instance: without the DB or with a partial virsh
    # inventory that absence is not proven.
    V = Virsh(args.virsh)
    blockers = []
    if args.no_db:
        blockers.append("--no-db: the absence of the instance in the cell DB would not be checked")
    if not V.active:
        blockers.append("--virsh missing: the absence of libvirt domains would not be checked")
    else:
        miss = virsh_missing(V, [h["hypervisor_hostname"]
                                 for h in api.compute_list("/os-hypervisors/detail", "hypervisors")])
        if miss:
            blockers.append(f"incomplete virsh inventory, {len(miss)} hypervisors missing: "
                            + ", ".join(miss[:10]) + (" ..." if len(miss) > 10 else ""))
    if blockers:
        for b in blockers:
            log("WARNING: " + b)
        if not args.force:
            sys.exit("Stopping: run collect-virsh on every host again and use DB access "
                     "(or --force, not recommended).")
        log("--force: going on anyway.")
    active = active_migrations(api)
    db = None if args.no_db else fetch_db(args, consumers)
    os.makedirs(args.backup_dir, exist_ok=True)
    for c in consumers:
        reasons = []
        nohost = False
        st, body = api.compute_call("GET", f"/servers/{c}")
        if st == 200 and args.no_host:
            probs = nohost_problems(body["server"])
            if probs:
                reasons.append("not a NO_HOST case: " + ", ".join(probs))
            else:
                nohost = True
                if not V.active:
                    reasons.append("--no-host requires --virsh")
        elif st == 200:
            s = body["server"]
            hint = " (no hypervisor: use --no-host)" if not nohost_problems(s) else ""
            reasons.append(f"answers in Nova (HTTP 200, status={s.get('status')}){hint}")
        elif st != 404:
            reasons.append(f"answers in Nova (HTTP {st})")
        elif args.no_host:
            reasons.append("404 in Nova: not a NO_HOST case, use delete without --no-host")
        # per-consumer re-check: a migration can start while delete is running
        if c in active:
            m = active[c]
            reasons.append(f"migration {m.get('status')} open (id {m.get('id')})")
        if db:
            x = db["live_by_uuid"].get(c)
            if nohost:
                if x is None:
                    reasons.append("not alive in the DB (cells and cell0)")
                elif x["host"] or x["node"]:
                    reasons.append(f"in the DB it has host={x['host']} node={x['node']}")
                if c in db["build_requests"]:
                    reasons.append("in build_requests")
            elif not args.no_host:
                if x:
                    reasons.append("alive in the cell DB")
                if c in db["build_requests"]:
                    reasons.append("in build_requests")
                if not (c in db["deleted"] or c in db["shadow"] or c in db["mig_by_uuid"]):
                    reasons.append("not found in the DB (UNKNOWN)")
        if V.hosts(c):
            reasons.append("libvirt domain on " + V.describe(c))
        if reasons and not args.force:
            print(f"SKIP {c}: " + "; ".join(reasons))
            continue
        st, full = api.placement_call("GET", f"/allocations/{c}")
        if st != 200 or not full.get("allocations"):
            print(f"SKIP {c}: no allocation in placement")
            continue
        bfile = os.path.join(args.backup_dir, f"alloc-{c}-{now()}.json")
        with open(bfile, "w") as f:
            json.dump({c: full}, f, indent=2)
        mb = sum(v["resources"].get("MEMORY_MB", 0) for v in full["allocations"].values())
        tag = " [NO_HOST]" if nohost else ""
        if args.dry_run:
            print(f"DRY  {c}{tag}: would delete {len(full['allocations'])} RP, {mb} MB (backup {bfile})")
            continue
        st, d = api.placement_call("DELETE", f"/allocations/{c}")
        print(f"{'OK  ' if st == 204 else 'ERR '} {c}{tag}: HTTP {st}, {mb} MB freed, backup {bfile}"
              + ("" if st == 204 else f" {d}"))


def cmd_restore(api, args):
    preflight(api, args, "restore")
    with open(args.backup) as f:
        data = json.load(f)
    for c in args.consumers:
        full = data.get(c)
        if not full:
            print(f"SKIP {c}: not in the backup")
            continue
        st, cur = api.placement_call("GET", f"/allocations/{c}")
        gen = cur.get("consumer_generation") if st == 200 and cur.get("allocations") else None
        if gen is not None:
            print(f"WARNING {c}: it already has allocations, they will be REPLACED")
        body = {"allocations": {rp: {"resources": v["resources"]}
                                for rp, v in full["allocations"].items()},
                "project_id": full["project_id"], "user_id": full["user_id"],
                "consumer_generation": gen}
        st, d = api.placement_call("PUT", f"/allocations/{c}", body)
        print(f"{'OK  ' if st == 204 else 'ERR '} {c}: HTTP {st}" + ("" if st == 204 else f" {d}"))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interface", default="internal", help="endpoint interface (default: internal)")
    p.set_defaults(cmd="report", host=None, out=None, verbose=False, virsh=None, stale_hours=24,
                   full=False, max_virsh_age=30, ignore_activity=False,
                   no_db=False, db_exec="docker exec -i nova_api python3 -", db_ssh=None,
                   db_user=None, db_key=False, db_sudo=None, nova_container="nova_api")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, ignore_help):
        sp.add_argument("--ignore-activity", action="store_true", help=ignore_help)
        sp.add_argument("--stale-hours", type=int, default=24,
                        help="migrations/operations stuck for more hours than this don't count as "
                             "'in progress' (migrations become 'stuck'); default 24")

    def db_opts(sp):
        sp.add_argument("--db-ssh", metavar="CONTROLLER",
                        help="reach nova_api via SSH on this controller, asking for user and password")
        sp.add_argument("--db-user", help="SSH user for the controller (asked if omitted)")
        sp.add_argument("--db-key", action="store_true", help="use SSH keys instead of the password")
        sp.add_argument("--db-sudo", dest="db_sudo", action="store_true", default=None,
                        help="use sudo on the controller (default: yes if the user is not root)")
        sp.add_argument("--no-db-sudo", dest="db_sudo", action="store_false")
        sp.add_argument("--nova-container", default="nova_api")
        sp.add_argument("--db-exec", default="docker exec -i nova_api python3 -",
                        help="alternative: command that runs Python from stdin in the nova_api container")
        sp.add_argument("--no-db", action="store_true",
                        help="skip the DB checks (not recommended; with delete it also requires --force)")
        sp.add_argument("--virsh", help="file produced by collect-virsh")

    cv = sub.add_parser("collect-virsh", help="libvirt domain inventory via SSH")
    cv.add_argument("--host", help="regex on the hypervisor name")
    cv.add_argument("--hosts", help="comma-separated list of hosts")
    cv.add_argument("--hosts-file", help="file with one host per line")
    cv.add_argument("--user", help="SSH user (asked if omitted)")
    cv.add_argument("--key", action="store_true", help="use SSH keys instead of the password")
    cv.add_argument("--sudo", dest="sudo", action="store_true", default=None,
                    help="use sudo on the node (default: yes if the user is not root)")
    cv.add_argument("--no-sudo", dest="sudo", action="store_false")
    cv.add_argument("--keep-going", action="store_true",
                    help="go on even if a host rejects the credentials")
    cv.add_argument("--ssh", default="ssh -o ConnectTimeout=10",
                    help="base ssh command (default: 'ssh -o ConnectTimeout=10')")
    cv.add_argument("--libvirt-container", default="nova_libvirt")
    cv.add_argument("--out", default="virsh_inventory.txt")
    cv.add_argument("--parallel", type=int, default=10,
                    help="hosts queried at the same time (default 10; 1 = one at a time). "
                         "The first host is always queried alone to check the credentials")
    common(cv, "skip the check for migrations in progress (with --hosts/--hosts-file "
               "the openstack CLI isn't needed either); not recommended")
    r = sub.add_parser("report", help="read-only comparison")
    r.add_argument("--host", help="regex on the hypervisor name, e.g. 'compute0[38]'")
    r.add_argument("--out", help="output directory")
    r.add_argument("-v", "--verbose", action="store_true", help="also show consistent hosts")
    r.add_argument("--full", action="store_true",
                   help="analyse every host even when there are stuck migrations")
    r.add_argument("--max-virsh-age", type=int, default=30,
                   help="warn if the virsh inventory is older than this many minutes (default 30)")
    common(r, "generate the report even with migrations/operations in progress or an outdated "
              "inventory: indicative only, every generated command is commented out")
    db_opts(r)
    d = sub.add_parser("delete", help="remove the allocations of A2 consumers (or NO_HOST with --no-host)")
    d.add_argument("consumers", nargs="+", help="UUIDs or @file with one UUID per line")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--force", action="store_true",
                   help="ignore the per-consumer safety checks (not recommended)")
    d.add_argument("--no-host", action="store_true",
                   help="consumer = existing instance without a hypervisor (SHELVED_OFFLOADED/ERROR, "
                        "no task_state, no libvirt domain, NULL node in the DB). Requires --virsh")
    d.add_argument("--backup-dir", default="placement-backup")
    common(d, "run even with migrations/operations in progress (not recommended)")
    db_opts(d)
    s = sub.add_parser("restore", help="restore allocations from a backup")
    s.add_argument("backup")
    s.add_argument("consumers", nargs="+")
    common(s, "run even with migrations/operations in progress")
    args = p.parse_args()

    if args.cmd == "collect-virsh":
        return cmd_collect_virsh(args)
    api = API(args.interface)
    {"report": cmd_report, "delete": cmd_delete, "restore": cmd_restore}[args.cmd](api, args)


if __name__ == "__main__":
    main()
