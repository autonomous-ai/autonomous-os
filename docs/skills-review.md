# Skills review — 2026-09-10

Scope: all 28 `skills/*/SKILL.md` entrypoints, with references and implementation checks for concrete findings. This is a source review, not a claim that every runtime or physical action has been tested. Preserve event thresholds, consent, identity attribution, device capabilities, and task completion requirements.

## Applied and verified locally

- `computer-use`: stop on known unavailable Buddy state; one read-only check when unknown; retain permission/focus/result verification. Its vision reference follows the same gate. No runtime status injection was added.
- `connectors`, `face-enroll`, `servo-control`, `wellbeing`: repair invalid YAML descriptions. The connectors description is shortened; full credential discovery, secret handling and write confirmation remain in the body. No API or capability changes.
- `user-emotion-detection`: replace the contradictory bash POST batch instruction with the existing inline log markers. Keep signal, mood decision, and selected-route logging; no duplicate shell writes.
- `wellbeing`: correct the summary that skipped every reaction log. Preserve the existing `noted_yawn` cooldown log and morning/sleep/meal route logs; ordinary backend-logged activities are not logged twice. Thresholds and priority order are unchanged.
- `sensing`: the earlier sound-only fast path is already committed; occurrence 1 and 3 were observed on device in English. That narrow test does not establish behavior for all sensing events or languages.

Validation so far: all 28 entrypoints pass the repository frontmatter validator after repairs; `go test ./system/skills/` passes; Buddy helper suite passes 19 tests. The helper tests do not evaluate model instruction adherence.

## Findings and disposition

The bounded tuning pass is complete. In addition to the changes above:

- Applied camera/servo capture consistency, executable hardware templates, display info retention, explicit guard-off confirmation, removal of spoken `(POST)`, and HAL-owned LED effect stopping.
- Audio recording now saves WAV bytes to a private temporary directory, reports success only after a successful HTTP response, and does not retry uncertain recording. Explicit audio actions skip optional generic preflight; relative volume still reads current volume.
- Mood/music suggestion reuse injected context plus the current signal. Habit intent logs use the existing wellbeing marker once per intent. Voice's entrypoint distinguishes automatic speech, additional speech and privacy controls without changing meeting behavior.
- Sensing history selects all local dates intersecting a relative interval and reports unavailable files rather than interpreting missing history as no events. Midnight is no longer confused with noon.

The table records the review evidence, including deliberately retained behavior; it is not a list of unfinished edits promised by this audit.

| Skill(s) | Evidence / opportunity | Preserve |
| --- | --- | --- |
| `camera`, `servo-control` | Camera's final rules and compound servo example still use raw snapshot for visual questions, conflicting with `/api/vision/look`; `system/server/vision.go` documents text-only hallucination risk. | Reuse supplied images; explicit aim/hold before looking; raw frame export remains available. |
| `led-control` | `/led/solid` and `/led/paint` already stop effects in `hal/routes/led.py`; skill adds redundant stop markers. | Explicit stop, gradient, persistence and brightness behavior. |
| `scene`, `led-control`, `servo-control`, `display` | Final output templates use descriptive labels rather than executable HW markers. | Existing action parameters and user-facing confirmation semantics. |
| `display` | Info immediately followed by eyes-mode erases it; display service switches immediately. | No invented duration/delay API. |
| `guard` | “I'm back” disables in trigger table but return workflow requires confirmation. | Explicit guard-off commands and alarm choreography. |
| `audio` | Recording example sends binary WAV to shell output; endpoint returns WAV bytes. | Requested recording duration and error handling. |
| `sensing-track` | Recent-history recipe reads only today, missing prior-day events across midnight; midnight example says 12pm. | Retention and missing-file distinctions. |
| `servo-tracking` | `(POST)` text follows markers in examples and can be spoken. | Aim/track/stop behavior and JSON bodies. |
| `mood` | “Immediately read history” conflicts with supplied emotion context and marker-only writes. | Current signal must participate in synthesis; missing-context fallback. |
| `habit` | Explicit intent logging uses curl although the same wellbeing marker is supported. | One intent log per turn and existing posture history support. |
| `music-suggestion` | History-learning wording could reuse injected `audio_recent` more clearly. | No automatic playback and existing cooldown. |
| `voice` | Opening “only explicit speech” wording omits privacy handling; meeting examples disagree on mic-only versus both mutes. | Do not silently choose a new privacy behavior. |
| `speaker-recognizer` | Description excludes unnamed fragments, matrix asks enrollment follow-up, and forced acknowledgment conflicts with SOUL's meaningless-fragment silence. | Self-enrollment, same-speaker cluster and audio requirements; resolve routing before editing. |
| `connectors` | Discover labels entries connected without checking token presence and suppresses read/parse errors; later reads assume per-connector storage despite generic discovery. | Credential secrecy, official destinations and confirmation rules; use synthetic fixtures before changing discovery. |
| `harness-use`, `computer-use`, `agent-management` | Bare personal names are not proof of Harness targeting. Existing response-route tag is not a target selector. | Use actual selected/listed agent evidence; keep Buddy independent. No routing patch in this batch. |

## Reviewed without a selected behavioral change

- `emotion`: inline reactions and automatic eye synchronization already avoid duplicate calls.
- `music`: direct playback/stop markers and bounded vague-request discovery are appropriate.
- `input-branching`: handled voice remains silent while required bookkeeping is preserved.
- `face-enroll`: per-flow references preserve enrollment identity/confirmation boundaries; only metadata repaired.
- `claude-buddy`: legacy approval protocol is separate from Autonomous Buddy/Harness; do not merge based on its name.
- `faq`: informational reference, not permission to operate settings; local Vite proxy instructions match configuration.
- `skill-creator`: evaluation tooling is intentionally more involved than a device reaction; do not impose a sound-style stopping rule.

## Deliberately retained / follow-up decisions

- Keep connector discovery implementation unchanged in this tuning pass. Its false-connected/error-suppression and generic-storage issues need a separate credential-resolution fix with synthetic fixtures; changing authentication during prompt cleanup would go beyond preserving current behavior.
- Keep meeting mic-versus-both mute and speaker enrollment/silence precedence unchanged. These require a product decision across SOUL, routing and skills, rather than guessing which contradictory instruction is intended.
- Keep Harness routing unchanged: a bare name is not a confirmed agent. Buddy's availability gate remains independent, and `harness-reply` alone does not select a target.
- Keep posture/check-in cooldown semantics unchanged: references disagree with their router/window descriptions. Do not introduce new thresholds or delete still-used posture history.

## Final verification and limits

- All 28 frontmatters pass `python3 skills/skill-creator/scripts/quick_validate.py <skill>`; `git diff --check` passes.
- `go test ./system/skills/ ./system/server/agent/delivery/http/ ./system/lib/flow/ ./system/skillcontext/...` passes. Flow/skillcontext subpackages report no test files, so this is not behavioral coverage for them.
- `python3 -m unittest discover -s skills/computer-use/tests -q`: 19 tests pass (one inherited Python resource warning); helper code was not modified.
- All 15 Bash blocks in audio and sensing-track pass `bash -n`. The recording recipe was executed against a local mock HTTP server: success preserves WAV bytes; HTTP 503 emits no success; exactly one request per case.
- The GNU-date history recipe was syntax-checked but not executed on Linux; no latency or live-history claim is made. Model behavior and physical effects of this audit's changes have not been tested on a device. Earlier sound tests are scoped above.

Review is complete at repository level, with the retained decisions explicitly recorded. No commit, deployment or external service call was made for this tuning pass. Goal completion describes the source review and verified edits, not resolution of every separately identified product bug.
