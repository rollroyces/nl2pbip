# nl2pbip

Natural Language to Power BI Project (.pbip) Engine & Fine-Tuning Suite

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg) ![License: Apache-2.0](https://img.shields.io/badge/license-Apache_2.0-blue.svg) ![CI](https://github.com/rollroyces/nl2pbip/actions/workflows/ci.yml/badge.svg)

## Overview

`nl2pbip` converts free-form analytics requirements into Git-friendly Power BI Project (`.pbip`) directories. It orchestrates a large language model (LLM) planner, DAX/TMDL tooling, and PBIR layout builders so that every natural-language prompt becomes:

- A semantic model expressed in Tabular Model Definition Language (TMDL)
- A report surface captured as PBIR JSON (pages, visuals, bindings)
- A packaged `.pbip` workspace that can be exported to `.pbix` or `.pbit`

### Architecture at a glance

- **Planner:** `StructuredLLMClient` normalizes responses from OpenAI, Azure OpenAI, Anthropic, DeepSeek, Qwen, Zhipu, Moonshot, or any OpenAI-compatible endpoint.
- **Agent runtime:** `Orchestrator` validates the plan, calls domain tools (table creation, DAX measures, relationships, roles, PBIR layout), and retries after lint feedback from `tmdl_linter` / `pbir_validator`.
- **Packager:** `package_pbip_handler` persistently writes semantic/report components plus `.pbip` manifests.
- **Exporter:** `PBIPExporter` integrates with [`pbi-tools`](https://github.com/pbi-tools/pbi-tools) and can fall back to zipped `.pbit` archives.
- **Fine-tune suite:** `nl2pbip.finetune` covers synthetic dataset generation, QLoRA training with Unsloth, and GGUF export for local inference backends such as Ollama or vLLM.

## Installation

```bash
pip install nl2pbip
```

Optional extras provide heavyweight dependencies only when you need them:

- Fine-tuning (datasets + Unsloth + transformers):

  ```bash
  pip install "nl2pbip[finetune]"
  ```

- PBIX/PBIT exports via `pbi-tools` integration:

  ```bash
  pip install "nl2pbip[export]"
  ```

> **Tip:** Install `pbi-tools` separately (`dotnet tool install --global TabularEditor.Tools.PBITools`) so `nl2pbip export` can emit `.pbix`. Without it, the exporter automatically creates a zipped `.pbit` template.

## Quickstart & Core Usage

### CLI: cloud-hosted LLMs

```bash
nl2pbip generate \
  --prompt "Executive sales dashboard with YoY, RLS per region, and KPI cards" \
  --provider openai \
  --model gpt-4o-mini \
  --project-name SalesInsights \
  --workspace artifacts/workspace \
  --export pbix
```

Key flags:

- `--provider` and `--model` override `NL2PBIP_LLM_PROVIDER` / `NL2PBIP_LLM_MODEL` env defaults.
- `--base-url`, `--api-key`, and `--api-version` allow Azure OpenAI, Anthropic, or other OpenAI-compatible hosts.
- `--dax-library` points to an organization-specific DAX pattern catalog (defaults to `dax_library.json`).
- `--export [pbix|pbit]` triggers compilation immediately after the PBIP folder is produced.

### CLI: local inference endpoints (Ollama, vLLM, LM Studio)

```bash
nl2pbip generate \
  --prompt "Marketing attribution model with creative/channel filters" \
  --provider custom \
  --base-url http://localhost:11434/v1 \
  --api-key sk-local-demo \
  --model mistral-openorca \
  --workspace artifacts/workspace-local \
  --output artifacts/pbip-local
```

`StructuredLLMClient` simply needs an OpenAI-compatible HTTP surface; the `custom` provider lets you point at self-hosted gateways with dummy API keys.

### Python API (`PBIPGenerator`)

Embed the planner inside automation or notebooks by aliasing the orchestrator:

```python
from pathlib import Path
from nl2pbip.orchestrator import Orchestrator as PBIPGenerator, register_builtin_tools
from nl2pbip.llm_client import StructuredLLMClient
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY

workspace = Path("artifacts/workspace")
workspace.mkdir(parents=True, exist_ok=True)
context = {
    MODEL_PATH_KEY: str(workspace / "semantic_model" / "model.tmdl"),
    REPORT_PATH_KEY: str(workspace / "report_workspace.json"),
    "project_name": "SalesInsights",
    "package_path": "artifacts/pbip-sales",
    "overwrite": True,
}

llm = StructuredLLMClient(provider="openai", model="gpt-4o-mini")
generator = PBIPGenerator(llm_client=llm)
register_builtin_tools(generator)

results = generator.run(
    "Executive dashboard with YoY measures, heatmap, and RLS per territory",
    context=context,
)
print(results[-1].output["project_path"])
```

`PBIPGenerator` (an alias of `Orchestrator`) returns structured `ToolResult` objects so you can inspect intermediate tool outputs, log them, or enforce custom compliance checks before persisting artifacts.

## LLM Fine-Tuning Module (`nl2pbip.finetune`)

Fine-tune open-weight coders such as **Qwen 2.5 Coder 7B** on curated TMDL + PBIR schemas to run nl2pbip entirely offline.

1. **Synthetic dataset generation** – uses OpenAI + `instructor` to capture validated ChatML records:

   ```bash
   nl2pbip-ft generate-dataset \
     --model gpt-4o-mini \
     --prompt-file prompts/nl_requirements.txt \
     --train-output finetune/train.jsonl \
     --val-output finetune/val.jsonl

   # From source you can also run: python -m nl2pbip.finetune.dataset_generator ...
   ```

2. **QLoRA training with Unsloth** – wraps `unsloth.FastLanguageModel`, `trl.SFTTrainer`, and Hugging Face datasets:

   ```bash
   nl2pbip-ft train \
     --train-file finetune/train.jsonl \
     --val-file finetune/val.jsonl \
     --output-dir finetune/output \
     --model-name unsloth/Qwen2.5-Coder-7B-Instruct \
     --epochs 2 \
     --batch-size 1 \
     --gradient-accumulation 4 \
     --export-gguf

   # Equivalent module call: python -m nl2pbip.finetune.train --export-gguf ...
   ```

3. **Export GGUF for Ollama / llama.cpp runtimes** – uses `FastLanguageModel.export_gguf` with quantization presets:

   ```bash
   nl2pbip-ft export \
     --adapter-path finetune/output/adapter \
     --gguf-dir finetune/output/gguf \
     --quantization q4_k_m

   # When invoked via `nl2pbip-ft train --export-gguf`, adapters + GGUF artifacts land under `finetune/output/`.
   ```

  When hacking on the repo before console entry points are installed, import `export_to_gguf` from
  `nl2pbip.finetune.train` and call it directly against the saved adapter directory.

Artifacts created in `finetune/output/gguf` can be served through Ollama (`ollama create nl2pbip -f Modelfile`) or vLLM; point `nl2pbip generate --provider custom --base-url http://localhost:8000/v1` at that endpoint for private inference.

## Export Engine (`.pbip` → `.pbix` / `.pbit`)

`PBIPExporter` stitches semantic + report directories into binary Power BI files. You can call it directly or via CLI:

### CLI exports

```bash
# Convert an existing PBIP directory to PBIX
nl2pbip export \
  --input artifacts/pbip-20240925/SalesInsights.pbipdir \
  --format pbix \
  --output dist/SalesInsights.pbix

# Generate a template (PBIT) during plan execution
nl2pbip generate \
  --prompt "Finance P&L with departmental RLS" \
  --export pbit \
  --export-output dist/FinanceTemplate.pbit
```

- `.pbix` exports require `pbi-tools` to be on `PATH`. The exporter streams `pbi-tools compile` and surfaces the log output.
- `.pbit` exports fall back to an in-process ZIP that writes dataset/report folders plus `Metadata.json` timestamps.

### Python API exports

```python
from nl2pbip.exporter import PBIPExporter

exporter = PBIPExporter()
pbip_dir = "artifacts/pbip-sales/SalesInsights.pbipdir"

if not exporter.export_with_pbi_tools(pbip_dir, "pbix", "dist/SalesInsights.pbix"):
    exporter.export_as_pbit_zip(pbip_dir, "dist/SalesInsights.pbit")
```

This dual-path approach lets CI or air-gapped build agents still emit templates even when `pbi-tools` is unavailable.

## CI/CD Integration

A ready-to-use workflow lives in `.github/workflows/ci.yml` and executes formatting, linting, and tests against Python 3.10–3.12. Minimal template:

```yaml
name: CI
on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  tests:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip
      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install .
          pip install black ruff pytest pytest-cov
      - name: Lint
        run: |
          black --check .
          ruff check .
      - name: Test
        run: pytest -v --cov=. --cov-report=term-missing
```

Populate the matrix-level environment variables (`OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, etc.) with low-privilege workspace keys so the CLI smoke tests can execute plan validation without touching production tenants.

## Next steps

- Browse `tests/` for pytest samples that exercise the exporter, validator, and linter components.
- Extend `dax_library.json` with your own calculation groups and measurement templates so planners lean on approved logic.
- Wire `nl2pbip generate` into deployment automation (e.g., GitHub Actions + `pbi-tools push`) to continuously ship fully reproducible Power BI apps from natural-language specs.
