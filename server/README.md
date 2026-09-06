# Global lookup table server

One Python file, standard library only, one SQLite database. Clients download the whole table
(`GET /v1/table`) on start-up and when the Refresh button in the app footer is pressed, then look
names up locally. Only the holder of the admin token can change the table (`PUT /v1/records`).

## Install on the VPS (Debian/Ubuntu, any Python 3.9+)

```bash
sudo useradd --system --home /opt/tradecheck --shell /usr/sbin/nologin tradecheck
sudo mkdir -p /opt/tradecheck
sudo cp tradecheck_server.py /opt/tradecheck/
sudo cp env.example /opt/tradecheck/env          # then edit: set TRADECHECK_ADMIN_TOKEN
sudo chmod 600 /opt/tradecheck/env
sudo chown -R tradecheck:tradecheck /opt/tradecheck
sudo cp tradecheck-table.service /etc/systemd/system/
sudo systemctl enable --now tradecheck-table
curl -s localhost:8787/health
```

Put Caddy (or nginx) in front for HTTPS; `Caddyfile` in this folder is a complete config once
the host name is replaced. The service only listens on localhost, so the proxy is the only way in.

## Publish your records

Export from the app (Records tab -> Export -> `.json`), then from the project folder:

```bash
python tools/publish_global.py https://table.example.com records.json
```

The token is read from the `TRADECHECK_ADMIN_TOKEN` environment variable or the `--token`
option. The upload **replaces** the table: names missing from the file are removed, rows with an
unchanged state keep their old timestamp. The response reports added / updated / removed counts.

## Behaviour

- `GET /v1/table` returns `{"version", "updated_at", "count", "records": [{name, state, notes,
  updated_at}]}`. The version is an integer bumped on every change and doubles as the ETag, so a
  client that already has the current table gets a 304 with no body.
- Rate limit: `TRADECHECK_RATE` requests per IP per rolling minute (default 10), answered with
  429 and a `Retry-After` header. With `TRADECHECK_TRUST_PROXY=1` the IP is taken from
  `X-Forwarded-For`, which is correct behind Caddy/nginx and wrong when exposed directly.
- Without `TRADECHECK_ADMIN_TOKEN` the table can be read but never written.

## Point the client at it

Set `GLOBAL_TABLE_URL` in `app/__init__.py` to the base URL (e.g. `https://table.example.com`)
before building, or for a one-off test set the `TRADECHECK_GLOBAL_URL` environment variable.
