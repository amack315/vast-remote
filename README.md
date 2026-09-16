# vast-remote

Drive [Vast.ai](https://vast.ai) GPU instances from an environment that only has
outbound HTTPS (port 443). No SSH required. Built for running Claude Code in its
cloud environment, but nothing here is Claude-specific.

Two scripts, stdlib only except `websocket-client`:

| script | does |
|---|---|
| `vastctl.py` | search offers, create / wait / inspect / destroy instances via the Vast REST API |
| `remote.py`  | run shell commands in a **persistent** bash session on the instance, and move files, through Jupyter's terminal and contents APIs over a Cloudflare tunnel |

## Quick start

```bash
pip install -r requirements.txt
export VAST_API_KEY=...            # from https://cloud.vast.ai/manage-keys/
export VAST_LABEL_PREFIX=<you>-claude-   # default: "$USER-claude-". Shared account: every instance gets labeled.

./vastctl.py offers --gpu "RTX 4090" --max-dph 0.6          # browse
./vastctl.py up --label scan-run --gpu "RTX 4090" --disk 64 --wait   # ~3-5 min, prints conn info JSON
./remote.py run 'nvidia-smi'
./remote.py run 'cd /workspace && git clone https://github.com/you/proj && cd proj && pip install -r requirements.txt'
./remote.py run 'nohup python train.py > train.log 2>&1 &'  # long jobs: background them
./remote.py run 'tail -n 20 train.log'
./remote.py put local.py workspace/proj/local.py
./remote.py get workspace/proj/results.json results.json
./vastctl.py down <id>     # only when you're done with the box for good; it keeps state otherwise
```

Boxes are cheap to keep and slow to rebuild (~5 min plus your setup), so the default is
to leave one running across sessions and reconnect with `vastctl.py info <id>`.

`vastctl.py wait` writes `~/.vast-remote/<id>.json` and `~/.vast-remote/current`;
`remote.py` reads those, so `--id` is only needed with several instances up.
Alternatively set `VAST_JUPYTER_URL` and `VAST_TOKEN`.

## How it works

- Instances are created from Vast's official **"PyTorch (Vast)"** template
  (`vastai/pytorch`, template hash `b84ca276fa572e949cd7ff43ae5fe855`) in its
  default **Jupyter runtype**. Its Instance Portal starts Cloudflare quick tunnels
  (`https://*.trycloudflare.com`) for Jupyter and the portal: HTTPS on 443 with
  valid certs, so they pass any HTTPS-only egress proxy.
- Tunnel URLs are discovered from the instance logs
  (`PUT /api/v0/instances/request_logs/<id>/`, lines `Default Tunnel started for ...`).
- Auth: the instance's `jupyter_token`. The portal edge wants
  `Authorization: Bearer <tok>`, Jupyter wants `?token=<tok>`; the scripts send both.
- `remote.py run` attaches to a named Jupyter terminal (`--term`, default `claude`),
  which is a long-lived bash process. It puts the shell in quiet mode
  (`stty -echo`, empty prompt), sends the command base64-wrapped in a single `eval`
  line bracketed by start/end markers, streams stdout+stderr back, and returns the
  real exit code. `cd`, `export`, `source venv/bin/activate` persist between calls.
- If the websocket drops mid-command, `remote.py` reconnects and queues a fresh
  end-marker; bash executes it after the running command finishes, so the exit code
  is still correct. Output produced during the gap is lost.

## Gotchas learned the hard way

- **Don't use the SSH runtype** with the Vast base image: it skips starting Jupyter
  ("not in /etc/portal.yaml") and the portal can't start it afterwards.
- **Vast's own Jupyter proxy** (`https://jupyter.vast.ai/jm/<ssh_idx>/<ssh_port>/…`)
  returned 504 on several hosts even with Jupyter running. Not used here.
- **Don't `exit`** in `remote.py run`; it kills the shell. A new terminal is created
  on the next call, but the exit status of that command is lost. Use `(exit N)` if
  you need to test exit codes.
- **Long jobs**: run under `nohup`/`tmux` on the instance and poll a log file.
  A single `run` call holds one websocket open; that's fine for minutes, less so
  for hours.
- Interactive TTY programs (`vim`, `top`) won't work through `run`. Plain
  commands, `tail -f` with `--timeout`, and streaming output are fine.
- Tunnels come up 30–90 s after the instance reports `running`; `wait` handles that.
- `vastctl.py down` refuses to destroy instances whose label lacks *your* prefix
  (shared account safety: you only tear down your own boxes). `--force` overrides.
- In containers `$USER` is often `root`; set `VAST_USER=<you>` or `VAST_LABEL_PREFIX` explicitly.
- Offer filters default to verified hosts, ≥10 direct ports, amd64, CUDA ≥ 12.4,
  ≥300 Mb/s down. Loosen with flags if you get no matches.
