# Single-VM Production Compose

`docker-compose.prod.yaml` is the supported Docker Compose path for a small,
single-VM self-hosted installation. It is an overlay on the existing local
stack, not a replacement for Kubernetes.

## Start the stack

Install Docker Engine and the Compose plugin, point a DNS record at the VM, and
make sure ports 80 and 443 are reachable from the internet. Then run:

```bash
cd boxkite
export BOXKITE_DOMAIN=boxkite.example.com
export SIDECAR_AUTH_TOKEN="$(openssl rand -hex 32)"
docker compose -f deploy/docker-compose.yml \
  -f deploy/docker-compose.prod.yaml up -d --build
```

Caddy obtains and renews a certificate automatically for `BOXKITE_DOMAIN`.
The sidecar still requires `SIDECAR_AUTH_TOKEN` for every non-health request.

The overlay applies bounded CPU and memory limits to the sandbox, sidecar,
MinIO, and Caddy, plus Docker's JSON log rotation (`10m` per file, five files
per container). The sandbox starts with 4 GiB and 2 CPUs, matching the current
warm-pool sizing baseline; reduce or increase those values only after checking
the workload and host capacity.

## Run a backup

The backup service archives the named Compose volumes and, when `DATABASE_URL`
is set, runs `pg_dump` against the control-plane database. The current base
compose stack does not run Postgres itself, so an external Postgres connection
is optional rather than silently assumed:

```bash
export DATABASE_URL='postgresql://user:password@host.docker.internal:5432/boxkite'
docker compose -f deploy/docker-compose.yml \
  -f deploy/docker-compose.prod.yaml \
  --profile backup run --rm backup
```

Backups are stored in the `backups` Docker volume. Set
`BACKUP_RETENTION_DAYS` to change the default 14-day retention. Copy that
volume to storage outside the VM on a schedule; a backup that remains only on
the same disk is not disaster recovery.

For a simple cron job, use the same `docker compose ... --profile backup run
--rm backup` command. Test both the backup and a restore procedure before
putting the VM into service.

## When to use Kubernetes instead

Use this Compose path when one VM is enough and the installation serves only a
handful of concurrent sandboxes. Move to Kubernetes when concurrency regularly
approaches the VM's CPU or memory ceiling, when you need multiple worker nodes
or automatic horizontal scaling, or when sessions require stronger per-pod
isolation, independent scheduling, or failure domains. Kubernetes is also the
right path for a multi-tenant service where one customer's workload must not
share the same static Compose sandbox boundary with another's.

This overlay does not turn the Compose runtime into Kubernetes: it does not
create one pod per session, and it does not add Kubernetes NetworkPolicy or
pod-security admission. Keep those boundaries in mind when deciding whether a
single VM is appropriate.
