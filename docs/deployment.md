# Deployment

This guide walks through a fresh install on a single Ubuntu 22.04+ VPS. Multi-machine routing (adding a home-lab box over Tailscale) is covered at the end.

## Prerequisites

- Ubuntu 22.04 or newer, 8 GB RAM minimum (14 GB+ for Qwen 2.5 14B).
- `python3.12`, `pip`, `venv`.
- [Ollama](https://ollama.com) installed.
- An Obsidian vault folder the runner can read/write.

## 1. Install Ollama and pull models

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:14b
ollama pull gemma3:4b      # optional, for vision/fast steps
```

Confirm: `curl http://localhost:11434/api/tags`.

## 2. Clone and install the runner

```bash
git clone https://github.com/<you>/vault-runner.git /opt/vault-runner
cd /opt/vault-runner/runner
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Edit at minimum:
- `vault_path` and the five `dirs:` entries → your vault location.
- `ollama.default_model` → a model you pulled.
- Create the directories referenced: `mkdir -p <vault>/{_queue,_active,_completed,_failed,runner-outputs}`.

## 4. Smoke test

```bash
# terminal 1 — worker
python runbook.py

# terminal 2 — drop a job
cat > /path/to/vault/_queue/hello.md <<'EOF'
---
type: text
model: qwen2.5:14b
---
Say hello in three words.
EOF
```

The worker should pick it up within 5 seconds and write the output to `runner-outputs/`.

## 5. Start the web UI

```bash
uvicorn web:app --host 0.0.0.0 --port 8000
```

Visit `http://<host>:8000`.

## 6. Run as systemd services

Copy the unit files:

```bash
sudo cp runner.service web.service /etc/systemd/system/
# edit paths inside each .service to match your install
sudo systemctl daemon-reload
sudo systemctl enable --now runner web
```

## Optional integrations

### Syncthing (vault sync between machines)

Install on every machine, share the vault folder, point `vault_path` at the synced location. No runner changes needed.

### Tailscale (private mesh)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

On a second machine with Ollama, add it under `runners:` in `config.yaml`:

```yaml
runners:
  secondary:
    base_url: http://<tailscale-ip>:11434
    default_model: gemma3:4b
model_runners:
  gemma3:4b: secondary
```

### Cloudflare Tunnel (public UI without port forwarding)

```bash
cloudflared tunnel login
cloudflared tunnel create vault-runner
cloudflared tunnel route dns vault-runner runner.example.com
cloudflared tunnel run --url http://localhost:8000 vault-runner
```

### OpenTelemetry → Tempo + Grafana

Run the OTel Collector, Tempo, and Grafana via Docker Compose (a stack is provided in most Grafana quickstarts). Set `otel.endpoint` in `config.yaml` to the Collector's OTLP gRPC port (default `4317`). Every job becomes a trace.

### Discord alerts

Create a webhook in your Discord server, paste the URL into `discord.webhook_url`. Failure alerts include the traceback and a link to the trace.

### SearXNG

```bash
docker run -d --name searxng -p 8080:8080 searxng/searxng
```

Set `searxng.base_url: http://localhost:8080` and `enabled: true`. Chain steps can now use `action: search`.

### MemPalace

See [../mempalace/README.md](../mempalace/README.md). Point `mempalace.store_path` at the store directory and set `enabled: true`. Jobs with `use_memory: true` will have the top-N relevant past outputs injected into the prompt.

## Verifying end-to-end

1. Submit a job via the UI with the "Quick Model Test" template.
2. Watch the SSE stream tail tokens live.
3. Confirm the output appears in `runner-outputs/`.
4. Check the Grafana trace view — you should see spans for the two chain steps.
5. Run `pytest runner/tests/` — all 76 tests should pass.
