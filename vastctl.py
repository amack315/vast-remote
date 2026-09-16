#!/usr/bin/env python3
"""Create / inspect / destroy Vast.ai instances over the plain HTTPS API.

Designed for environments where only HTTPS (port 443) egress is available.
Instances are launched from Vast's official base image so that the Instance
Portal opens Cloudflare quick tunnels for Jupyter; remote.py then talks to
Jupyter's terminal API through that tunnel.

Usage:
  vastctl.py offers [--max-dph 0.5] [--gpu "RTX 4090"] [--min-gpu-ram 20] [--n 10]
  vastctl.py up --label amack-claude-<purpose> [--offer ID | --gpu ... --max-dph ...] [--disk 32]
  vastctl.py wait <id>            # block until running + tunnels discovered; prints JSON conn info
  vastctl.py info <id>            # conn info JSON (tunnels, token) once available
  vastctl.py list                 # all instances on the account (label, status)
  vastctl.py logs <id> [--tail N]
  vastctl.py down <id>
Env: VAST_API_KEY (required), VAST_LABEL_PREFIX (default "amack-claude-").
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request

API = "https://console.vast.ai/api/v0"
TEMPLATE = "b84ca276fa572e949cd7ff43ae5fe855"  # "PyTorch (Vast)" official template, vastai/pytorch
RUNTYPE = "jupyter_direc ssh_direc ssh_proxy"   # MUST be jupyter runtype: ssh runtype skips Jupyter
KEY = os.environ.get("VAST_API_KEY")
PREFIX = os.environ.get("VAST_LABEL_PREFIX", "amack-claude-")
STATE_DIR = os.path.expanduser("~/.vast-remote")


def api(method, path, body=None, timeout=60):
    if not KEY:
        sys.exit("VAST_API_KEY not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def offers(args):
    q = {"verified": {"eq": True}, "rentable": {"eq": True}, "rented": {"eq": False},
         "num_gpus": {"eq": args.num_gpus}, "reliability2": {"gte": 0.98},
         "inet_down": {"gte": 300}, "inet_up": {"gte": 100},
         "direct_port_count": {"gte": 10}, "cpu_arch": {"eq": "amd64"},
         "cuda_max_good": {"gte": 12.4}, "dph_total": {"lte": args.max_dph},
         "order": [["dph_total", "asc"]], "type": "on-demand", "limit": args.n}
    if args.gpu:
        q["gpu_name"] = {"eq": args.gpu}
    if args.min_gpu_ram:
        q["gpu_ram"] = {"gte": args.min_gpu_ram * 1000}
    if args.min_disk:
        q["disk_space"] = {"gte": args.min_disk}
    if args.geo:
        q["geolocation"] = {"in": args.geo.split(",")}
    return api("POST", "/bundles/", q)["offers"]


def cmd_offers(args):
    for o in offers(args):
        print(f"{o['id']:>10}  {o['gpu_name']:<18} {o['num_gpus']}x {o['gpu_ram']/1000:5.1f}GB  ${o['dph_total']:.3f}/h  "
              f"dl {o['inet_down']:.0f}Mb/s  rel {o['reliability2']:.3f}  disk {o['disk_space']:.0f}GB  {o['geolocation']}  host {o['host_id']}")


def cmd_up(args):
    label = args.label if args.label.startswith(PREFIX) else PREFIX + args.label
    offer = args.offer
    if not offer:
        os_ = offers(args)
        if not os_:
            sys.exit("no offers match")
        offer = os_[0]["id"]
        print(f"picked offer {offer}: {os_[0]['gpu_name']} ${os_[0]['dph_total']:.3f}/h {os_[0]['geolocation']}", file=sys.stderr)
    body = {"client_id": "me", "template_hash_id": TEMPLATE, "disk": args.disk, "label": label, "runtype": RUNTYPE}
    if args.env:
        body["env"] = dict(kv.split("=", 1) for kv in args.env)
    r = api("PUT", f"/asks/{offer}/", body)
    if not r.get("success"):
        sys.exit(f"create failed: {r}")
    iid = r["new_contract"]
    print(iid)
    if args.wait:
        args.id = iid
        cmd_wait(args)


def instance(iid):
    return api("GET", f"/instances/{iid}/")["instances"]


def fetch_logs(iid, tail=3000, settle=25):
    r = api("PUT", f"/instances/request_logs/{iid}/", {"tail": tail})
    url = r.get("result_url")
    if not url:
        return ""
    time.sleep(settle)
    for _ in range(6):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                txt = resp.read().decode(errors="replace")
            if "<Code>AccessDenied</Code>" not in txt:
                return txt
        except Exception:
            pass
        time.sleep(10)
    return ""


TUNNEL_RE = re.compile(r"^Default Tunnel started for (.+?) \(https?://localhost:\d+\) - (https://\S+)", re.M)


def conn_info(iid, log_txt):
    inst = instance(iid)
    tunnels = {name: url for name, url in TUNNEL_RE.findall(log_txt)}
    info = {"id": iid, "label": inst.get("label"), "status": inst.get("actual_status"),
            "gpu": inst.get("gpu_name"), "dph": inst.get("dph_total"),
            "token": inst.get("jupyter_token"), "tunnels": tunnels,
            "jupyter": tunnels.get("Jupyter"), "portal": tunnels.get("Instance Portal")}
    return info


def save_info(info):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{info['id']}.json"), "w") as f:
        json.dump(info, f, indent=1)
    with open(os.path.join(STATE_DIR, "current"), "w") as f:
        f.write(str(info["id"]))


def cmd_wait(args):
    iid = args.id
    t0 = time.time()
    while True:
        st = instance(iid).get("actual_status")
        print(f"  [{time.time()-t0:5.0f}s] {st}", file=sys.stderr)
        if st == "running":
            break
        if st in ("exited", "error") or time.time() - t0 > args.timeout:
            sys.exit(f"instance {iid} not running: {st}")
        time.sleep(10)
    # tunnels appear ~30-90s after running
    for _ in range(12):
        info = conn_info(iid, fetch_logs(iid))
        if info["jupyter"]:
            save_info(info)
            print(json.dumps(info, indent=1))
            return
        print("  waiting for Jupyter tunnel...", file=sys.stderr)
        time.sleep(15)
    sys.exit("Jupyter tunnel never appeared in logs; check `vastctl.py logs`")


def cmd_info(args):
    info = conn_info(args.id, fetch_logs(args.id, settle=20))
    if info["jupyter"]:
        save_info(info)
    print(json.dumps(info, indent=1))


def cmd_list(args):
    for i in api("GET", "/instances/")["instances"]:
        print(f"{i['id']:>10}  {str(i.get('label')):<32} {i.get('actual_status'):<9} {i.get('gpu_name')}  ${i.get('dph_total', 0):.3f}/h")


def cmd_logs(args):
    print(fetch_logs(args.id, args.tail))


def cmd_down(args):
    inst = instance(args.id)
    if not (inst.get("label") or "").startswith(PREFIX) and not args.force:
        sys.exit(f"refusing: instance {args.id} label {inst.get('label')!r} lacks prefix {PREFIX!r} (use --force)")
    print(api("DELETE", f"/instances/{args.id}/"))
    p = os.path.join(STATE_DIR, f"{args.id}.json")
    if os.path.exists(p):
        os.remove(p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def offer_args(p):
        p.add_argument("--max-dph", type=float, default=0.6)
        p.add_argument("--gpu")
        p.add_argument("--num-gpus", type=int, default=1)
        p.add_argument("--min-gpu-ram", type=float, help="GB")
        p.add_argument("--min-disk", type=float, help="GB")
        p.add_argument("--geo", help="comma list, e.g. US,CA")
        p.add_argument("--n", type=int, default=10)

    p = sub.add_parser("offers"); offer_args(p); p.set_defaults(f=cmd_offers)
    p = sub.add_parser("up"); offer_args(p)
    p.add_argument("--label", required=True); p.add_argument("--offer", type=int)
    p.add_argument("--disk", type=int, default=32); p.add_argument("--env", action="append", help="KEY=VALUE")
    p.add_argument("--wait", action="store_true"); p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(f=cmd_up)
    p = sub.add_parser("wait"); p.add_argument("id", type=int); p.add_argument("--timeout", type=int, default=900); p.set_defaults(f=cmd_wait)
    p = sub.add_parser("info"); p.add_argument("id", type=int); p.set_defaults(f=cmd_info)
    p = sub.add_parser("list"); p.set_defaults(f=cmd_list)
    p = sub.add_parser("logs"); p.add_argument("id", type=int); p.add_argument("--tail", type=int, default=500); p.set_defaults(f=cmd_logs)
    p = sub.add_parser("down"); p.add_argument("id", type=int); p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_down)
    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
