# Abide Companion - Design Notes

A running log of architectural decisions, trade-offs, and known limitations. This file is the source of truth for why the codebase is the way it is.

Format for each entry:
- Decision - what we did
- Context - why it matters
- Alternatives considered - what else we looked at
- Trade-offs - what we gave up

---

## Phase 1-2: Foundation

### D1. Single-file HTML frontend, no framework
- Decision: Keep the frontend in one frontend/index.html file with inline CSS/JS, no React/Vue/build tooling.
- Context: First-run UX had to be double-click simple for non-technical users.
- Alternatives considered: React + Vite, Vue, componentized SPA builds.
- Trade-offs: Less component reuse and stricter discipline in one large file, but near-zero setup friction.

### D2. FastAPI with one WebSocket endpoint
- Decision: Use a single /ws channel for audio up/down, control messages, and vision events.
- Context: Low-latency interrupt handling and simpler browser/server coordination.
- Alternatives considered: HTTP + SSE split, multiple sockets, WebRTC data channels.
- Trade-offs: We own message-type multiplexing logic, but reduce transport complexity.

### D3. silero-vad runs locally in-process
- Decision: Run VAD locally with silero-vad + torch in the backend process.
- Context: VAD sits on the critical barge-in path; network calls here are too expensive.
- Alternatives considered: Remote VAD APIs, browser-only energy thresholds.
- Trade-offs: Bigger dependency footprint, much lower runtime latency.

### D4. Linear interpolation for 48k to 16k downsampling
- Decision: Use lightweight interpolation in audio.py for VAD input.
- Context: VAD needs speed and stability more than studio-grade resampling.
- Alternatives considered: scipy/librosa/polyphase filters.
- Trade-offs: Minor signal-fidelity loss in exchange for lower CPU and fewer deps.

### D5. Direct httpx for Anthropic calls
- Decision: Use direct httpx streaming for Claude instead of the Anthropic SDK.
- Context: Windows reliability was better with direct HTTP/2 control.
- Alternatives considered: SDK pinning and cert workarounds.
- Trade-offs: More custom SSE handling code, fewer opaque SDK failures.

### D6. Persistent module-level HTTP/2 clients
- Decision: Keep one long-lived httpx AsyncClient with HTTP/2 per module.
- Context: Per-request clients were paying repeated TCP/TLS setup overhead.
- Alternatives considered: Per-call clients, shared HTTP/1.1 clients.
- Trade-offs: Need careful lifecycle handling; major latency win.

### D7. Rolling conversation history cap
- Decision: Keep in-memory history capped (initially 20 messages, later expanded in D94).
- Context: Token growth over longer sessions hurt cost and latency.
- Alternatives considered: Unbounded history, full summarization pipeline.
- Trade-offs: Old turns roll off, but session remains responsive.

---

## Phase 3-5: Voice Loop, TTS, and Barge-In

### D8. Sentence-boundary TTS streaming
- Decision: Start TTS at first sentence boundary instead of waiting for full Claude completion.
- Context: Needed faster first audio for conversational feel.
- Alternatives considered: Full-response TTS only.
- Trade-offs: More TTS calls and queue logic, much better perceived latency.

### D9. Producer/consumer parallel TTS pipeline
- Decision: Split generation and playback into concurrent producer/consumer tasks.
- Context: Serial synthesis delayed downstream sentences.
- Alternatives considered: Fully sequential TTS.
- Trade-offs: More asyncio coordination; smoother turn flow.

### D10. OpenAI TTS opus format
- Decision: Use response_format=opus for TTS payloads.
- Context: Smaller payloads improve transfer time.
- Alternatives considered: mp3, wav.
- Trade-offs: Browser decode requirements, best latency/quality balance.

### D11. Web Audio API playback (not audio element)
- Decision: Decode/play with AudioContext and source nodes.
- Context: Barge-in needs deterministic stop behavior.
- Alternatives considered: HTML audio element.
- Trade-offs: More custom client playback code; precise interruption control.

### D12. Cooperative cancellation first
- Decision: Favor cancel flags and cooperative stop points before hard task cancel.
- Context: Preserve stream state and partial replies safely.
- Alternatives considered: Immediate hard cancellation everywhere.
- Trade-offs: Slightly more orchestration; fewer broken stream edge cases.

### D13. Save partial assistant output on interruption
- Decision: Persist partial assistant content into history when interrupted.
- Context: Prevent repeated restarts and duplicate phrasing next turn.
- Alternatives considered: Drop partial output.
- Trade-offs: History may contain truncated assistant turns, but continuity is better.

### D14. Sustained-speech barge-in gate (initial)
- Decision: Add sustained-speech timing gate before barge-in trigger.
- Context: Reduce echo/bleed false positives.
- Alternatives considered: Immediate trigger on any VAD speech.
- Trade-offs: Higher barge-in latency but far fewer accidental cuts. (Refined in D67 with loud-window counting; retuned for MeetUp hardware AEC in D80.)

### D15. Sequential decode plus epoch guard
- Decision: Decode queued TTS clips in strict order and discard stale callbacks with epoch IDs.
- Context: Parallel decode caused out-of-order playback glitches.
- Alternatives considered: Parallel decode with post-sort.
- Trade-offs: Slight decode serialization overhead, deterministic output order.

---

## Phase 6: Vision Foundation

### D16. Browser-side frame capture
- Decision: Capture camera frames in browser and send encoded images to backend.
- Context: Browser already owns media permission flow and device access.
- Alternatives considered: Server-side camera capture.
- Trade-offs: WS payload overhead, cleaner deployment model.

### D17. Base64 JSON frame transport
- Decision: Send vision frames as JSON with base64 payloads.
- Context: Avoid binary-frame protocol collisions with audio channels.
- Alternatives considered: Binary multiplexer/magic bytes.
- Trade-offs: Extra payload size, simpler protocol handling.

### D18. Fire-and-forget vision worker with drop-if-busy
- Decision: Skip new vision batches while previous inference is in flight.
- Context: Prevent backlog amplification and voice-loop interference.
- Alternatives considered: Queue all frames, cancel previous work.
- Trade-offs: Occasional dropped frame batches, stable runtime behavior.

### D19. Rolling vision context buffer with relative time
- Decision: Maintain recent scene observations with relative timestamps for prompt context.
- Context: Companion behavior depends on short-term activity continuity.
- Alternatives considered: Single latest scene only, long raw scene history.
- Trade-offs: Modest token overhead, much better temporal grounding.

### D20. Compact vision activity schema
- Decision: Enforce short activity labels and forbid appearance/emotion speculation.
- Context: Reduce noisy or inappropriate descriptions.
- Alternatives considered: Free-form descriptions.
- Trade-offs: Less expressive text, safer and cheaper prompts.

### D21. Bounding box overlay from vision output
- Decision: Require bbox in vision response and render on canvas.
- Context: Makes model interpretation auditable to users.
- Alternatives considered: Text-only scene summaries.
- Trade-offs: Approximate boxes, major UX trust gain.

### D22. Video-first branded UI direction
- Decision: Build around a 16:9 hero video region and calm companion styling.
- Context: Product should feel like an in-room agent, not a debug console.
- Alternatives considered: Form-heavy utility layout.
- Trade-offs: More frontend polish work, better product signaling.

### D23. Multi-frame vision sampling
- Decision: Move from single frame to short frame sequences per call.
- Context: Motion classification requires temporal evidence.
- Alternatives considered: Single frame with heuristics.
- Trade-offs: Higher vision cost/latency per call, much better activity quality.

### D24. Fall detection via FALL prompt contract
- Decision: Use model-driven FALL prefix to trigger alerts and urgent context.
- Context: Safety behavior had to be explicit and visible.
- Alternatives considered: Separate classical fall classifier only.
- Trade-offs: Some false positives; preferred over false negatives. (Pose-landmark second signal added in D93, later disabled in D95 after false positives on seated-bent-over postures; vision FALL prefix remains the sole fall path.)

### D25. Image-first prompt ordering
- Decision: Put image content first and de-emphasize stale prior text.
- Context: Prevent model anchoring/confirmation bias from previous labels.
- Alternatives considered: Prior context first, larger textual history.
- Trade-offs: Slight continuity loss, far better scene correctness.

### D25b. RMS-gated barge-in pre-check
- Decision: Add loudness gate to sustained-speech trigger.
- Context: Echo and tiny spikes were still triggering interruptions.
- Alternatives considered: Raise timing threshold only.
- Trade-offs: Very quiet speech may not interrupt immediately; far fewer false triggers.

### D25c. Minimal Whisper bias prompt for product name
- Decision: Keep Whisper prompt narrowly focused on uncommon name disambiguation.
- Context: Broad prompt content was causing hallucinated stock phrases.
- Alternatives considered: Larger example prompt lists.
- Trade-offs: Less bias coverage, better transcription integrity.

---
## Phase 7: Observability

### D26. Langfuse v2 pin
- Decision: Standardize on Langfuse v2 APIs.
- Context: v2 fit the current explicit trace/span model with less migration risk.
- Alternatives considered: Immediate v3 migration.
- Trade-offs: Missed v3 features, lower integration churn.

### D27. Conversation telemetry side-channel
- Decision: Store per-turn metrics on engine instance fields during streaming.
- Context: Async generator streaming made tuple-return telemetry awkward.
- Alternatives considered: Callback plumbing, altered stream payload shape.
- Trade-offs: Single-turn concurrency assumption, cleaner call surface.

### D28. Trace model separation
- Decision: Keep turn traces, vision traces, and session summaries as separate top-level artifacts.
- Context: Vision cadence is independent from conversational turns.
- Alternatives considered: Force vision spans under nearest turn.
- Trade-offs: More trace types, truer system timeline.

### D29. Server-only Langfuse credentials
- Decision: Langfuse keys live in backend env, never in browser key drawer.
- Context: Telemetry is operator concern, not end-user setup.
- Alternatives considered: User-supplied telemetry keys.
- Trade-offs: Env management required for devs; cleaner user UX and security posture.

### D30. Telemetry must be non-blocking no-op on failure
- Decision: Guard telemetry calls so outages never impact voice loop.
- Context: Companion runtime cannot depend on observability availability.
- Alternatives considered: Fail-fast telemetry errors.
- Trade-offs: Some telemetry failures become soft and require log review.

---

## Phase 8: Hardening and Security Sweep

### D31. Typed ConversationError plus generic client errors
- Decision: Use typed errors internally and user-safe generic messages externally.
- Context: Raw upstream errors leaked too much detail.
- Alternatives considered: Pass-through exception strings.
- Trade-offs: Less user-visible diagnostics, safer surface.

### D32. WebSocket payload limits and type guards
- Decision: Add explicit size/type validation for audio and frame payloads.
- Context: Avoid malformed input crashes and memory abuse.
- Alternatives considered: Implicit trust of client payloads.
- Trade-offs: Potential drop of oversized legit payloads, better resiliency.

### D33. Delimited camera observation injection
- Decision: Wrap camera observations in explicit read-only tags/instructions.
- Context: Prompt injection defense against OCR/inference artifacts.
- Alternatives considered: Unwrapped inline text.
- Trade-offs: Small token overhead, stronger prompt safety.

### D34. STT hard timeout
- Decision: Wrap STT calls in explicit timeout.
- Context: Avoid hanging the full response pipeline on vendor stalls.
- Alternatives considered: Vendor default timeout only.
- Trade-offs: Rare timeout retries, better liveness.

### D35. Persist partial streams from finally
- Decision: Ensure partial Claude text is saved even on interrupted/errored streams.
- Context: Preserve conversational continuity in failure paths.
- Alternatives considered: Save only on success path.
- Trade-offs: Slightly more state handling complexity.

### D36. Log prewarm task exceptions
- Decision: Attach done callbacks to background prewarm tasks.
- Context: Silent startup task failures made diagnosis hard.
- Alternatives considered: Fire-and-forget with no callbacks.
- Trade-offs: Extra logging noise, better operational visibility.

### D37. Bound latency arrays and queue depth
- Decision: Cap metrics list growth and queue sizes.
- Context: Long sessions should not accumulate unbounded memory.
- Alternatives considered: Unbounded collections.
- Trade-offs: Rolling-window stats instead of full-session absolute history.

### D38. Audio buffer validation before numpy conversion
- Decision: Validate chunk structure/size before np.frombuffer.
- Context: Corrupt payload lengths can raise and break session loop.
- Alternatives considered: Blind conversion.
- Trade-offs: Tiny per-chunk checks, stronger robustness.

### D39. Remove redundant float32 copies in RMS path
- Decision: Skip unnecessary astype copies where dtype is already guaranteed.
- Context: Hot-path allocations added avoidable CPU and GC churn.
- Alternatives considered: Keep defensive cast always.
- Trade-offs: Relies on upstream dtype guarantees.

### D40. client_playing state for audible-window barge-in
- Decision: Track actual client playback state separately from server response-task state.
- Context: Fixed barge-in dead window after server finished sending but client still playing.
- Alternatives considered: Estimate playback from byte counts or timers.
- Trade-offs: Extra client/server control events, correct interrupt behavior.

### D41. Whisper hallucination blocklist
- Decision: Drop known boilerplate hallucination phrases post-STT.
- Context: Model occasionally emitted memorized subtitle/video phrases on ambiguous audio.
- Alternatives considered: Aggressive audio thresholding only.
- Trade-offs: Rare false drops if user literally says blocked phrase. (Extended with mixed-script rejection and single-token alphanumeric floor in D96 to catch multi-language fabrications after D91 removed the English-only lock.)

### D42. Startup auth checks moved off critical path
- Decision: Run telemetry connectivity checks in background with timeouts.
- Context: Startup should not block on telemetry network conditions.
- Alternatives considered: Blocking startup auth validation.
- Trade-offs: Two-phase startup log signaling.

### D43. Remove unsafe dotenv fallback
- Decision: Load .env from explicit project path only; guard parse failures.
- Context: CWD fallback could load attacker-controlled env files.
- Alternatives considered: Generic dotenv search behavior.
- Trade-offs: Less flexibility in odd launch dirs, safer default.

### D44. Remove duplicated partial-save path
- Decision: Delete redundant session-level save_partial after engine-level finally fix.
- Context: Prevent duplicate interrupted assistant turns in history.
- Alternatives considered: Keep both save paths.
- Trade-offs: None; cleaner single source of truth.

### D45. Staleness clamp on playback state
- Decision: Auto-clear stale client_playing=true after long inactivity window.
- Context: Prevent permanently stuck audible state from one bad client path.
- Alternatives considered: Trust client to always send end event.
- Trade-offs: Extra timestamp checks, better self-healing.

### D46. Non-root Docker runtime (pre-native phase)
- Decision: Run container as unprivileged user.
- Context: Security hardening while Docker was still active.
- Alternatives considered: Root default.
- Trade-offs: Slight image setup complexity.

### D47. .env.example secret-safety warning
- Decision: Add explicit warning to avoid committing real secrets.
- Context: Template misuse was a realistic accidental leak path.
- Alternatives considered: Implicit convention only.
- Trade-offs: Documentation-only change.

### D48. Whisper confidence filter (verbose_json) plus standalone phrase filter
- Decision: Combine segment confidence gating with phrase-level rejection.
- Context: Catch confident-looking but low-signal hallucination outputs.
- Alternatives considered: RMS-only filtering, larger hard blocklists.
- Trade-offs: Possible rejection of some edge short utterances; better overall precision. (Lexical phrase list extended for non-English fabrications in D96.)

---

## Post-launch Iteration: D49-D78

### D49. Diary tab with timestamped mixed event log
- Decision: Add live diary stream mixing user, assistant, vision, and alerts.
- Context: Care workflows needed more than transient transcript text.
- Alternatives considered: Conversation-only display.
- Trade-offs: More UI state and rendering complexity.

### D50. Session summary overlay plus analysis endpoint
- Decision: Show full session recap with generated got-right and got-wrong analysis on stop.
- Context: End-of-session review value for demos and care context.
- Alternatives considered: No summary, client-only heuristics.
- Trade-offs: Extra one-shot API call and analysis latency.

### D51. Strong proactive persona prompt
- Decision: Reframe assistant prompt toward live companion behavior.
- Context: Assistant was too passive and chatbot-like in early tests.
- Alternatives considered: Per-turn nudges only.
- Trade-offs: Needed tighter guardrails to avoid over-commentary.

### D52. 30-second silence proactive check-in loop
- Decision: Add background timer-triggered check-in when user is quiet.
- Context: Presence behavior should not require explicit user prompts.
- Alternatives considered: User-only initiation.
- Trade-offs: Risk of occasional chatty feel, mitigated by silence/audibility gates.

### D53. UserContext extraction and per-turn injection
- Decision: Extract structured user facts and inject into future turns.
- Context: Improve continuity and personalization within session.
- Alternatives considered: Regex extraction, full transcript memory.
- Trade-offs: Extra background extraction call, bounded memory lists required.

### D54. Vision-reactive responses with queueing
- Decision: Trigger proactive responses on significant vision events, queue when busy.
- Context: Reactive events were being dropped while assistant was mid-response.
- Alternatives considered: Immediate-only trigger.
- Trade-offs: Single-slot queue semantics, more predictable behavior.

### D55. start_response mutex semantics
- Decision: Cancel/replace in-flight response before starting new one.
- Context: Prevent overlapping response tasks from concurrent triggers.
- Alternatives considered: Lock plus queue.
- Trade-offs: Priority favors latest trigger, especially user speech.

### D56. Route all server sends through safe helper
- Decision: Centralize WS sends through safe helper wrapper.
- Context: Closed-socket races were throwing noisy runtime errors.
- Alternatives considered: Direct sends everywhere.
- Trade-offs: Dropped late messages on closed sockets by design.

### D57. Sample-rate coercion and bounds
- Decision: Coerce/validate sample rate input with safe defaults.
- Context: Bad config values could crash downstream resampling path.
- Alternatives considered: Trust client-provided values.
- Trade-offs: Out-of-range values ignored for stability.

### D58. Brand-aligned visual redesign plus theme toggle
- Decision: Adopt Abide visual language with warm palette, typography, hero glow, and dark/light modes.
- Context: Product credibility and consistency with brand surfaces.
- Alternatives considered: Generic utilitarian styling.
- Trade-offs: More frontend design surface and CSS complexity.

### D59. In-memory TTS cache for frequent phrases
- Decision: Prewarm and cache high-frequency stock phrase audio.
- Context: First-byte TTS latency dominated repeated phrases.
- Alternatives considered: No cache, full LRU of all sentences.
- Trade-offs: Exact phrase matching limits hit rate.

### D60. Dynamic Whisper biasing with known user name
- Decision: Add hydrated user name as narrow STT bias hint.
- Context: Proper nouns are frequently mistranscribed.
- Alternatives considered: No personalization, broad bias lists.
- Trade-offs: Slight prompt growth, better name recognition.

### D61. Welcome greeting on connect
- Decision: Fire a fast canned greeting after connect.
- Context: Cold starts felt inactive without assistant-initiated first contact.
- Alternatives considered: Wait for user to speak first.
- Trade-offs: Additional startup sequencing and history bookkeeping.

### D62. Scene confidence badge heuristic
- Decision: Add low/confident/alert visual badges from activity and bbox heuristics.
- Context: Users needed confidence cues for scene interpretation.
- Alternatives considered: No confidence indicator.
- Trade-offs: Heuristic rather than model-native probability.

### D63. Activity stability suppression
- Decision: Suppress repeated unchanged activity context for a cool-down window.
- Context: Repetitive commentary on unchanged posture was annoying.
- Alternatives considered: Lower vision frequency globally.
- Trade-offs: Slightly less context during stable windows.

### D64. Diary export as plain text
- Decision: Add one-click diary export to txt.
- Context: Easy sharing and review requirement.
- Alternatives considered: JSON or Markdown export first.
- Trade-offs: Simpler format, less structural richness.

### D65. Session resume banner on refresh (later reverted)
- Decision: Added opt-in restore of prior diary/transcript from local storage.
- Context: Accidental refreshes were losing visible session context.
- Alternatives considered: No restore option.
- Trade-offs: UX confusion in start-new flow; reverted in D74.

### D66. User-only fact extraction plus name blocklist
- Decision: Extract facts only from user turns and block assistant-like name candidates.
- Context: Prevented user-name contamination with assistant labels.
- Alternatives considered: Prompt-only fix.
- Trade-offs: Hardcoded safety blocklist for known model failure mode.

### D67. Loud-window-count gate
- Decision: Replace peak-RMS gating with sustained loud-window counting.
- Context: Single spikes triggered false interrupts. Evolves the D14 sustained-speech gate and the D25b RMS pre-check into one combined trigger.
- Alternatives considered: Just raise RMS threshold.
- Trade-offs: Slightly stricter barge-in trigger, far fewer spike false positives. (Retuned for MeetUp hardware AEC in D80.)

### D68. Prompt trim plus cache phrase expansion
- Decision: Tighten prompt verbosity and enlarge seed phrase cache list.
- Context: Reduce first-sentence delay and improve cache-hit frequency.
- Alternatives considered: Larger prompt examples.
- Trade-offs: Prompt later grew again for safety/behavior rules.

### D69. Priority hierarchy and silence gating refinement
- Decision: Clarify listen-first ordering and strengthen silence gate before proactive vision comments.
- Context: Assistant was interrupting active dialog with visual narration.
- Alternatives considered: Cooldown-only tuning.
- Trade-offs: More prompt tokens for behavior reliability.

### D70. Motion-scope vision prompt redesign
- Decision: Use ordered schema and motion-scope rules to force grounded classification.
- Context: Narrow-label misclassification persisted.
- Alternatives considered: Handwritten activity-pair rules.
- Trade-offs: Larger prompt; more robust generalization.

### D71. Correction-response shape constraints
- Decision: Add strict brevity/shape rules for correction acknowledgements.
- Context: Model over-apologized and produced verbose correction turns.
- Alternatives considered: Phrase blocklists only.
- Trade-offs: Less stylistic variation in correction moments.

### D72. Replace reactive keyword allowlist with model noteworthy
- Decision: Let vision model output semantic noteworthy boolean.
- Context: Keyword lists were brittle and high-maintenance.
- Alternatives considered: Keep curating allowlists.
- Trade-offs: Relies on prompt quality and model judgment.

### D73. Auto-populated TTS phrase frequency store
- Decision: Persist phrase frequencies and prewarm recurring phrases automatically.
- Context: Manual seed maintenance did not track real usage patterns.
- Alternatives considered: Static manual list only.
- Trade-offs: Adds lightweight file I/O and ranking policy.

### D74. Remove resume banner and storage path
- Decision: Remove D65 flow; refresh now means fresh UI state.
- Context: Resume behavior caused more confusion than value in current product flow.
- Alternatives considered: Keep banner with more complex merge UX.
- Trade-offs: Refresh loses visible transcript state.

### D75. Time-of-day awareness in greeting and prompt
- Decision: Inject local-time bucket context and choose time-aware welcome variants.
- Context: Reliable low-risk personalization easter egg.
- Alternatives considered: Server-time only.
- Trade-offs: Minor token overhead and timezone plumbing.

### D76. Post-audit cleanup bundle
- Decision: Ship targeted hot-path, reliability, and dependency-bound fixes from audit.
- Context: Pre-eval quality sweep identified several high-value low-risk corrections.
- Alternatives considered: Defer all cleanup.
- Trade-offs: Small code surface increase, fewer latent failure modes.

### D77. Audio-reactive hero glow
- Decision: Drive hero glow intensity from output audio analyzer.
- Context: Fixed metronome-like visual pulse disconnected from real voice output.
- Alternatives considered: Static or timer-only glow animation.
- Trade-offs: Continuous animation loop cost, better presence cue.

### D78. Cross-session UserContext persistence
- Decision: Persist bounded user facts per resident ID to local JSON; keep turn history ephemeral.
- Context: Needed cross-session familiarity without long-context token bloat.
- Alternatives considered: Full transcript persistence, cloud sync.
- Trade-offs: Plaintext local facts and bounded memory scope by design.

---

## Phases K-U: Live-session Follow-ups (D79-D99)

### D79. Browser PTZ attempt retired; out-of-frame welfare check shipped
- Decision: MediaCapture-PTZ pan/tilt path was removed for MeetUp; fallback welfare check retained.
- Context: MeetUp exposed zoom but not pan/tilt in browser-accessible capabilities.
- Alternatives considered: Keep unreliable browser PTZ path.
- Trade-offs: No browser pan/tilt tracking on MeetUp; robust camera-agnostic welfare behavior.

### D80. MeetUp-tuned barge-in constants
- Decision: Tune to 150 ms sustained speech and 4 loud windows for hardware AEC setup.
- Context: MeetUp echo suppression allowed faster interrupt thresholds.
- Alternatives considered: Keep laptop-safe 400/6 defaults globally.
- Trade-offs: Deployments without hardware AEC should revert to conservative values.

### D81. Personalized name-aware welcome variant
- Decision: Select name-aware greeting when memory already has user name.
- Context: Better continuity on returning sessions.
- Alternatives considered: Generic greeting always.
- Trade-offs: Extra prewarm variant handling.

### D82. Native Python deployment replaces Docker
- Decision: Drop Docker runtime and ship venv bootstrap from start.bat/start.sh.
- Context: Windows Docker path blocked direct camera/PTZ needs and hurt first-run goals.
- Alternatives considered: Keep Docker with host-device workarounds.
- Trade-offs: Environment variability across host Python installs.

### D83. Claude model upgrade to Sonnet 4.6
- Decision: Move conversation model ID to newer Sonnet 4.6.
- Context: Better TTFT/latency profile with low migration risk.
- Alternatives considered: Stay pinned on older model.
- Trade-offs: Need ongoing compatibility checks with vendor lifecycle.

### D84. On-request optical zoom markers
- Decision: Use inline CAM markers for zoom in/out/reset commands.
- Context: Gives user-visible camera control even when pan/tilt is unavailable.
- Alternatives considered: Free-text parser without explicit marker channel.
- Trade-offs: Prompt discipline required so markers are emitted only when appropriate.

### D85. Clarify turn-latency metric mismatch
- Decision: Document that whole-turn latency metrics do not represent TTFA SLA.
- Context: Evaluation needed accurate first-audio metric interpretation.
- Alternatives considered: Rename existing metric only.
- Trade-offs: Required additional instrumentation work in D86.

### D86. Add TTFA plus per-stage latency percentiles and smoke test
- Decision: Record and emit TTFA, STT, Claude TTFT, and TTS first-byte percentile metrics.
- Context: Needed actionable latency attribution, not one aggregate number.
- Alternatives considered: Keep only turn-level latency.
- Trade-offs: More metrics plumbing and logging.

### D87. Vision model bump to GPT-4.1-mini
- Decision: Switch vision inference model from gpt-4o-mini to gpt-4.1-mini.
- Context: Better cost/latency profile while preserving quality targets.
- Alternatives considered: Stay on older vision model.
- Trade-offs: Ongoing quality watch in activity/bbox edge cases.

### D88. PTZ retune plus capability injection into system prompt
- Decision: Retune control gains and inject per-session camera capability notes into assistant prompt.
- Context: Prevent assistant from claiming unsupported camera actions.
- Alternatives considered: Static capability assumptions.
- Trade-offs: Slight prompt growth, more truthful camera behavior.

### D89. Audio-event classifier scaffold
- Decision: Add end-to-end plumbing for non-speech audio events with stub output path.
- Context: Prepare pipeline before dropping in real classifier.
- Alternatives considered: Wait for full classifier before any wiring.
- Trade-offs: Temporary no-op stage until D90 shipped.

### D90. YAMNet integration for curated non-speech events
- Decision: Integrate YAMNet TFLite locally and expose filtered health-relevant tags.
- Context: Add lightweight acoustic awareness without cloud dependency.
- Alternatives considered: Full 521-class exposure, server-side audio model API.
- Trade-offs: Extra CPU/inference cost and model asset management.

### D91. Whisper multi-language auto-detect
- Decision: Remove forced English language parameter.
- Context: Expand accessibility for non-English users.
- Alternatives considered: Keep English-only bias.
- Trade-offs: Slightly broader STT variance; more inclusive behavior.

### D92. Browser MediaPipe pose landmarks for smoother tracking
- Decision: Run pose landmarking client-side and stream face/shoulder bbox guidance.
- Context: Improve tracking cadence and PTZ smoothness beyond vision-call frequency.
- Alternatives considered: Vision-model-only bbox updates.
- Trade-offs: CDN/WASM dependency and extra frontend compute path.

### D93. Pose-based fall heuristic added as second signal
- Decision: Add nose-vs-hip sustained heuristic feeding existing fall alert path.
- Context: Wanted faster local fall cueing independent of cloud vision cadence.
- Alternatives considered: Keep only model FALL signal.
- Trade-offs: Later produced false positives and was disabled in D95.

### D94. Live-session fix bundle #1 (abide-585f1dec5ee2)
- Decision: Tighten pose-fall thresholds, narrow zoom trigger language, cap user zoom, expand history to 60.
- Context: Real session logs showed false falls, wrong zoom triggers, over-zoom, cache misses.
- Alternatives considered: Single-point fixes only.
- Trade-offs: Larger context window/token spend for cache stability benefits.

### D95. Live-session fix bundle #2 (abide-621b915bf3e3)
- Decision: Disable pose-fall heuristic, improve fall-banner contrast, restrict pose bbox to face/shoulders, enforce user-perspective left/right phrasing.
- Context: Bent-over desk posture still caused false falls; tracking followed hands; left/right language confusion.
- Alternatives considered: Further retune pose-fall only.
- Trade-offs: Fall relies on vision FALL path only; fewer false alarms.

### D96. Live-session fix bundle #3 (latency plus noise floor)
- Decision: Parallelize audio-event classification with STT, add timing anchors, rewrite noteworthy criteria, add mixed-script filter, enforce vision timeout, tighten Claude max tokens.
- Context: Session feedback highlighted latency and over-reactive behavior.
- Alternatives considered: Isolated single tweaks.
- Trade-offs: More coordination logic, better controllability.

### D97. Live-session fix bundle #4 (stall resilience)
- Decision: Add first-token and first-byte deadlines, stall logs, ConversationError pass-through, reduce YAMNet window count, add still-thinking status escalation.
- Context: Upstream stalls looked like assistant death to users.
- Alternatives considered: Passive retries without explicit deadlines.
- Trade-offs: Timeout skips in edge cases, far clearer failure behavior.

### D98. YAMNet prewarm on connect
- Decision: Pre-load classifier/interpreter during connect prewarm fan-out.
- Context: First-turn TTFA penalty came from lazy model init.
- Alternatives considered: Keep lazy load.
- Trade-offs: Slight connect-time background work, faster first interaction.

### D99. Pre-demo security and robustness sweep
- Decision: Ship final bundle including vision escape symmetry, localhost bind, richer shutdown error typing, and callback coverage on background tasks/futures.
- Context: Reduce silent failures and network exposure before demo.
- Alternatives considered: Defer to post-demo cleanup.
- Trade-offs: Small code complexity increase, materially better diagnosability and safety.

---

## Phase V: Zero-Config Python Auto-Install

### D100. Launcher auto-installs Python 3.12 when missing
- Decision: start.bat silent-installs the official python.org installer per-user when Python 3.12+ is not on the machine; start.command/start.sh on macOS downloads the official .pkg and opens the Apple Installer GUI; Linux falls back to a one-line distro-specific install message since every silent path needs sudo.
- Context: Previous launcher told users to install Python manually with "Add python.exe to PATH" ticked, which is Settings-style configuration explicitly forbidden by the brief.
- Alternatives considered: winget (unreliable on pre-1809 Windows 10), PyInstaller single-exe (1-1.5 GB + SmartScreen trips on unsigned builds), embeddable Python zip (strips ensurepip, breaks torch/silero-vad), detect-and-instruct (status quo ante, violates brief).
- Trade-offs: Requires internet on first run; adds ~30-60 s of cold-start on Windows when Python is missing; macOS install is GUI not silent since Apple provides no sudo-free system-install path; clean "install manually" fallback message on every download/install failure.

### D102. PTZ tilt wide dead-zone to prevent standing-user runaway

- Decision: Added a separate `_TILT_DEAD_ZONE = 0.35` for the y-axis in `app/ptz.py`, leaving `_DEAD_ZONE = 0.15` for pan only.
- Context: Live session log analysis showed that when the user stands up, MediaPipe places the face+shoulders centroid at y approx 0.13-0.22 (top of frame), giving oy approx -0.35. With the shared 0.15 dead zone, tilt fired every nudge and maxed out at +15 within ~1 s, then stayed pinned. The camera appeared to "keep moving" throughout the standing portion of the session.
- Root cause: `nudge_to_bbox()` used the same `_DEAD_ZONE` for both axes. The standing-user centroid offset of ~-0.35 greatly exceeds 0.15, so tilt drove to its rail immediately.
- Fix: `if abs(oy) >= _TILT_DEAD_ZONE` in `nudge_to_bbox()`. At 0.35, tilt only fires when the subject is truly at the extreme edge of frame, not just standing normally. Pan still uses 0.15 for responsive left/right tracking.
- Trade-offs: Tilt tracking is now essentially dormant for most seated and standing positions. That is intentional for this narrow-range hardware (MeetUp tilt range is only +-15 total). Wider-range PTZ cameras could use a smaller _TILT_DEAD_ZONE.

### D101. macOS start.command launcher plus Gatekeeper documentation
- Decision: Ship start.command alongside start.sh as a Finder-double-clickable macOS sibling with identical contents; add .gitattributes forcing LF endings on shell scripts so Windows-committed scripts still run on Mac/Linux; document the one-time Gatekeeper right-click-Open bypass rather than Apple-sign the binaries.
- Context: macOS Finder opens .sh files in TextEdit on double-click, and every downloaded file gets Gatekeeper-quarantined regardless of format (.app/.command/.pkg/.dmg all blocked) unless Apple-Developer-signed and notarized.
- Alternatives considered: py2app bundle (same Gatekeeper block, no advantage), ship only .command (breaks Linux convention), xattr -d com.apple.quarantine in launcher (requires terminal, violates brief), $99/year Apple Developer ID signing (rejected for eval-phase build).
- Trade-offs: One-time right-click-Open on first macOS launch; two launcher files instead of one (small duplication, zero user confusion); .gitattributes mandated to prevent Windows CRLF from breaking bash on Mac/Linux.

---

## Known Limitations

- Barge-in latency depends on deployment audio path and AEC quality.
- Vision bounding boxes are approximate guidance, not surgical localization.
- Fall detection is best-effort and does not trigger emergency dispatch.
- Conversation history is bounded for latency/cost stability.
- Cross-session persistence stores only bounded UserContext facts, not full conversation history.
- Direct httpx path is intentionally lower-level than SDK wrappers.

## Notable Capabilities

- Local silero VAD on the hot path.
- Sentence-boundary parallel TTS pipeline.
- Proactive check-ins plus vision-reactive behavior with safety gating.
- Cross-session user facts with forget-me wipe flow.
- Diary view plus export plus session summary analysis.
- Prompt-injection-safe camera context wrapping.
- PTZ capability-aware behavior and on-request zoom control.
- Stage-level latency observability (TTFA/STT/TTFT/TTS first-byte).

## Living Document Rule

When behavior changes materially, add a new Dxx decision entry with concrete context and trade-offs. Keep this file aligned with shipped behavior and CLAUDE.md.
