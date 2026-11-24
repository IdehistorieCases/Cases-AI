# Cases-AI — inference and model services for CASES

This is the canonical AI-service repository for the CASES game. Some Compose projects, dashboards, and local data paths retain the legacy `Local-AI-API-LITE` name. The repository contains the optional local Qwen vLLM provider, the ChatGPT 6 Luna gateway and Codex CLI bridge, and CPU retrieval services for trusted local clients.

This repository owns the inference and retrieval services and their serving limits. It does not own application state, prompts, or game rules. Optional local Qwen is served through `http://ddu-llm:8080` as `ddu-narrator`; the CASES game uses ChatGPT 6 Luna through the signed-in Windows Codex CLI bridge as its main narrator.

## Coordinated CASES startup

For the playable CASES stack, use [Cases-Startup](https://github.com/IdehistorieCases/Cases-Startup). It pins Cases-AI, Cases-Backend, and Cases-GUI, prepares private local configuration, then starts the Luna bridge and gateway, backend, and GUI in order. The gateway starts with Luna disabled on a fresh install; an operator enables it through the dashboard only after reviewing the cloud-data disclosure.

## Optional Local Qwen service

Requirements: Docker Desktop with Linux containers, WSL2 GPU support, an NVIDIA driver visible to Docker, and enough free disk for the image and model cache. The pinned checkpoint is public; no Hugging Face token is required.

1. Copy `.env.example` to `.env` and set `HF_CACHE_DIR` to an absolute cache path outside this repository.
2. Create the shared network once if it does not already exist:

   ```powershell
   docker network inspect ddu_inference_bus *> $null
   if ($LASTEXITCODE -ne 0) { docker network create ddu_inference_bus }
   ```

3. Validate the resolved Compose configuration and start the service:

   ```powershell
   docker compose config --quiet
   docker compose up -d
   docker compose ps
   docker compose logs -f vllm
   ```

The first start downloads the pinned model into the persistent Hugging Face cache. The service listens on port 8080 inside Docker only. Compose publishes no host port; trusted clients attached to `ddu_inference_bus` use `http://ddu-llm:8080`.

## Verify

Run the independent contract client from a throwaway container on the shared network. From PowerShell in this directory:

```powershell
$scripts = (Resolve-Path .\scripts).Path
docker run --rm --network ddu_inference_bus --mount "type=bind,source=$scripts,target=/app,readonly" python:3.11-slim python /app/check_server.py --base-url http://ddu-llm:8080 --model ddu-narrator
docker run --rm --network ddu_inference_bus --mount "type=bind,source=$scripts,target=/app,readonly" python:3.11-slim python /app/shared_inference_smoke.py --base-url http://ddu-llm:8080 --model ddu-narrator --disable-thinking --concurrent
```

For a bounded concurrency profile, save sanitized JSONL outside the source tree:

```powershell
$results = (New-Item -ItemType Directory -Force .\benchmark-output).FullName
docker run --rm --network ddu_inference_bus --mount "type=bind,source=$scripts,target=/app,readonly" --mount "type=bind,source=$results,target=/results" python:3.11-slim python /app/benchmark.py --base-url http://ddu-llm:8080 --model ddu-narrator --disable-thinking --concurrency 1 2 4 --requests-per-concurrency 20 --output /results/rtx3060.jsonl
```

These clients check HTTP readiness, the served alias, strict synthetic history/examiner schemas, and completion finish reasons. `--disable-thinking` applies the verified Qwen3.5 chat-template option; without it, reasoning can consume the token budget and produce an empty response. The option is model-specific. Clients print sanitized results and never print prompts, model output, or tokens. Host acceptance and measured results are recorded in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

Set up the repository Python environment once, then run CPU-only tests and configuration checks without this service. The editable dashboard package installs its runtime dependencies, including `argon2-cffi`, into the repository virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check scripts tests dashboard/backend
docker compose config --quiet
```

## CASES narrator configuration

The connected CASES game uses ChatGPT 6 Luna (`gpt-6-luna`) through the Windows Codex CLI bridge as its default narrator. A session is pinned to its selected provider; Luna never falls back to local Qwen. The dashboard keeps Luna disabled until an operator acknowledges the cloud-data disclosure and enables it. Local Qwen is an optional provider, not a required Luna dependency. See [Cases-Startup](https://github.com/IdehistorieCases/Cases-Startup) for pinned, coordinated startup of the bridge, gateway, backend, and GUI.

## Service boundaries

- One vLLM container and one model instance serve multiple trusted clients.
- The external `ddu_inference_bus` network and `ddu-llm:8080` alias preserve the history client interface.
- No inference port is published on the host. Do not attach untrusted containers or expose the Docker host.
- The medical-examiner workload is entirely fictional and tests JSON transport only; it makes no diagnostic claims.
- The llama.cpp measurements in the companion project's M1.5 record are historical baseline data, not vLLM results.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), [docs/API_CONTRACT.md](docs/API_CONTRACT.md), and [docs/CUTOVER_AND_ROLLBACK.md](docs/CUTOVER_AND_ROLLBACK.md) before connecting an application.

The local dashboard is described in [dashboard/README.md](dashboard/README.md) and its [operator runbook](docs/DASHBOARD.md). It runs as a separate loopback-only CPU controller with authenticated status and lifecycle controls, bounded jobs, sanitized logs, and optional read-only PostgreSQL probes. Inference operations require a locally configured stable Compose project path and explicit administrator confirmation.

The existing CPU retrieval workers and their independent dashboard status and controls are covered by the [M3 operational reliability runbook](docs/M3_OPERATIONAL_RELIABILITY.md), including the bounded three-stage synthetic benchmark and the host acceptance checks that remain pending.

The optional, independently deployed Danish CPU speech-to-text and text-to-speech services are documented in [docs/SPEECH_SERVING.md](docs/SPEECH_SERVING.md). Their pinned profile appears separately from the narrator profiles in the dashboard.
