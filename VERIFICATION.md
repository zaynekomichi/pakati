# Verification — Pakati for Mac 0.2.0

The packaged-app checks below describe the existing 0.2.0 downloads. The automatic handoff source update is documented separately at the end; it has not been packaged into a new app or installer.

Verified on an Apple Silicon Mac running macOS 26.6.2. The native app targets macOS 13 or later; macOS 13 itself and Intel Macs were not used for runtime testing. The app and helper are arm64 builds.

## Automated checks

**31 checks passed across the packaged engine and native model/runner test drivers:**

| Test driver | Passed | Coverage |
| --- | ---: | --- |
| `Tests/test_packaged.py` | 20 | Executes the frozen helper, including capture/restore, version guards, integrity checks, existing-destination refusal, project configuration preservation, and generated hook commands. |
| `Tests/test_notes_model.py` | 7 | External edits preserve both project notes and editor drafts; drafts survive relaunch; explicit reload resets the baseline; an untouched editor refreshes newer notes; symlink notes are refused; explicit note saves preserve existing `0600` permissions; equivalent canonical and symlink store paths match, including before the first checkpoint directory exists and with missing parent directories; unrelated, relative, wrong-task, and disabled configurations are refused. |
| `Tests/test_runtime_runner.py` | 4 | Unchanged helpers are reused; changed bytes refresh the cache even with the same version; runtime symlinks are refused; both 256 KiB stdout and 256 KiB stderr are drained without deadlock. |

The engine tests exercised linked Git worktrees, unpushed commits, staged and unstaged changes preserved independently, binary files, deletions, executable bits, nonignored new files, ignored/secret-like exclusions, stale-source guards, notes reuse and timestamps, and failures that preserve the previous checkpoint. Setup preserved existing project rules and unrelated assistant settings, remained idempotent, and retained authored notes.

Generated hook commands were executed using literal paths containing spaces, apostrophes, and command-substitution characters. The fixture command-substitution sentinel was not created. Hook events, including a simulated Claude `rate_limit` failure, were supplied as local JSON fixtures.

The 18 original source-backend integration tests also passed. They overlap the packaged-engine coverage and are not counted again in the total above. The ad-hoc signed app's smoke mode also passed five note-conflict/draft assertions overlapping the native model checks, and verified that the bundled helper could be installed at its stable runtime path and invoked.

## Native app and packaging checks

The native app compiled, passed local ad-hoc signature verification, and launched. The task view scrolls vertically on shorter windows; the notes editor has a finite height. Its GUI was used to configure a disposable Git project, save a checkpoint, and restore into a new folder. The restored fixture contents were checked against the saved work. These actions were performed with an isolated app data directory.

The delivered disk image was mounted read-only and its app passed strict, deep signature verification and isolated smoke checks. The installed helper remained usable after the image was detached. The app ZIP was extracted into a clean temporary directory and its signature also passed.

The helper bundles Python 3.12.14 using PyInstaller 6.22.0. Mach-O inspection showed a macOS 11 deployment minimum for both the helper bootloader and bundled Python library, consistent with the app's macOS 13 deployment target. This inspection does not establish compatibility on every supported macOS release.

The app still requires Git. A bundled Python runtime does not remove that requirement.

## Limits of this verification

- Actual Codex and Claude desktop hook trust, startup, and subscription-limit lifecycles have not been validated in a live assistant session. Local hook-event fixtures do not prove that every desktop usage-limit screen invokes a hook.
- Native conversations, hidden model state, and automatic submission of a continuation prompt were not transferred or tested. The user opens the restored folder and starts the continuation conversation.
- The build is ad-hoc signed for local testing. It has no Apple Developer ID signature and has not been notarized. Successful local signature verification does not establish Gatekeeper approval on another Mac. Public distribution requires the appropriate signing and notarization workflow. [Apple's distribution guidance](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
- Testing used disposable repositories and isolated app data. No real user project or global Codex/Claude configuration was changed during verification.

## Repeat the test drivers

From the source package, after building the helper, use an absolute helper path and a disposable workspace directory:

```sh
python3 Tests/test_packaged.py --engine /absolute/path/to/relay-engine --scratch /absolute/path/to/work/engine-tests
python3 Tests/test_notes_model.py --scratch /absolute/path/to/work/model-tests
python3 Tests/test_runtime_runner.py --scratch /absolute/path/to/work/runner-tests
```

The two native test drivers require the Swift command-line toolchain. They compile test harnesses from the actual native app source and isolate their state in the supplied scratch directory. Testing does not require model calls or assistant accounts.

## Pakati branding update

The app is named Pakati. Its Dock icon and in-app mark use a solid black center dot, white surround, and sixteen radial black lines. Legacy storage and hook identifiers are retained for compatibility. This update changes branding and bundled helper display text; handoff behavior and checkpoint format are unchanged. The rebuilt app and helper are checked again before delivery.

## Automatic handoff source update (unbuilt)

The source adds a managed controller for installed Codex and Claude Code command-line agents, plus native Start/Stop controls, saved executable paths, and live task status. Confirmed account quota failures trigger a final checkpoint, a fresh restore, configuration and adoption of that checkout, then continuation with the other agent. The default allows one switch; both exhausted accounts stop the run.

Verification uses fake local agents and disposable Git repositories. It does not send model requests, consume subscription credits, or change global assistant configuration. The current app downloads and source archive remain the previously delivered versions; use the Git repository for this source update.

**48 Python checks passed:** 18 original source-engine integration tests, 13 managed-controller tests, and 17 independent automatic-handoff regression tests. Coverage includes final capture and fallback continuation, both accounts exhausted, permission denial, temporary limits and quoted quota text, checkpoint ownership, stopped and killed controllers, child-process cleanup, and stale state handling.

**Eight native model check groups passed through the Swift interpreter/JIT.** They cover saved settings, bounded state parsing, draft preservation, same-folder write exclusion, worker generation guards, stale polling, cleanup status, and cancellation before launch. Installed Codex 0.160.0 and Claude Code 2.1.285 passed read-only version/help preflight checks.

The controller checks ordinary response completion separately from quota failure, respects denied tool permissions, preserves agent edits to notes, and maintains exclusive checkout ownership. A successful CLI response is not proof that the user's entire goal has been completed. Subscription event payloads can change; unsupported failures stop with a status instead of guessing and launching a fallback.

Crash and cancellation cleanup covers the managed process group and its ordinary descendants. Intentionally detached processes that create another session and drop inherited locks are outside that guarantee.

The complete native source passed Swift typechecking for arm64 with a macOS 13 deployment target, without creating an app build. Live authenticated agent execution and end-to-end GUI interaction with this new mode are not covered by the fake-agent checks.
