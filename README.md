# Autoruns Velo

Nightly reports of what changed in the Windows autostart locations (Sysinternals Autoruns) on machines you are authorized to monitor.

[Velociraptor](https://docs.velociraptor.app/) collects the data. `autoruns_velociraptor.py` asks the Velociraptor server (through its API) to run the built-in `Windows.Sysinternals.Autoruns` artifact on each online Windows client, compares the results with the previous run, and writes two web pages:

- `delta.html`: what changed in this run, per host.
- `history.html`: every change recorded in the last 30 days, newest first.

`chart_report.py` then builds a third page from the same database:

- `chart.html`: a stacked-bar time series for each host. Click a segment to list that day's changes below the charts.

This is the Velociraptor-based successor to [Autoruns-Powershell](https://github.com/jjkirn/Autoruns-Powershell), which used PowerShell remoting, MySQL and Apache. Storage is a single SQLite file, and there is no database server or web upload step.

> **Use only on systems you are authorized to monitor.**

**Status:** tested end to end against a Velociraptor server with Windows 10 and Windows 11 clients (agent 0.75.6). Baseline creation and the "no changes" run have been verified. Different server versions may need small adjustments (see [Troubleshooting](#troubleshooting)).

## How it works

```
Velociraptor clients (Windows)
        ^   Windows.Sysinternals.Autoruns
        |
Velociraptor server  <--gRPC API-->  autoruns_velociraptor.py
                                          |
                                autoruns.sqlite   reports/delta.html
                                      |           reports/history.html
                              chart_report.py     reports/chart.html
```

1. The script lists the server's Windows clients. Clients not seen in the last 10 minutes are skipped and noted in `delta.html`.
2. It starts the Autoruns collection on every online client at the same time and waits for them to finish (default timeout: 15 minutes).
3. Each result is compared with that host's stored snapshot from its previous successful run. A host's first run only creates its baseline.
4. Differences are stored in a change log, entries older than 30 days are pruned, and both reports are rewritten.

### What counts as a change

Each autorun entry is identified by its **Entry Location + Entry + Image Path + Launch String**. Then:

| Change | Meaning |
|---|---|
| ADDED | An entry with a new identity appeared |
| REMOVED | An entry with that identity is no longer present |
| MODIFIED | Same identity, but one of these fields differs: SHA-256, MD5, Signer, Company, Version, Enabled, Category |

The `Time` column is ignored, since it changes whenever an entry is edited. Rows with no identity fields at all are skipped, and exact duplicates are merged, so the entry count is a few lower than the raw row count.

The "baseline" is the host's previous successful run, not a manually approved state. After every successful run it is replaced with the latest snapshot, so a change is reported once, on the run that first sees it, and stays in `history.html` for 30 days. A run that returns 0 rows leaves the baseline unchanged.

## Requirements

- A Velociraptor server with Windows clients enrolled
- Python 3.9 or later on the machine that runs the script (the Velociraptor server itself works)
- `pyvelociraptor` and `pyyaml` (`requirements.txt`)

## Setup

### 1. Python environment

On Ubuntu/Debian you need the venv package first:

```
sudo apt install -y python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. API client config

On the Velociraptor server, create an API client certificate. Run the command as the `velociraptor` service user, write to `/tmp`, then move the file into place:

```
sudo -u velociraptor velociraptor --config /etc/velociraptor/server.config.yaml \
  config api_client --name autoruns_api --role api,investigator /tmp/api.config.yaml
sudo mv /tmp/api.config.yaml ~/autoruns-velo/api.config.yaml
sudo chown $USER:$USER ~/autoruns-velo/api.config.yaml
chmod 600 ~/autoruns-velo/api.config.yaml
```

Adjust the paths to your install. The `api_connection_string` in the file is the address the script connects to (usually `127.0.0.1:8001`). Do not expose that port.

### 3. Grant the API user its role and restart

Creating the certificate does not create the user on the server. Without this step the script fails with `User not found: autoruns_api`.

```
sudo -u velociraptor velociraptor --config /etc/velociraptor/server.config.yaml \
  acl grant autoruns_api --role api,investigator
sudo systemctl restart velociraptor_server
```

The service name may differ on your install (`systemctl list-units | grep -i velo`). Clients reconnect on their own within a minute or so.

### 4. First run

Test on one host first:

```
python3 autoruns_velociraptor.py --api-config api.config.yaml --hosts HOSTNAME --dump-columns
```

This prints the result column names, creates the baseline, and writes the reports. Run it a second time and it should report `0 changes`.

## Usage

```
python3 autoruns_velociraptor.py --api-config api.config.yaml [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--api-config` | (required) | Path to the Velociraptor `api.config.yaml` |
| `--db` | `autoruns.sqlite` | SQLite database (baselines and change log) |
| `--out-dir` | `reports` | Where `delta.html` and `history.html` are written |
| `--hosts` | all Windows clients | Comma-separated host names to include |
| `--timeout` | `900` | Seconds to wait for collections to finish |
| `--online-window` | `600` | A client counts as online if seen within this many seconds |
| `--dump-columns` | off | Print the first result row's column names |

## Chart page

```
python3 chart_report.py --db autoruns.sqlite --out reports/chart.html
```

It only reads the SQLite database and uses the Python standard library, so it needs no extra packages and no Velociraptor connection. The page is a single file with the data embedded.

- One stacked-bar chart per host, one bar per day (last 7, 14 or 30 days).
- Stack by change type (Added, Removed, Modified) or by Autoruns category.
- Click a segment to list its changes below the charts, with the same columns as `history.html` plus Category. Click another segment and the list is replaced. Click the empty part of a day column for every change that day.
- A table view of the same counts is available for screen readers and printing.
- Each category has a fixed colour taken from `CATEGORY_ORDER` at the top of `chart_report.py`; other categories are grouped as "Other".
- Days are counted by when the change was collected (see Limitations), shown in the viewer's time zone.

The nightly script rebuilds it after each run. Serve it with the other reports.

**Run markers.** The script records every attempt per host (ok, baseline, offline, error, timeout, no_data) in a `runs` table. The chart uses it to show a small dash on days a host ran with no changes, and an "x" on days it did not run. History starts from the first run after this table was added; days before that have no marker.

## Unsigned page

`unsigned_report.py` builds `reports/unsigned.html`, a page that lists entries
with no trustworthy code signature so they are not lost on a day with hundreds
of changes. Like the chart page it only reads the SQLite database.

```bash
python3 unsigned_report.py --db autoruns.sqlite --out reports/unsigned.html
```

Signer classes:

| Class | Meaning |
| --- | --- |
| No signature | Signer is empty and the entry points at a file |
| Invalid signature | Signer starts with `(Not verified)`: a signature is present but does not validate |
| No file | Signer is empty and there is no Image Path (nothing to sign); hidden by default |

Two views: **Changes** (added, modified or removed entries, with a per-day count
picker and a "Signer changed" badge when a signed entry became unsigned) and
**Current inventory** (every unsigned entry now, with first-seen date and how
many hosts carry the same file hash). SHA-256 values link to VirusTotal; the
link is only followed if you click it.

To hide known-harmless items, copy `unsigned_ignore.txt.example` to
`unsigned_ignore.txt` next to `autoruns.sqlite` and list patterns (one per line,
`*` and `?` wildcards, matched against Image Path or Launch String). Ignored
entries stay available behind the "Show ignored" switch. `run_nightly.sh`
rebuilds the page after every run.

## Nightly schedule

`run_nightly.sh` wraps the script (and the chart rebuild) with absolute paths and logs to `nightly.log`. See `examples/crontab.example` for the cron line.

Note that a machine that is switched off at run time is skipped that night. Pick a time your machines are usually on.

## Viewing the reports

The reports list every changed autostart entry on your machines. Serve them only on a trusted network or behind authentication. For a quick look from another machine:

```
cd reports && python3 -m http.server 8080
```

## Security notes

- `api.config.yaml` contains a private key that can control your Velociraptor server. It is excluded by `.gitignore`. Keep it readable only by the account that runs the script (`chmod 600`).
- `autoruns.sqlite` and `reports/` contain your hosts' startup entries and are also excluded from git.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `User not found: autoruns_api` | Step 3 is missing: grant the role and restart the server |
| `externally-managed-environment` from pip | Use the virtual environment from step 1 |
| `Velociraptor should be running as the 'velociraptor' user` | Run the `config api_client` command with `sudo -u velociraptor` |
| Host listed as offline in `delta.html` | The client was not seen within `--online-window`; run when it is on |
| `collection returned 0 rows` | The baseline is left unchanged; check the flow's Logs tab in the GUI |
| Different column names | Run with `--dump-columns` and edit `KEY_FIELDS` and `WATCH_FIELDS` at the top of the script |

## Limitations

- Hosts that are offline are skipped, not queued. They are compared against their previous run the next time they are online.
- Only the most recent run is shown in `delta.html`. Earlier changes are in `history.html`.
- History for a host starts on its second run.
- On the chart, bar segments under 4 px are drawn at 4 px so they stay clickable.
- The detection date is when the change was collected, not when it happened on the machine.
- Several hosts at once can be slow on a small server, since each collection runs the full Autoruns scan.

## Legacy script

`autoruns_delta.py` is an earlier, untested design that launches a single Velociraptor hunt (`launch` then `collect`) instead of per-host collections. It is kept for reference and is not documented here.

## License

Apache License 2.0. See [LICENSE](LICENSE).
