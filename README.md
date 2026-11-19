# AeroGuard Inventory Planning

A Flask and SQLite web application based on the existing inventory project.

## Run locally

```bash
python -m pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`.

Default users are created when no users exist:

- `admin` / `admin123`
- `planner` / `planner123`

The database is stored in `inventory.db`. Change the Flask secret key before deploying.

## Main features

- Dashboard risk and pull-in overview
- Individual part records and planning fields
- Forecast snapshots by need period
- CSV forecast import and part-report export
- Purchase order confirmation and supply tracking
- Pull-in, push-out, expedite, reallocation, and escalation actions
- Period timeline and demand scenarios
- Configurable planning weights and zones
- Audit history and role-aware API operations

For production deployment, place it behind HTTPS, rotate secrets, move credentials out of source control, and add database-backed sessions and stronger migration tooling.
