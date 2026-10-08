# Contributing to Pakati

Open an issue to describe a bug or proposed change, or send a pull request with a focused fix. Include reproduction steps and the checks you ran.

## Source layout

- `Sources/AgentRelayApp.swift`: native SwiftUI app, task state, notes, and helper runner.
- `Engine/`: checkpoint engine, project configuration, hooks, and backend integration checks.
- `Engine/autopilot.py`: managed CLI execution, quota detection, cancellation, and automatic continuation.
- `Tests/`: packaged engine and native model/runner checks.
- `Assets/` and `Tools/generate-icon.swift`: Pakati branding and icon generator.
- `build.py` and `build-mac.sh`: Apple Silicon app and disk image packaging.

Some Agent Relay identifiers remain for compatibility with existing tasks and project hooks. Preserve that compatibility when changing storage or configuration.

## Validate a change

Follow the build and test commands in [README.md](README.md). Run checks relevant to the change; use disposable Git projects and scratch folders under `work/`. Native tests require an Apple Silicon Mac and the Swift command-line toolchain.

For packaged helper changes, build the engine and run:

```sh
./build-mac.sh --engine-only
python3 Tests/test_packaged.py --engine "$PWD/work/dist/relay-engine" --scratch "$PWD/work/engine-tests"
python3 Tests/test_auto_packaged.py --engine "$PWD/work/dist/relay-engine" --scratch "$PWD/work/auto-packaged-tests"
```

Describe any limits in your verification. Changes to checkpoint formats should include a compatibility plan and preserve existing work on failure.

For managed handoff changes, run:

```sh
python3 Engine/test_autopilot.py
python3 Tests/test_auto_regressions.py --scratch "$PWD/work/auto-regressions"
python3 Tests/test_auto_model.py --scratch "$PWD/work/auto-model-tests"
```

These tests use fake local command-line agents and disposable Git repositories, without model calls or subscription usage. The managed model driver runs through the Swift interpreter/JIT without creating an app or helper build. Treat successful agent output as the end of a run, and classify account exhaustion only from supported lifecycle events. Preserve exclusive checkout ownership and wait for child processes to stop before capturing and transferring files.

Contributions are licensed under the project's [MIT license](LICENSE).
