# vllm-bridge — reach a loopback-only model server from NemoClaw sandboxes

**Symptom:** your NemoClaw sandbox reports inference as
`unreachable (http://127.0.0.1:8000/v1/models)` or the gateway returns HTTP 502,
even though your vLLM server answers on the host.

**Cause:** NemoClaw routes sandbox inference to `host.openshell.internal`, which
resolves to the host's docker-bridge address (find it on your host —
typically `172.18.0.1` on the `openshell-docker` network). A model server bound
to `127.0.0.1` only listens on loopback and is *not* reachable from that path.
OpenShell's guidance: *"A server listening only on 127.0.0.1 may also be
unreachable from a container; bind it to an address reachable from the gateway
runtime."*

**Fix:** [`vllm_bridge.py`](vllm_bridge.py) — a stdlib-only TCP relay that
listens **only on the docker-bridge interface** and forwards to your loopback
model server. Nothing is exposed on the LAN or the public internet.

## Usage (on the NemoClaw host)

```bash
# find the bridge gateway for the openshell network
docker network inspect openshell-docker \
  --format '{{(index .IPAM.Config 0).Gateway}}'    # e.g. 172.18.0.1

# install the relay and start it (user-level, no sudo)
mkdir -p ~/bin
cp vllm_bridge.py ~/bin/
nohup python3 ~/bin/vllm_bridge.py --listen 172.18.0.1:18300 --target 127.0.0.1:18300 \
  >> ~/vllm-bridge.log 2>&1 &

# verify the bind is bridge-only (NOT 0.0.0.0, NOT your LAN IP)
ss -tlnp | grep 18300
```

Make it survive reboots with a user crontab line (no sudo):

```cron
@reboot sleep 30 && nohup python3 ~/bin/vllm_bridge.py --listen 172.18.0.1:18300 --target 127.0.0.1:18300 >> ~/vllm-bridge.log 2>&1 &
```

## Verify from inside the sandbox

```bash
nemohermes <sandbox> exec -- curl -sS -o /dev/null -w '%{http_code}\n' https://inference.local/v1/models
# → 200, and the body lists your model
```

If the bridge address changes (the docker network was recreated), re-run the
`docker network inspect` step and restart the bridge with the new `--listen`.