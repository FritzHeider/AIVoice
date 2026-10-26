# Memories

## Patterns

## Decisions

## Fixes

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
