# placement_reconcile

Reconciles **OpenStack Placement** allocations with **Nova** and **libvirt**, hypervisor by hypervisor, on clouds deployed with **kolla-ansible**.

The script compares three sources:

- the allocations recorded in Placement;
- the instances according to Nova (API and cell databases, cell0 included);
- the libvirt domains that actually exist on the compute nodes.

When the sources disagree, the order of trust is **virsh > Nova DB > Placement**.

The script never fixes anything on its own. `report` is read-only and produces a file of commands that you review and run by hand. The only operations that write are `delete` and `restore`, and both always save a backup of the allocation before touching it.

> ⚠️ This tool works on production data (Placement, Nova databases). Read the whole procedure before using it, and never run the generated commands blindly.

## Contents

- [Why it exists](#why-it-exists)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Full procedure](#full-procedure)
  - [Phase 1: back up the placement database](#phase-1-back-up-the-placement-database)
  - [Phase 2: operations in progress](#phase-2-operations-in-progress)
  - [Phase 3: libvirt inventory and report](#phase-3-libvirt-inventory-and-report)
  - [Phase 4: fixes](#phase-4-fixes)
  - [Phase 5: cases to handle by hand](#phase-5-cases-to-handle-by-hand)
  - [Phase 6: final check](#phase-6-final-check)
- [Classes](#classes)
- [Restore](#restore)
- [Options](#options)
- [Known limitations](#known-limitations)

## Why it exists

Nova and Placement are two separate databases with no distributed transaction between them. Every operation that moves a VM (migration, resize, evacuate, delete) writes to both, at different moments. If something breaks between the two writes, the mismatch stays: since Queens, nova-compute no longer corrects allocations, and `heal_allocations` adds missing allocations but does not remove orphaned ones.

The typical result is hypervisors that look full to the scheduler when they are not, or the other way round. The most common causes:

- migrations interrupted by RPC timeouts or container restarts;
- evacuated nodes that come back with a different hostname or UUID;
- cold migrations and resizes that were never confirmed (VERIFY_RESIZE);
- manual edits to the Nova database, or `reset-state`.

## Requirements

- Python 3.7+ on the machine running the script (standard library only).
- `openstack` CLI with admin credentials, usually on the kolla-ansible deploy host.
- SSH access to a controller and to the hypervisors, with `sudo` if you don't log in as root.
- Standard kolla containers: `nova_api` on the controller and `nova_libvirt` on the hypervisors. Change the names with `--nova-container` and `--libvirt-container`.
- No migration or operation in progress on instances (see [Phase 2](#phase-2-operations-in-progress)). The script checks this by itself.

## Quick start

```bash
source /etc/kolla/admin-openrc.sh
openstack token issue -f value -c expires        # must answer without errors

python3 placement_reconcile.py collect-virsh --user <user>
python3 placement_reconcile.py report --virsh virsh_inventory.txt --db-ssh <controller>
```

The report creates a `reconcile-<date-time>` directory with the results and a `fix_commands.sh` to review. Run every command from the same directory, because the generated paths are relative.

Follow the full procedure before fixing anything.

## Full procedure

### Phase 1: back up the placement database

Take the backup before anything else, on a controller whose MariaDB node is in sync with the Galera cluster. On the controller:

```bash
# database credentials from the configuration rendered by kolla
dbcred() {  # dbcred <container> <conf file> <section> <username|password>
  sudo docker exec "$1" python3 -c 'import configparser, sys
from urllib.parse import urlsplit, unquote
c = configparser.RawConfigParser(strict=False); c.read(sys.argv[1])
u = urlsplit(c.get(sys.argv[2], "connection"))
print(unquote(getattr(u, sys.argv[3])))' "$2" "$3" "$4"
}
NOVA_PW=$(dbcred nova_api /etc/nova/nova.conf database password)
PL_PW=$(dbcred placement_api /etc/placement/placement.conf placement_database password)
[ -n "$NOVA_PW" ] && [ -n "$PL_PW" ] && echo "credentials read" || echo "ERROR: empty credentials"

# IP this controller's MariaDB listens on
sudo docker exec mariadb grep -rhE '^\s*bind[-_]address' /etc/mysql/ 2>/dev/null
NODE_IP=<address printed above>

# the node must be in sync with the cluster
sudo docker exec -e MYSQL_PWD="$NOVA_PW" mariadb mysql -h "$NODE_IP" -u nova -e \
  "SHOW STATUS WHERE Variable_name IN ('wsrep_local_state_comment','wsrep_cluster_size');"

# dump of the placement database
sudo docker exec -e MYSQL_PWD="$PL_PW" mariadb mysqldump -h "$NODE_IP" -u placement \
  --single-transaction --no-tablespaces placement > ~/placement-$(date +%F-%H%M).sql

# check: the file is complete and contains the allocations
F=$(ls -t ~/placement-*.sql | head -1)
tail -1 "$F"                                 # "-- Dump completed on ..."
awk '/^INSERT INTO/ {t=$3; next} t && /^\(/ {n[t]++} /;$/ {t=""} END {for (k in n) print n[k], k}' "$F" | sort -k2
```

Check three things before going on:

- `wsrep_local_state_comment` must be `Synced` and `wsrep_cluster_size` must match the number of controllers. If the node is not in sync, take the dump from another controller.
- The last line of the file must be `-- Dump completed on ...`. If it's missing, the dump was interrupted.
- There must be rows for `allocations`, `consumers`, `resource_providers` and `inventories`.

The `dbcred` function and the `NOVA_PW` and `NODE_IP` variables are needed again in Phase 4. If you disconnect from the controller, define them again.

### Phase 2: operations in progress

During a migration, a resize or a build, Placement, Nova and libvirt can legitimately disagree. Fixing things at that moment risks removing valid allocations or creating duplicates. So every command of the script starts with a check, and stops if it finds any of these:

- open migrations (`queued`, `preparing`, `running` and similar) that have not been stuck for more than `--stale-hours` (default 24 hours);
- cold migrations in `finished` state with the instance in `VERIFY_RESIZE`, i.e. waiting for confirmation. These block at any age;
- instances with a `task_state` that moves, creates or frees allocations (build, resize, migrate, shelve, delete, rebuild). Snapshot and reboot don't block.

To see the situation before starting:

```bash
openstack server migration list -f json | python3 -c '
import json, sys
done = {"completed","confirmed","reverted","error","failed","done","cancelled"}
for m in json.load(sys.stdin):
    if (m.get("Status") or "").lower() not in done:
        print(m["Id"], m["Server UUID"], m["Type"], m["Status"],
              m["Source Node"], "->", m["Dest Node"], m["Created At"])'
```

Despite the name, for a cold migration `finished` is **not** a final state:

| State | Meaning | Done |
|---|---|---|
| `queued`, `preparing`, `running`, `migrating`, `post-migrating` | Migration in progress | No |
| `finished` | Instance on the destination in VERIFY_RESIZE; the shut-off domain and the allocation are still on the source | No, waiting for confirmation |
| orphaned `finished` | The instance is no longer in VERIFY_RESIZE (or no longer exists), but the record is still open | No, the record must be closed |
| `completed`, `confirmed`, `reverted` | Migration finished | Yes |
| `error`, `failed`, `cancelled` | Migration failed or cancelled | Yes |

`finished` migrations must be closed by whoever started them, after checking that the instance works on the destination:

```bash
openstack server show <uuid> -c status          # must be VERIFY_RESIZE
openstack server resize confirm <uuid>          # or: openstack server resize revert <uuid>
```

With `resize_confirm_window = 0`, the default, Nova never confirms on its own. As long as they are pending, the source host keeps both the shut-off domain and the migration's allocation.

An orphaned `finished` can't be fixed with `resize confirm`, which would be rejected because the instance is not in the right state. The script treats it like any other open migration: it blocks only while recent, and after `--stale-hours` it becomes a stuck migration, to be closed with `close_migrations.sql` (Phase 4, section 0b).

Instances with a `task_state` stuck for more than `--stale-hours` don't block the script. They show up among the cases to handle by hand as `STUCK_OPERATION`.

`--ignore-activity` skips the check. With `report` the result becomes indicative only and every generated command is commented out. Use it only to get an idea during an emergency, never to fix anything.

### Phase 3: libvirt inventory and report

Run the two commands one right after the other. The inventory is a snapshot: if too much time passes before the report, instances created, deleted or migrated in the meantime distort the comparison.

```bash
python3 placement_reconcile.py collect-virsh --user <user>
python3 placement_reconcile.py report --virsh virsh_inventory.txt --db-ssh <controller>
```

`collect-virsh` asks for the password once and queries the `nova_libvirt` container on every hypervisor. Hosts are queried in parallel, 10 at a time by default (`--parallel`), with these safeguards:

- the first host is queried alone, so a wrong password produces a single attempt and doesn't lock the account;
- if a later host rejects the credentials, the hosts not yet queried are cancelled and no inventory is written;
- hosts that answer with an error are marked as such in the file and excluded from the virsh checks.

With SSH keys, add `--key`.

**Deletions need an inventory that covers every hypervisor.** The A2, NO_HOST and INCOMPLETE_DELETE fixes rely on the absence of a libvirt domain, and that absence is not proven if the host where the domain could be was not queried. If even one hypervisor is missing, the report comments out the deletion commands and `delete` refuses to start.

To restrict the analysis to some hosts, `collect-virsh` and `report` accept `--host` with a regex on the hypervisor name. The regex matches anywhere in the name, so always include the part that identifies the AZ or cluster. To check what it selects:

```bash
openstack hypervisor list -f value -c "Hypervisor Hostname" | grep -E '<regex>'
```

A partial inventory is fine to get an idea, not to delete anything.

`report` stops without producing anything in two cases:

- the Phase 2 check finds operations in progress;
- the virsh inventory is older than migrations that finished after it was collected. Just run `collect-virsh` again and then `report` right after.

At the end of the analysis the report repeats the check. If a migration started in the meantime, the report is marked as invalid and every generated command is commented out.

#### Output files

| File | Contents |
|---|---|
| `report.json` | Full classification detail, host by host |
| `backup_allocations.json` | Copy of the allocations of every consumer that is a candidate for a fix |
| `fix_commands.sh` | Fix commands in the right order, preceded by `exit 1` |
| `fix_mappings.sql` | Only if some `instance_mapping` points to the wrong cell |
| `a2_consumers.txt` | A2 consumers, one per line, grouped by host |
| `close_migrations.sql` | Only if there are stuck migrations that can be closed |

#### On-screen summary

The summary shows one line per hypervisor with anomalies (every host with `-v`):

| Column | Meaning |
|---|---|
| A1 | Allocations of live instances that run on another host |
| A2 | Allocations of instances that no longer exist |
| A2 GiB | RAM held by A2 allocations, i.e. how much will become available |
| NOHOST | Allocations of instances without a hypervisor (NO_HOST) |
| INCDEL | Instances with a delete that never completed (INCOMPLETE_DELETE) |
| B | Instances on the host without an allocation |
| CHECK | Cases to handle by hand |
| STUCK | Allocations of stuck migrations |

### Phase 4: fixes

Open `fix_commands.sh` and read all of it before running anything. Run the commands by hand, one section at a time, in the order of the file. Don't remove `exit 1` to run the whole file.

If the file starts with a **STOP** block, sections 1-3 are commented out. The block states the reason: broken mappings, stuck migrations, forced or invalid report. Fix that first and regenerate the report before going on.

The commands in `fix_commands.sh` have no `sudo`. Add it if you don't log in to the controller as root.

#### 0) Broken mappings

These are live instances in a cell database whose `instance_mapping` in `nova_api` is missing or points to the wrong cell. The report doesn't rely on the symptom (404 or API errors): it compares the cell databases directly with `nova_api.instance_mappings`. Mappings must be fixed before any fix on Placement, and only for a problem the report has confirmed: don't run `map_instances` just in case.

The two cases are independent. If the report finds both, you need both fixes, in any order.

**Missing mapping** (`type: MISSING`). Fixed with `map_instances`, which creates the missing mappings without touching existing ones. The report already includes the command with the right cell UUID:

```bash
sudo docker exec nova_api nova-manage cell_v2 map_instances --cell_uuid <cell_uuid>
```

**Mapping to the wrong cell** (`type: WRONG`). `map_instances` doesn't help, because it skips instances that already have a mapping. The report generates `fix_mappings.sql`, which updates `cell_id` only if the current value is still the one the report read. First check each instance:

```bash
sudo docker exec nova_api nova-manage cell_v2 verify_instance --uuid <uuid>
```

Then, on the controller, with `dbcred` and `NODE_IP` from Phase 1:

```bash
API_USER=$(dbcred nova_api /etc/nova/nova.conf api_database username)
API_PW=$(dbcred nova_api /etc/nova/nova.conf api_database password)

# backup of the instance_mappings table
sudo docker exec -e MYSQL_PWD="$API_PW" mariadb mysqldump -h "$NODE_IP" -u "$API_USER" \
  --single-transaction --no-tablespaces nova_api instance_mappings > ~/nova_api-instance_mappings-$(date +%F-%H%M).sql
tail -1 "$(ls -t ~/nova_api-instance_mappings-*.sql | head -1)"    # "-- Dump completed on ..."

# copy the file to the controller (scp), then into the container
sudo docker cp fix_mappings.sql mariadb:/tmp/fix_mappings.sql
sudo docker exec -it -e MYSQL_PWD="$API_PW" mariadb mysql -h "$NODE_IP" -u "$API_USER" nova_api
```

Before running `source`, open `fix_mappings.sql` and check:

- that the UUIDs match the output of `verify_instance`;
- for each row, the current `cell_id` (the `<=>` condition) and the new one (`SET cell_id`);
- the number of expected rows in the final `SELECT`;
- that there are no statements other than `START TRANSACTION`, `UPDATE instance_mappings` and `SELECT`.

Then, in the mysql session:

```sql
source /tmp/fix_mappings.sql
-- the last SELECT shows rows_fixed and rows_expected
COMMIT;      -- only if the two numbers match
-- ROLLBACK; -- otherwise, then regenerate the report
```

Remove the file from the container: `sudo docker exec -u root mariadb rm /tmp/fix_mappings.sql`.

In both cases, regenerate inventory and report before going on: with the mapping fixed, the API sees instances it couldn't see before, and the classification changes.

#### 0b) Stuck migrations

The report stops here if it finds open migrations stuck for more than `--stale-hours`. For each one it checks where the instance really is, according to Nova and to virsh, and classifies it:

- **CLOSABLE**: the instance is consistent between Nova and libvirt and the record can be closed. It goes into `close_migrations.sql`.
- **CHECK**: something doesn't add up. No SQL is generated and the reasons are listed at the end of `fix_commands.sh`.

`finished` migrations waiting for confirmation are never considered stuck: confirm them as described in Phase 2. Orphaned `finished` migrations are: in the summary they show up with `[orphaned finished]` and in `close_migrations.sql` they have a block of their own.

On the controller, with the Phase 1 variables:

```bash
# backup of the cell database migrations table
sudo docker exec -e MYSQL_PWD="$NOVA_PW" mariadb mysqldump -h "$NODE_IP" -u nova \
  --single-transaction --no-tablespaces nova migrations > ~/nova-migrations-$(date +%F-%H%M).sql
tail -1 "$(ls -t ~/nova-migrations-*.sql | head -1)"    # "-- Dump completed on ..."

# copy the file to the controller (scp), then into the container
sudo docker cp close_migrations.sql mariadb:/tmp/close_migrations.sql
sudo docker exec -it -e MYSQL_PWD="$NOVA_PW" mariadb mysql -h "$NODE_IP" -u nova nova
```

The file can contain up to two blocks, each with its own `UPDATE` and its own check `SELECT`: one for stuck migrations, one for orphaned `finished` migrations. Before running `source`, check:

- for each migration, in the comment, that ID, instance, state, and the Nova and virsh hosts are what you expect. For orphaned `finished` migrations the instance state must not be VERIFY_RESIZE;
- that the UUIDs in the `IN` clause of each block are the same as in that block's comments;
- the number of expected rows in each block's `SELECT`;
- that there are no statements other than `START TRANSACTION`, `UPDATE migrations` and `SELECT`.

```sql
source /tmp/close_migrations.sql
-- each block shows rows_changed and rows_expected
COMMIT;      -- only if the two numbers match in every block
-- ROLLBACK; -- otherwise, then regenerate the report
```

Remove the file from the container: `sudo docker exec -u root mariadb rm /tmp/close_migrations.sql`.

Then regenerate **both the inventory and the report**. Re-running the report alone is not enough: closing the records updates the migrations' `updated_at`, so they appear to have finished after the old inventory was collected, and the report would stop because the inventory is out of date. In the new report the allocations of the closed migrations show up as A2. Continue from the new `fix_commands.sh`, not the old one.

#### 1) A2: instances that no longer exist

A single command deletes every A2 allocation, reading the UUIDs from `a2_consumers.txt`. To exclude a consumer, comment out its line. Always run the dry run first:

```bash
python3 placement_reconcile.py delete --db-ssh <controller> --virsh virsh_inventory.txt \
  --dry-run @reconcile-<date-time>/a2_consumers.txt

python3 placement_reconcile.py delete --db-ssh <controller> --virsh virsh_inventory.txt \
  @reconcile-<date-time>/a2_consumers.txt
```

`delete` only starts with database access and a virsh inventory that covers every hypervisor. Before deleting, it re-checks each consumer: Nova API, open migrations, cell database, `build_requests` and virsh inventory. If something doesn't add up it prints `SKIP` with the reason and moves on. Understand a SKIP, don't work around it: `--force` exists, but it doesn't belong in this procedure.

The backup of each deleted allocation goes to `placement-backup/alloc-<uuid>-<date-time>.json`.

#### 1b) NO_HOST: instances without a hypervisor

These are instances that exist in Nova but have no hypervisor: usually in ERROR in cell0 after a failed scheduling, or SHELVED_OFFLOADED. They shouldn't have allocations, and `heal_allocations` doesn't touch them because they have no node. So the allocation has to be removed:

```bash
python3 placement_reconcile.py delete --no-host --db-ssh <controller> --virsh virsh_inventory.txt --dry-run <uuid>
python3 placement_reconcile.py delete --no-host --db-ssh <controller> --virsh virsh_inventory.txt <uuid>
```

The instance stays as it is: only the Placement allocation is removed. `--no-host` requires `--virsh` and rejects the instance if it has a `task_state`, a domain on any host, or a node in the database.

#### 1c) INCOMPLETE_DELETE: deletions that never completed

These are instances deleted by their owner whose delete broke off before the last step, for example because of a communication error with the conductor. Nova removes the record only at the very end of the deletion, so domain, disks, ports and allocation are already gone, but the instance stays in Nova with `terminated_at` set. A later `reset-state` can make it look like any other stopped VM, and every attempt to migrate it fails.

The script recognises them from three things:
- `terminated_at` is set;
- the latest delete in the action history has an event in error;
- there is no libvirt domain.

If the delete was requested by the owning project, the script marks the instance as `DELETABLE` and generates the command; otherwise it sends it to Phase 5 with the reason. These instances are never proposed for `heal_allocations`, which would recreate the allocation for a VM that no longer exists.

Before running the command:

```bash
# details of the failed delete (request ID from the comment in fix_commands.sh)
openstack server event show <uuid> <request-id>

# the disk must no longer exist, on the hypervisor Nova reports
sudo docker exec nova_compute ls -la /var/lib/nova/instances/<uuid>

# the ports must no longer exist
openstack port list --device-id <uuid>
```

If the disk still exists, don't delete: the VM can still be recovered and the owner must be consulted. Otherwise:

```bash
openstack server delete <uuid>
openstack server show <uuid>        # expected: 404 after a few seconds
```

If the delete fails again, `openstack server event list <uuid>` shows the cause. As in sections 1 and 1b, these commands are commented out if the virsh inventory doesn't cover every hypervisor.

#### 2) A1: allocation on the wrong host

On the controller:

```bash
sudo docker exec nova_api nova-manage placement heal_allocations --verbose --force --instance <uuid>
```

`--force` replaces the instance's allocations with the ones computed from its current host according to Nova. The comment next to each command shows where the instance lives and on which host the allocation is stale. If it includes a virsh note, check that first.

#### 3) B: instances without an allocation

On the controller:

```bash
sudo docker exec nova_api nova-manage placement heal_allocations --verbose --instance <uuid>
```

Without `--force`: it creates the allocation only if it's missing.

### Phase 5: cases to handle by hand

The section at the end of `fix_commands.sh` lists the cases the script can't fix by itself. Each one needs a check on the real instance. Don't run any of the following commands without knowing who uses the VM and what the impact will be.

```bash
openstack server show <uuid> -c status -c OS-EXT-STS:task_state -c OS-EXT-SRV-ATTR:hypervisor_hostname -c project_id
openstack server migration list --server <uuid>

# on the hypervisor
sudo docker exec nova_libvirt virsh dominfo <uuid>
sudo docker exec nova_libvirt virsh domblklist <uuid>
sudo docker exec nova_libvirt virsh domjobinfo <uuid>
```

**NOVA_VIRSH.** Nova and Placement say the instance is on host X, but virsh says otherwise.
- If the domain exists nowhere, the VM isn't running even if Nova shows it ACTIVE. Usually `openstack server reboot --hard <uuid>` recreates the domain on the host recorded in Nova. Agree on it with the owner, because the VM restarts.
- If the domain is running on a host other than X, Nova has the wrong host, typically after a migration that went wrong. The cell database must be fixed with a separate procedure. **Don't hard reboot**: Nova would try to recreate the VM on X while it's still running on the other host.

Before shutting down a hypervisor for maintenance, always check `virsh list --all` on the host itself, not just the list of instances in Nova. VMs that Nova places elsewhere are not moved by an evacuation and stop with the host.

**DUPLICATE_DOMAIN.** The same UUID is defined on more than one hypervisor: usually a running copy and a shut-off one left over from a migration. The shut-off copy blocks future migrations to that host. After checking which copy is the active one (the one Nova reports, running), remove the shut-off definition on the host that holds it:

```bash
sudo docker exec nova_libvirt virsh domblklist <uuid>    # check the disks first
sudo docker exec nova_libvirt virsh undefine <uuid>
```

With Ceph-backed disks the shut-off copy points to the same RBD images as the active one. `virsh undefine` with no options only removes the definition. **Never use `--remove-all-storage`.**

**ORPHAN_DOMAIN and VIRSH_ALIVE.** Domains present on a hypervisor but unknown to Nova. If they are running, they use RAM and CPU the scheduler can't see, with a real risk of overcommit. Identify the VM from its name, its disks (`virsh domblklist`) and the Nova metadata in the XML (`virsh dumpxml <uuid> | grep -A5 nova:instance`). Then search for the UUID in every cell database and among deleted instances. What to do with it must be decided with the owner.

**DB_ALIVE.** The instance is alive in the cell database but the API returns 404. The note on the line says which case applies, based on `global.mapping`:
- missing mapping: fix it with `map_instances` (section 0);
- mapping to the wrong cell: fix it with `fix_mappings.sql` (section 0);
- correct mapping: the 404 has another cause and the mapping must not be touched. Check with `nova-manage cell_v2 verify_instance`, the `nova_api` logs and whether the cell database is reachable.

In all three cases `delete` won't remove the allocation, because it finds the instance alive in the database.

**UNKNOWN and UNCERTAIN.**
- UNCERTAIN: the API answered with a temporary error. Regenerate the report; if the error persists, check the `nova_api` logs and the state of the cells.
- UNKNOWN: the consumer exists neither in the API nor in the database. It can be a consumer of another service or a very old leftover. `backup_allocations.json` shows the project and user it belongs to.

**B_ERROR, A1_CHECK, B_CHECK, STUCK_OPERATION.** Instances in ERROR, in unexpected states, or with operations stuck for hours; the reason is on the line. They usually need to be brought back to a consistent state, then the report is regenerated. Before changing an instance's state, look at its history with `openstack server event list <uuid>` and understand why the last operation failed. A state change doesn't fix the cause, it hides it.

**INCOMPLETE_DELETE to check.** The script doesn't generate the command in three cases:
- the delete was requested by a project other than the owner;
- a libvirt domain still exists;
- the history has no delete, or no event in error.

Once these points are clear, proceed as in section 1c.

**STUCK_MIG to check.** Stuck migrations for which the script generates no SQL. The reasons say what doesn't add up: instance in ERROR, `task_state` set, domain on a host other than Nova's. Fix the instances first; at the next report the migrations become closable.

### Phase 6: final check

```bash
python3 placement_reconcile.py collect-virsh --user <user>
python3 placement_reconcile.py report --virsh virsh_inventory.txt --db-ssh <controller>
```

Columns A1, A2, NOHOST, INCDEL and B must be zero. Only the Phase 5 cases not yet resolved should remain.

As a cross-check you can use Nova's own audit (since Ussuri), which is read-only without `--delete`:

```bash
sudo docker exec nova_api nova-manage placement audit --verbose
```

## Classes

| Class | Meaning | Action |
|---|---|---|
| A1 | Allocation on this host, but the instance is alive and runs elsewhere | `heal_allocations --force` |
| A2 | Instance doesn't exist: 404 from the API, deleted or in shadow tables in the database, no domain | `delete` |
| NO_HOST | Existing instance without a hypervisor (SHELVED_OFFLOADED or ERROR, usually in cell0) with a leftover allocation | `delete --no-host` |
| B | Instance on the host without an allocation on its resource provider | `heal_allocations` |
| B_ERROR | Like B, but the instance is in ERROR | By hand |
| A1_CHECK, B_CHECK | Nova and virsh disagree, or unexpected state, or stuck `task_state` | By hand |
| NOVA_VIRSH | Nova and Placement agree, virsh doesn't | By hand |
| DUPLICATE_DOMAIN | The same UUID is defined on more than one hypervisor | By hand |
| ORPHAN_DOMAIN | libvirt domain unknown to Nova | By hand |
| VIRSH_ALIVE | Consumer unknown to Nova, but the domain exists | By hand |
| DB_ALIVE | 404 from the API but the instance is alive in the cell database | Depends on the mapping |
| UNKNOWN | Found neither in the API nor in the database | By hand |
| UNCERTAIN | The API answered with an error other than 404 (500, 503) | Run the report again |
| STUCK_MIGRATION | Allocation of an open migration stuck for more than `--stale-hours`, orphaned `finished` included | `close_migrations.sql` |
| STUCK_OPERATION | Instance with a `task_state` stuck for more than `--stale-hours` | By hand |
| INCOMPLETE_DELETE | `terminated_at` set and a failed delete in the history: deletion started and never completed | `openstack server delete` |

## Restore

To restore the allocations of one or more consumers deleted with `delete`:

```bash
python3 placement_reconcile.py restore placement-backup/alloc-<uuid>-<date-time>.json <uuid>
```

You can also use the report's `backup_allocations.json`, which holds every candidate:

```bash
python3 placement_reconcile.py restore reconcile-<date-time>/backup_allocations.json <uuid> [<uuid> ...]
```

If the consumer already has allocations, `restore` replaces them and says so.

Restoring the whole placement database from the Phase 1 dump is the last resort. It also overwrites every allocation created after the backup (new instances, migrations), so it must be agreed on and done with the `placement_api` services stopped.

## Options

| Option | Commands | Effect |
|---|---|---|
| `--host <regex>` | collect-virsh, report | Restricts the analysis to hypervisors whose name matches the regex |
| `--hosts`, `--hosts-file` | collect-virsh | Explicit list of hosts to query (no `openstack` CLI needed) |
| `--user`, `--key` | collect-virsh | SSH user; `--key` uses keys instead of the password |
| `--parallel <n>` | collect-virsh | Hosts queried at the same time. Default 10; with 1 the collection is sequential |
| `--db-ssh <controller>` | report, delete | Queries the Nova database through the controller's `nova_api` container |
| `--no-db` | report, delete | Skips the database checks. With `report` the deletion commands are commented out; `delete` refuses to start without `--force` |
| `--virsh <file>` | report, delete | Inventory produced by `collect-virsh` |
| `--stale-hours <n>` | all | Past this age, migrations and operations no longer count as in progress. Default 24 |
| `--max-virsh-age <min>` | report | Warns if the inventory is older than this. Default 30 |
| `--full` | report | Analyses every host even when there are stuck migrations |
| `-v` | report | Also shows hosts without anomalies in the summary |
| `--dry-run` | delete | Shows what would be deleted without doing it |
| `--no-host` | delete | Allows deletion for existing instances without a hypervisor |
| `--force` | delete | Skips the safety checks. Not for the normal procedure |
| `--ignore-activity` | all | Skips the operations-in-progress check. Emergencies only |

The full list is in `python3 placement_reconcile.py --help`.

## Known limitations

- Designed and tested on kolla-ansible (Docker containers, MariaDB/Galera). On other deployments (Charmed, OpenStack-Ansible, native packages) the commands that reach the databases and libvirt need to be adapted.
- Only the compute nodes' resource providers are considered. Nested resource providers (vGPU, guaranteed network bandwidth) and allocations from other services are not handled; the latter show up as UNKNOWN.
- Templated `database_connection` values in `cell_mappings` are not supported.
- The virsh inventory is a snapshot: on very busy clusters, collect it and use it within a few minutes.
