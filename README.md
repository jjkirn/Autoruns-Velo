# Velociraptor Autoruns Delta

Nightly reports of what changed in the Windows autostart locations (Sysinternals Autoruns) on machines you are authorized to monitor. Collection is done by [Velociraptor](https://docs.velociraptor.app/); this script launches the hunt through the Velociraptor API, compares the results against a per-host baseline, and writes two web pages:

- `delta.html`: each host's current differences from its baseline.
- `history.html`: a searchable log of day-to-day changes, kept for 30 days by default.

This is a rewrite of an earlier project, [Autoruns-Powershell](https://github.com/jjkirn/Autoruns-Powershell), which used PowerShell remoting, a MySQL database and Apache. This version uses Velociraptor for collection and Python with SQLite for storage, so there is no separate database server or web-page upload step.

> **Status: prototype.** The code has not been tested end to end. The Velociraptor function and column names in `autoruns_delta.py` were written from documentation and should be verified against your server version (see [Things to verify](#things-to-verify)).

> **Use only on systems you are authorized to monitor.**

## How it works

```
Velociraptor clients (Windows)  <--  hunt: Windows.Sysinternals.Autoruns
            |
   Velociraptor server  --gRPC API-->  autoruns_delta.py (cron)
                                            |
                              data/autoruns.db (SQLite)
                              data/delta.html, data/history.html
```

1. `launch` starts a hunt of the built-in `Windows.Sysinternals.Autoruns` artifact. The server downloads `autorunsc` and serves it to the clients, so nothing extra needs installing on the target machines. Hunts stay open for 24 hours, so machines that were off at launch report when they next check in.
2. `collect` pulls the hunt's results through the API and compares them with each host's stored baseline. A host's first result becomes its baseline.
3. Each entry is identified by its category, profile, name, image path and launch string. A change in signer or hash on the same entry is reported as CHANGED, which avoids false alarms from routine updates changing a file hash.
4. Hosts that did not report are listed separately instead of being treated as having removed all their entries.

The database holds three tables: `baseline` (what you accepted as normal), `last_run` (the previous result, used to log only new day-to-day changes), and `delta_log` (the rolling change history).

## Requirements

- A running Velociraptor server with Windows clients enrolled
- Python 3.9 or later on the machine that runs the script (the Velociraptor server itself works)
- `pip install -r requirements.txt`

## Setup

1. Create a Python environment and install dependencies:

   ```
   python3 -m venv venv && venv/bin/pip install -r requirements.txt
   ```

2. Create an API client config on the Velociraptor server:

   ```
   velociraptor --config /etc/velociraptor/server.config.yaml \
     config api_client --name autoruns --role api,investigator api_client.yaml
   chmod 600 api_client.yaml
   ```

   The `api,investigator` roles are my best understanding of the minimum needed to start hunts and read results. If you get a permission error, try `administrator` temporarily to confirm. The API listens on loopback by default; do not expose its port.

3. Optional: label the machines you want to monitor in the Velociraptor GUI (select them in the client list and use Add label), then set `AR_LABEL` to that label. Without a label, every Windows client the server knows about is included.

4. Run `launch`, then `collect` once hosts have reported:

   ```
   venv/bin/python autoruns_delta.py launch
   venv/bin/python autoruns_delta.py collect
   ```

## Usage

```
autoruns_delta.py launch                      # start tonight's hunt
autoruns_delta.py collect [--rebaseline]      # pull results, update reports
autoruns_delta.py history [--host H] [--days N] [--change added|removed|changed]
autoruns_delta.py history-html [--days N]     # rebuild history.html on demand
```

Run `collect --rebaseline` after you have reviewed a host's changes and want to accept them as the new normal. Re-baselining does not touch the history.

Schedule with cron; see `examples/crontab.example`.

## Settings

All optional, set as environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `AR_API_CONFIG` | `api_client.yaml` | Path to the Velociraptor API client config |
| `AR_DATA_DIR` | `data` | Where the database and HTML reports are written |
| `AR_KEEP_DAYS` | `30` | Days of change history to keep |
| `AR_LABEL` | (none) | Only hunt clients with this Velociraptor label |
| `AR_HUNT_HOURS` | `24` | How long a hunt stays open for late hosts |

## Things to verify

These parts were written from documentation and are the most likely to need adjustment:

- `hunt_flows(hunt_id=...)` should return `ClientId` and `FlowId` columns. Check with `SELECT * FROM hunt_flows(hunt_id='H.XXXX') LIMIT 1`.
- The `hunt()` function returns an object containing the hunt ID. The script accepts `hunt_id` or `HuntId`.
- The `include_labels` parameter name on `hunt()` (used when `AR_LABEL` is set).
- The column names in the Autoruns results: `Entry`, `Category`, `Profile`, `Image Path`, `Launch String`, `SHA-256`, `Signer`.
- If the results contain only system-wide entries and no per-user ones, check the `AutorunsArgs` parameter default of the artifact for a stray newline. This was a bug in an old Velociraptor version.
- If your server has multiple orgs, the API request needs an `org_id`.

## Security notes

- `api_client.yaml` contains a private key that can control your Velociraptor server. Keep it out of version control (it is in `.gitignore`) and readable only by the account that runs the script.
- The generated pages list every autostart entry that changed on your machines. Serve them only on a trusted network or behind authentication.

## Limitations

- Baselines and history only cover hosts that reported. A host that is off for several days appears in the "No data from" list.
- History for a host starts on its second run.
- `run_date` is the date the data was collected, not the date the change happened.

## License

Not yet chosen.
