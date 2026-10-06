# Autoruns Velo

Nightly reports of what changed in the Windows autostart locations (Sysinternals Autoruns) on machines you are authorized to monitor.

[Velociraptor](https://docs.velociraptor.app/) collects the data. `autoruns_velociraptor.py` asks the Velociraptor server (through its API) to run the built-in `Windows.Sysinternals.Autoruns` artifact on each online Windows client, compares the results with the previous run, and writes two web pages:

- `delta.html`: what changed in this run, per host.
- `history.html`: every change recorded in the last 30 days, newest first.

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
                                                  reports/history.html
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

## Nightly schedule

`run_nightly.sh` wraps the script with absolute paths and logs to `nightly.log`. See `examples/crontab.example` for the cron line.

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
- The detection date is when the change was collected, not when it happened on the machine.
- Several hosts at once can be slow on a small server, since each collection runs the full Autoruns scan.

## Legacy script

`autoruns_delta.py` is an earlier, untested design that launches a single Velociraptor hunt (`launch` then `collect`) instead of per-host collections. It is kept for reference and is not documented here.

## License

Apache License 2.0. See [LICENSE](LICENSE).
