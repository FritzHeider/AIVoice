# Memories

## Patterns

## Decisions

## Fixes

### mem-1792988928-d413
> Exit-gate clean clones must live under /Users (Docker Desktop cannot bind-mount from /var/tmp: collector.yaml 'not a directory'); compose project name is fixed 'voiceagents' so a clone's make up replaces the main stack - make down/up from main repo after. uv run --no-project --python 3.12 prepends /opt/homebrew/bin to PATH, shadowing nvm node 22 with brew node 26
<!-- tags: tooling, docker, ci | created: 2026-10-26 -->

### mem-1792988571-0143
> Never importlib.reload an app module in tests: it rebinds module globals (app) in place and breaks identity asserts in tests that imported the old object (xdist order-dependent). Use monkeypatch.delitem(sys.modules, mod) + setattr(pkg, name, orig) then import_module for a fresh copy
<!-- tags: testing, pytest | created: 2026-10-26 -->

### mem-1792988312-0c44
> gitleaks full-history scan: plan/phase-20.md:48 'key rotation, Telnyx/ElevenLabs' is a generic-api-key false positive, allowlisted in .gitleaks.toml (path+match AND); the word 'placeholders' in .py trips check_no_placeholders filler-word rule unless written as check_no_placeholders
<!-- tags: tooling, ci | created: 2026-10-26 -->

### mem-1792987695-026b
> pnpm audit in TS workspace: vitest 3.x pulls critical tinypool advisories (use vitest ^4.1.12); next 15.5 pins postcss 8.4.31 (override postcss@<8.5.23 in root package.json pnpm.overrides); Next 15.5 next-env.d.ts references .next/types so git-ignore it
<!-- tags: tooling, typescript, pnpm | created: 2026-10-26 -->

### mem-1792986234-2b28
> uv_build workspace app whose import package differs from the dist name needs [tool.uv.build-backend] module-name = "va_x" in its pyproject, else uv sync fails 'Expected a Python module at src/va_<dist>'
<!-- tags: tooling, uv | created: 2026-10-26 -->

### mem-1792986234-b9d2
> uvicorn Server.serve calls sys.exit() on startup failure; inside an asyncio task SystemExit escapes the loop - catch it in the coroutine (va_common.service._serve -> ServerStartupError). Cancelling a running uvicorn task without shutdown leaks its listening socket (pytest unraisable warning in a later test)
<!-- tags: testing, uvicorn, service | created: 2026-10-26 -->

### mem-1792982690-b4e4
> bash 3.2 (stock macOS /bin/bash) under set -u errors on "${arr[@]}" when arr is empty; guard with ${#arr[@]} checks; test scripts with /bin/bash too
<!-- tags: tooling, bash | created: 2026-10-26 -->

### mem-1792981166-10bf
> aioboto3 session.client(config=...) needs aiobotocore.config.AioConfig; mypy rejects botocore.config.Config
<!-- tags: aws, mypy | created: 2026-10-26 -->

### mem-1792981166-ce01
> pytest-xdist: never parametrize with random values (os.urandom) - workers collect different test IDs and the run errors; parametrize sizes and generate inside the test
<!-- tags: testing, pytest | created: 2026-10-26 -->

### mem-1792920384-602e
> build.done backpressure parser substring-matches plain text tokens 'tests: pass', 'lint: pass', 'typecheck: pass', 'audit: pass', 'coverage: pass', 'duplication: pass', 'complexity: <number>'; emit build.done as a plain string payload, NOT --json (JSON keys like "tests":"pass" never match)
<!-- tags: tooling, ralph | created: 2026-10-25 -->

### mem-1792920274-acc2
> build.done payload must include backpressure keys tests, lint, typecheck, audit, coverage, complexity, duplication (performance/specs optional) or Ralph converts it to build.blocked; obtain audit via uvx pip-audit on uv export + pnpm audit, complexity via uvx radon cc, duplication via jscpd
<!-- tags: tooling, ralph | created: 2026-10-25 -->

### mem-1792920236-93ac
> ruff>=0.17 formats python code blocks in Markdown; root pyproject extend-exclude includes *.md to protect specs/
<!-- tags: tooling, ruff | created: 2026-10-25 -->

## Context
