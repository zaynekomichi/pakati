# Contributing to Pakati

Open an issue to describe a bug or proposed change, or send a pull request with a focused fix. Include reproduction steps and the checks you ran.

## Source layout

- `Sources/AgentRelayApp.swift`: native SwiftUI app, task state, notes, and helper runner.
- `Engine/`: checkpoint engine, project configuration, hooks, and backend integration checks.
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
```

Describe any limits in your verification. Changes to checkpoint formats should include a compatibility plan and preserve existing work on failure.

Contributions are licensed under the project's [MIT license](LICENSE).
