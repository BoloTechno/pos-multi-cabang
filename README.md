# Bolo POS · multi-branch operations

A first working version of a cloud-ready, shared-database POS for **one business with multiple branches**. Django renders a responsive web UI; PostgreSQL is required for production stock locking. SQLite supports a quick, single-user development preview.

## Included workflows

- Session login and CSRF-protected forms. Head office (superuser) manages users, memberships, products and locations in `/admin/`.
- Branch cashiers sell multiple products at catalog prices, with exact decimal totals, cash received, change and a receipt. Prices and product names are snapshotted on sale lines.
- Warehouse managers receive external deliveries with a reference, then dispatch single-product shipments to branches.
- Dispatch deducts warehouse on-hand stock; the shipment remains **in transit** until the destination manager confirms full physical receipt. In-transit stock cannot be sold.
- Managers reconcile physical stock against a versioned balance. A stale count is rejected if any intervening stock movement occurred, including movements that return to the original quantity.
- Stock movements retain operator, time, quantity delta, resulting balance, reason and operation ID. Operational records are read-only in the admin interface.
- Branch reports filter dates and branch, export CSV, and show sales count, revenue, units sold/received, count adjustments and **current** on-hand stock. Branch employees only see assigned locations.

## Quick start (local, SQLite)

Requires Python 3.14. Run from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DEBUG=true
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000`. Sign in, then use Administration to add:

1. A central warehouse and one or more branch locations.
2. Products with unique SKUs and prices (one shared catalog and currency).
3. Users, then manager/cashier memberships at the appropriate locations. Only head-office users should be superusers; branch users do not need staff status.

Use **Inventory → Receive stock**, **Distribution → Dispatch stock**, sign in as the destination manager to **Confirm full receipt**, then **Point of sale**. Use **Inventory → Record count** for physical reconciliation.

### Optional demo data

On a development database only (`DEBUG=true`), choose a strong password and enter it without storing it in shell history:

```bash
read -rs -p 'Demo password: ' DEMO_PASSWORD
export DEMO_PASSWORD
python manage.py seed_demo
unset DEMO_PASSWORD
```

Creates `owner`, `north-manager`, `north-cashier`, and `south-manager`, two branches, a warehouse, three products, traced opening stock, transfers and sample cash sales. Newly created users share the supplied development password. Re-running does not repeat stock movements or reset existing passwords. Never run demo seeding on a production database.

## PostgreSQL development with Docker

```bash
cp .env.example .env
# Set strong SECRET_KEY and URL-safe POSTGRES_PASSWORD values in .env.
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Open `http://127.0.0.1:8000`. Compose is a **local development** profile with DEBUG enabled and a persistent PostgreSQL volume. The application process does not run migrations automatically: execute migrations once per release before serving new code. Do not delete the database volume to upgrade.

## Cloud deployment

Build the Docker image and deploy it to a container host with managed PostgreSQL. The app listens on port 8000 via Gunicorn. Configure secrets through the host, not through committed files:

| Variable | Production value |
| --- | --- |
| `DEBUG` | `false` (default) |
| `SECRET_KEY` | Unique random secret; keep stable across replicas |
| `DATABASE_URL` | PostgreSQL URL, with provider-required TLS settings (e.g. `?sslmode=require`) |
| `ALLOWED_HOSTS` | Comma-separated exact hostnames; no scheme |
| `CSRF_TRUSTED_ORIGINS` | Public HTTPS origin(s) |
| `TIME_ZONE` | Business reporting timezone; default `UTC` |
| `CURRENCY` | Shared reporting currency label; default `IDR` |
| `TRUST_PROXY` | `true` only if a trusted TLS proxy strips client forwarding headers and supplies `X-Forwarded-Proto` |

Before enabling traffic, run `python manage.py migrate`, `python manage.py createsuperuser`, and `python manage.py check --deploy --fail-level WARNING` with production settings. Static assets are collected during the Docker build and served by WhiteNoise. `/health/` is an unauthenticated process liveness endpoint; it does not attest database readiness. Set up managed database backups and restore testing, HTTPS, monitoring, and login throttling at your ingress. Do not expose PostgreSQL publicly.

The production settings refuse SQLite, require a secret, disable debug, and enable secure cookies, HTTPS redirection and HSTS. See Django's [deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/).

## Verification

```bash
export DEBUG=true
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test -v 2
python manage.py collectstatic --noinput
```

To exercise real concurrency tests, export a PostgreSQL `DATABASE_URL` before running tests; the database user needs permission to create a test database. SQLite skips the PostgreSQL row-locking tests. GitHub Actions runs both database configurations, migration drift checks, static collection, the production settings check and a Docker image build. It tests concurrent checkout against limited stock and simultaneous receipts of the same transfer.

## Transaction design and review scope

Every stock command uses `transaction.atomic()` and locks affected location rows with `SELECT ... FOR UPDATE` in ascending ID order. This serializes stock writes within a location, including first-time stock creation. Transfers lock both locations; receipt then locks the shipment. Insufficient stock rolls back the entire command. Database constraints prevent negative balances and duplicate balance/ledger rows. PostgreSQL is required because [SQLite does not implement these row locks](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update).

Each submitted command has a UUID idempotency key bound to its actor, kind and payload fingerprint. Retrying the same command returns its original result; reusing a key with different data is rejected. This protects HTTP retries and repeated form submission, rather than detecting separately entered duplicate business transactions.

Review the service layer in `pos/services.py`, access scoping in `pos/views.py`, and regression/concurrency tests in `pos/tests/test_pos.py`. Stock must only be changed through the command services. The admin blocks operational record edits/deletions; database administrators and custom ORM scripts can still alter records, so this is an application audit trail, not a tamper-proof accounting ledger.

## Initial-version boundaries

- One organization and one currency per deployment; not a multi-tenant SaaS. Locations and users share one database.
- Whole-unit stock, up to one billion units per balance. Cash payments only; no card processing, tax engine, discounts, returns, voids, purchasing/accounts payable, accounting integration, barcode hardware integration, offline sync or negative stock.
- Transfers contain one product and must be received in full. Partial deliveries, loss in transit, cancellation and return-to-warehouse workflows are not implemented. Do not confirm a shipment that has discrepancies; investigate it first.
- No count approval workflow or stock reservations during counting. If stock changes, the count must be reloaded and redone. Cashiers can view reports for their own assigned branches; managers additionally handle counts and receipts. Warehouse managers need warehouse membership.
- Reports use completed cash sales, without tax, refunds or cost-of-goods calculations. Date-filtered activity is historical; on-hand quantities are explicitly current. Product prices apply to future sales only.
- Location kind cannot be changed in the admin after creation. Retire locations/products instead of deleting those referenced by history. Retired products can still be counted and existing shipments received.
- No public signup or self-service password reset; head office provisions accounts and resets passwords through admin. Protect login endpoints with ingress rate limiting before public use.
- The build prepares code and deployment configuration for review. It does not provision cloud infrastructure or deploy a live production service.
