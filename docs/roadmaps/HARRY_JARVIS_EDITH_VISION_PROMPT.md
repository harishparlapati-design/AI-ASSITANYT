# HARRY — JARVIS-STYLE HOME, CONVERSATIONAL SELF-DEVELOPMENT & EDITH ADMIN CONTROL
## Implementation master prompt for Freebuff AI
**Prompt ID:** 58364  
**Target repository:** `harishparlapati-design/AI-ASSITANYT`  
**Target branch:** `dev` only  
**Reference UI:** the user-provided image showing a compact pill-shaped light/dark toggle (sun on light side, moon on dark side).

---

## 1. Mission

Upgrade the existing Harry product into a coherent, polished, conversational personal AI assistant inspired by the *capabilities and interaction philosophy* of fictional JARVIS/EDITH—not a copy of their branding, dialogue, or fictional claims.

The user wants Harry to feel like one intelligent assistant, not a collection of disconnected admin pages and tools. The primary experience is the **home/chat screen**. The user should be able to talk naturally, ask for work, upload material, share a screen, request new capabilities, and watch Harry plan, work, test, and report progress without leaving the conversation.

The implementation must extend the existing repository and reuse its real services. Inspect the current code before changing anything. Do not rebuild existing systems, invent integrations, display fake telemetry, or mark planned capabilities as active.

Deliver a working implementation in small, testable vertical slices. Start with a design/architecture inventory and a prioritized plan, then implement the highest-value slices in this same task. Do not stop after writing a plan.

## 2. Main home screen — required layout

Create a responsive, application-quality home screen as the default route after authentication.

### A. Overall layout
- Full-height application shell.
- Left navigation/sidebar, central conversation/home canvas, optional right preview/context panel.
- On narrow screens, collapse the sidebar and preview into accessible drawers; the chat remains usable.
- Keep the existing Harry identity and functionality. Do not introduce fake AETHER branding or a separate product identity.
- The first screen should feel calm, premium, intelligent, and uncluttered—not a dense admin dashboard.

### B. Left sidebar
At the top:
1. Harry brand/name and a compact new-chat action.
2. A prominent **Emergency Stop** control, visually unmistakable and keyboard accessible.
   - It must call a real backend stop/cancel endpoint.
   - Stop active generation, speech playback, cancellable jobs, and computer/screen actions where supported.
   - Show what was stopped and what could not be stopped.
   - It must not delete data or shut down the host.
   - Require a deliberate confirmation for any broader shutdown operation; emergency stop itself should be immediate.
3. Search conversations.
4. Conversation history grouped by date, with rename, archive, and delete actions, confirmation, ownership checks, and persistence.
5. Clear indication of active conversation.

At the bottom:
- A circular account/profile button with avatar/initials and account name.
- Clicking opens a compact account menu or settings drawer containing:
  - Profile and preferences
  - Security and MFA
  - Admin / EDITH Control Center (admin role only)
  - Plugins / integrations
  - Skills / capabilities
  - Agents
  - Models
  - Memory and personalization
  - Automations and scheduled tasks
  - Files and generated documents
  - Diagnostics and system health
  - GitHub / self-development history
  - Notifications
  - About, help, sign out
- Hide admin-only navigation from non-admin users and enforce the same restriction server-side. Hiding UI is not authorization.

### C. Theme toggle — use uploaded image as visual reference
- Put the toggle in the top-right of the main application header.
- Reproduce the compact pill concept: sun icon for light mode, moon icon for dark mode, clear selected-state indicator, smooth but restrained transition.
- Support Light, Dark, and System preferences.
- Persist the choice per account and restore it on every device/session.
- Respect OS preference in System mode and respond to OS theme changes.
- Meet WCAG contrast and focus requirements; provide accessible labels and keyboard operation.
- Do not use the uploaded screenshot as a background; implement the control with HTML/CSS/SVG/icon assets.

### D. Central home canvas and Harry robot face
- Center a distinctive Harry robot face on the empty/new-chat home screen. It must visibly include **two expressive eyes, a mouth, and two side ears**. It can be a clean CSS/SVG illustration or a locally stored original asset; do not rely on an emoji glyph as the only implementation.
- The face is a living status indicator, not a decorative fake animation:
  - Idle: relaxed eyes and neutral/small smile.
  - Listening: ears/eye ring or subtle pulse; only when microphone/listening state is genuinely active.
  - Thinking: subtle eye movement or progress animation tied to a real active request.
  - Speaking: mouth animation synchronized to actual TTS playback state, not merely a timer.
  - Working: focused expression plus task progress.
  - Paused/muted/error/offline: clear accessible state and non-color-only indication.
- Respect reduced-motion preferences. No continuous distracting animation.
- Show a concise welcome line and a few useful suggested actions based on capabilities that are actually enabled.
- When a conversation begins, the face can compact into a small persistent conversation status/avatar; it must not obstruct messages.

### E. Conversation composer
A bottom-anchored, expandable composer in the central column:
- Left **+** button opens an attachment/action menu.
- Text input supports multiline, Enter-to-send, Shift+Enter newline, clear focus state, and mobile layout.
- Right-side **microphone** button for one-to-one live voice conversation (push-to-talk and continuous mode only when enabled/consented).
- Send/stop button changes according to request state.
- Display attachment chips with remove, upload progress, file type, and error state.
- Show recording/listening state visibly and provide an immediate stop/mute control.
- Do not start microphone, camera, screen capture, or recording without a clear user gesture and browser permission.

The + menu should expose real, capability-gated actions:
1. Upload files (multiple files; drag-and-drop; progress; size/type validation).
2. Share screen (browser getDisplayMedia permission; explicit start/stop; selected tab/window/screen where browser supports it).
3. Attach image.
4. Attach document.
5. Camera capture only if implemented and permission granted.
6. Create document / export.
7. Start a task / automation only if the corresponding service is available.
8. Other enabled tools/plugins.
Unavailable features must be clearly labelled “Not available yet” or omitted—not clickable fake controls.

### F. Right-side preview/context panel
- Collapsible panel on desktop; drawer on mobile.
- Context-sensitive content:
  - File preview (safe text, image, PDF/document metadata where supported).
  - Screen-share preview and sharing indicator.
  - Generated document/image/media preview.
  - Current task plan, step progress, tool activity and verification status.
  - Relevant citations/research sources.
  - Optional code diff preview during an approved self-development task.
- The panel must be driven by actual conversation/task events. Do not show demo data.
- Provide explicit stop-sharing, close-preview, download, and open-in-new-tab actions where applicable.
- Enforce tenant ownership and content access checks for every preview/download.

## 3. One conversation, all capabilities

Harry should feel like a single assistant in the conversation. Do not force the user to navigate to an admin page or manually invoke a separate “upgrade wizard” to request a capability improvement.

Examples of desired natural interactions:
- “Harry, add a feature that lets you summarize PDFs.”
- “You are slow when answering. Investigate and improve response speed.”
- “Build a screen-sharing feature so I can show you my desktop.”
- “Improve your voice interruption handling.”
- “Find why this tool fails and fix it.”
- “I want a dashboard showing the progress of your development.”

### Required conversational workflow
When a user request is a **feature-development or self-improvement request**:
1. Detect that it is a development request, distinct from ordinary conversation or normal task execution.
2. Explain briefly what Harry understood and identify the relevant subsystem.
3. Inspect the current implementation, tests, dependencies, and repository state.
4. Research documentation or alternatives when needed, using approved research tools.
5. Produce a concise implementation plan with acceptance criteria, risks, permissions, estimated effort, and any potential paid-service cost.
6. Ask the administrator for approval before protected/high-impact actions, external spending, new credentials, production changes, security-sensitive changes, or destructive data migrations.
7. Create an isolated working branch/worktree or staging area. Never edit the running production code in place.
8. Implement the change using the existing architecture and coding standards.
9. Run focused tests, regression tests, security checks, static analysis, and relevant simulations.
10. Review the diff for secrets, privacy issues, unsafe execution, tenant isolation, and unintended changes.
11. Present results in the same conversation: what changed, files/modules, tests and exact outcomes, limitations, screenshots/preview if available, and rollback point.
12. Activate only after all required checks pass and required approval is present. Restart/reload only through a controlled service manager and only if authorized.
13. Commit and push successful changes to `dev` only, following the repository's existing safe Git workflow. Never push to `main`, force-push, merge to main, or deploy without explicit user approval.
14. If checks fail, keep the change isolated, report the failure, attempt bounded repairs, and do not activate or push failing code.

For low-risk, reversible, local development tasks, the administrator may configure pre-approval policies. Defaults must be conservative. The user must be able to inspect, pause, cancel, approve, reject, and roll back every development run.

**Important distinction:** A normal user asking Harry to “make a chart” or “summarize this file” is asking Harry to use existing capabilities, not rewrite source code. Only explicit or clearly implied requests to build, add, fix, upgrade, improve, or develop Harry itself should enter the self-development workflow. If ambiguous, ask one short clarifying question.

Do not promise that Harry can modify its own model weights, bypass platform limitations, or autonomously grant itself new permissions. Self-development means controlled source-code changes, tools, prompts/configuration, tests, and integrations under administrator policy.

## 4. JARVIS-style intelligence and task execution

Strengthen the existing plan → execute → observe → verify → recover/replan loop.

For non-trivial tasks, Harry should:
- Clarify the desired outcome and constraints only when necessary.
- Break the task into ordered steps and identify dependencies.
- Select tools/agents based on verified capabilities.
- Execute steps with bounded timeouts, budgets, retries, and cancellation.
- Observe actual tool outputs and environment state.
- Verify the result against acceptance criteria, not merely whether a command returned exit code 0.
- Recover from transient failures, try a safe alternative, or ask for help.
- Keep the user informed with concise progress events in the same conversation.
- Produce a final result with evidence, completed/failed steps, and next actions.

Do not claim “thinking” means human consciousness. Implement transparent reasoning summaries, plans, evidence, and decision records—not hidden chain-of-thought exposure. Show concise explanations and verifiable work, not private internal reasoning.

## 5. EDITH — administrator oversight and owner experience

Implement **EDITH** as the administrator oversight/control persona or interface layer (not a second unrestricted assistant). Only the verified administrator can access privileged EDITH controls.

EDITH should help the owner:
- See what Harry is doing now and why.
- Inspect task plans, tool calls, agent assignments, progress, outputs, errors, and approvals.
- Review proposed source changes, diffs, tests, simulations, security review, and rollback checkpoints.
- Approve/reject sensitive actions and role/permission changes.
- Pause/resume/cancel tasks and self-development runs.
- View audit history, model/tool configuration, health, performance, and usage.
- Understand whether a task is complete, partially complete, blocked, or failed.
- Review a “what improved” report after each self-upgrade.

Use clear status labels: Proposed, Awaiting approval, Running, Testing, Security review, Ready to activate, Active, Rolled back, Failed, Cancelled. Every status must reflect persisted backend state.

The owner should receive a concise completion summary such as:
- Requested outcome
- What changed
- Number and names of tests/simulations executed
- Pass/fail counts and any skipped tests
- Security checks performed
- Performance before/after when measured
- Known limitations
- Commit SHA and branch
- Rollback/checkpoint identifier

Do not fabricate happiness scores, success percentages, simulation counts, or test results. “Owner satisfaction” can be collected as explicit feedback (helpful/not helpful or rating) and used as a product-quality signal, not silently inferred or manipulated.

## 6. Multi-agent team

Use the existing agent registry/orchestrator where possible. Provide specialized roles:
- Research
- Reasoning / task analysis
- Architecture
- Planning
- Programming
- Debugging
- Testing / QA
- Security review
- Performance optimization
- Vision / OCR
- Document processing
- Automation
- Verification
- Release / Git operations

Requirements:
- Agents are bounded workers with explicit inputs, outputs, capabilities, timeouts, and budgets.
- Only the orchestrator can assign work; agents cannot grant themselves permissions.
- Parallelize independent research/test/review tasks where safe.
- Require independent verification for high-impact code/security changes.
- Avoid duplicate work and circular delegation.
- Persist run logs and make them visible in EDITH.
- Do not imply agents are separate conscious beings.

## 7. Plugin and skill center — available in chat

- Integrate existing plugins and skills into the conversation's tool/capability registry.
- In the composer, allow users to see which tools are available and which are active for the current task.
- Use capability discovery and tool selection automatically when a request clearly needs a connected tool.
- Make plugin/skill status visible: Connected, Available, Needs permission, Disabled, Error, Unsupported.
- Explain when a plugin requires connection, OAuth, a permission grant, or configuration.
- User must explicitly approve permission scopes and external account actions.
- Keep plugin manifests signed/hashed and validated using the existing security architecture.
- Skills are instructions/workflows, not arbitrary executable code. Do not execute skill text as code.
- Never claim a plugin is active unless it is installed/connected and a real health/action check succeeds.
- Admin can enable/disable integrations and review permissions; ordinary users can only use permitted capabilities.

## 8. Screen sharing and multimodal interaction

Implement or complete real browser screen sharing:
- User-initiated `getDisplayMedia` only.
- Show browser-provided sharing indicator and Harry's own clear active-sharing banner.
- Stream frames at a bounded rate/resolution; do not continuously upload full-resolution video by default.
- Allow pause, resume, stop, and immediate stop on browser track-ended event.
- Transmit only to the authenticated session and authorized vision service.
- Explain what is being shared and whether frames are retained. Default to no persistent retention unless user explicitly saves.
- Do not capture hidden tabs, other windows, or screens beyond the browser permission.
- Add OCR/vision interpretation with citations to visible evidence where possible.
- Voice and screen context should work in the same conversation, with clear controls.

Complete image understanding, document understanding, speech, and image generation integrations only where real providers/services exist. Video generation is a separate capability and must not be faked with a slideshow. If a capability is not yet implemented, show it as upcoming.

## 9. Conversation and task persistence

- Persist conversation history, titles, timestamps, attachments metadata, and task links securely per user/tenant.
- Provide search, rename, archive, export, and delete.
- Use safe pagination and retention policies.
- Support long-running tasks without blocking chat.
- Stream progress and final responses without duplicate messages.
- Support cancellation and reconnect/resume where technically safe.
- Prevent one user from seeing another user's conversations, files, previews, task runs, or memory.

## 10. Security requirements — non-negotiable

Treat security as a core feature, not a visual badge.
- Preserve existing role checks, MFA, step-up elevation, session revocation, CSRF protection, origin/host validation, bootstrap protections, approval workflow, and audit logging.
- Revalidate authorization on every privileged backend request; never trust UI visibility or client-supplied role flags.
- Immediately revoke elevated sessions on demotion/deactivation/deletion.
- Enforce per-user/tenant access on chat history, files, screen frames, jobs, memory, previews, and generated outputs.
- Store credentials in OS/cloud secret stores or encrypted server-side storage; never in Git, frontend bundles, logs, prompts, or audit events.
- Apply strict upload limits, MIME sniffing, extension allowlists, malware scanning where available, safe archive handling, and path traversal protection.
- Treat uploaded documents, websites, code, plugin output, and screen text as untrusted input. Defend against prompt injection and data exfiltration.
- Use least privilege for tools and agents. No unrestricted shell, arbitrary Python execution, or direct production database access from user prompts.
- Self-development must run in isolated staging with resource/time limits and an allowlisted workspace.
- Require approvals for security policy changes, authentication changes, role changes, new external permissions, destructive actions, production deployment, paid services, and data migrations.
- Provide immutable/auditable event records for privileged actions.
- Add abuse/rate limits and safe error redaction.
- Do not let Harry/EDITH change its own security policy, remove audit records, create a new admin, or bypass MFA.

## 11. Performance and UX

- Keep the ordinary chat path fast and avoid unnecessary planning/model calls for simple questions.
- Stream partial answers and progress.
- Lazy-load heavy preview/admin panels.
- Show measured latency and model/provider health without fake values.
- Avoid loading large models on every request.
- Keep UI responsive while long jobs run.
- Respect accessibility, keyboard navigation, screen readers, reduced motion, focus trapping, and mobile touch targets.

## 12. Tests and acceptance criteria

Add or extend automated tests for:
1. Home screen route, responsive layout, sidebar and history.
2. Theme light/dark/system persistence, including OS preference.
3. Robot face states driven by real listening/thinking/speaking/working/muted/error events.
4. Composer send, multiline, attachment menu, upload validation/progress.
5. Emergency stop actually cancels active generation/speech/jobs and is authorization-safe.
6. Conversation history CRUD, search, pagination, ownership isolation.
7. Profile menu and admin-only EDITH visibility plus backend authorization.
8. Right preview panel and tenant-safe file/task preview.
9. Plugin/skill discovery and real status, permission prompts, error states.
10. Screen-share permission, stop, browser track-ended, frame limits, no-retention default.
11. Development-intent detection vs normal task intent.
12. Self-development plan, approval gates, isolated worktree/staging, tests, security review, activation, rollback, and failure paths.
13. Agent assignment, timeout, budget, logs, cancellation, and independent verification.
14. EDITH approvals, audit records, progress and upgrade reports.
15. CSRF, XSS, IDOR, prompt injection, path traversal, upload abuse, session revocation, role escalation, secrets redaction, rate limits.
16. Regression suites for existing admin/auth/bootstrap/autonomy/dashboard/conversation/voice/docs/media/language/integrations.

Use temporary databases and isolated test directories for destructive tests. Add frontend tests using the project's existing framework; if none exists, choose a lightweight framework compatible with the current stack and document why. Do not rely only on static HTML checks or mocked browser clicks. Include at least one real browser end-to-end run for the main flow if browser automation is available.

Acceptance:
- User can open Harry and see the robot-face home screen, theme toggle, sidebar, composer, and optional preview panel.
- User can start a conversation, attach a file, use voice with permission, and see real task state.
- User can request a Harry feature upgrade in natural language and receive a tracked development workflow in the same conversation.
- No feature is reported as complete unless tests and verification pass.
- Admin can inspect/approve/stop/rollback development from EDITH.
- Security regressions remain green.

## 13. Git workflow

Repository: `https://github.com/harishparlapati-design/AI-ASSITANYT.git`

- Work on `dev` only.
- Inspect current branch, clean/dirty state, recent commits, and remote before starting.
- Preserve user changes. Never reset, overwrite, or discard uncommitted work.
- Implement in vertical slices and run targeted tests after each slice.
- Run full relevant regression suite at milestone boundaries.
- Secret scan and inspect diff before commit.
- Commit and push only slices whose required tests pass.
- Never push to `main`, force-push, rewrite history, merge to main, delete branches, or deploy without explicit approval.
- Never commit voice recordings, voice profiles, credentials, local models, virtual environments, user documents, private data, or ignored files.
- If the worktree is dirty or branch state is unexpected, stop and report before altering it.
- Do not claim a push succeeded without checking the remote branch SHA.

## 14. Paid services and hosting

Do not provision paid APIs, GPUs, hosting, domains, storage, or subscriptions automatically. First provide provider, expected monthly/usage cost, alternatives, data handling, and ask for explicit approval. Local/offline options may be implemented where appropriate. No production deployment without approval.

## 15. Execution order

First inspect existing frontend framework, routes, CSS/theme, conversation/event APIs, jobs, voice, files, plugins/skills, agents, selfdev, admin/EDITH, and tests. Map existing functionality and gaps.

Then execute in this order:
1. Home shell and visual design: sidebar, history, top-right theme toggle, central robot face, composer, right preview panel.
2. Connect existing chat, SSE/events, voice, stop, files and task state to the new UI.
3. Account menu and navigation to existing security/admin/plugins/skills/agents/models/memory/automation/docs/diagnostics/Git features.
4. Unified capability discovery and in-chat plugin/skill activation.
5. Conversational self-development intent and tracked plan/progress in chat.
6. EDITH owner oversight, approvals, simulations/test evidence, rollback and upgrade history.
7. Screen sharing and multimodal improvements.
8. Multi-agent orchestration improvements.
9. Accessibility, mobile, performance and security hardening.

Do not attempt a risky all-at-once rewrite. Keep the product runnable after every slice. At the end, report implemented, partial, blocked, and not implemented items separately, with test commands/results and commit SHAs.

## 16. Required final report

Return:
- Architecture inventory and reuse map.
- Screenshots or browser evidence for desktop and mobile.
- List of changed files.
- Exact test commands and pass/fail counts.
- Security checks and any limitations.
- Feature status table: Complete / Partial / Blocked / Not started.
- Performance measurements where applicable.
- Commit SHA(s), push confirmation and remote `dev` SHA.
- What the owner must configure or approve next.
- Explicitly list any feature not implemented. Never substitute a mockup for a working feature.

**Start now by inspecting the current codebase and then implement the first vertical slice. Do not ask the user to manually repeat the development request in a separate admin screen.**


---

## 17. Core intelligence implementation — build Harry's actual brain

The user explicitly requests implementation of Harry's core intelligence, not only UI or prompt changes. Inspect the existing `brain.py`, `llm.py`, `task_engine.py`, `agents.py`, `memory.py`, `tools.py`, `builtin_tools.py`, `conversation.py`, `language_policy.py`, `selfdev.py`, and related modules. Preserve working behaviour and refactor incrementally. Do not create a second competing brain.

### 17.1 Required architecture

Build or complete a clearly separated, testable pipeline:

1. **Input normalization:** accept text, speech transcript, attachments, screen observations and structured events; preserve source, timestamps, permissions and conversation ID.
2. **Context assembly:** gather only authorized conversation history, relevant memories, user preferences, current task state, tool results and attachment summaries. Apply token/context budgets and relevance ranking.
3. **Intent and risk classification:** distinguish conversation, question answering, tool use, multi-step task, development request, admin action, emergency stop and unsafe/unsupported request. Use deterministic checks for security-sensitive intent and model assistance for ambiguous natural language.
4. **Model routing:** select configured model/provider by task type (fast chat, reasoning, coding, vision, speech/transcription) based on actual availability, measured health, latency, privacy and configured budget. Have a deterministic fallback when a provider fails. Never silently call a paid provider.
5. **Planning:** for complex tasks create a structured plan with goal, assumptions, steps, dependencies, required capabilities, risk level, budget, timeouts and acceptance criteria. Simple conversation should bypass expensive planning.
6. **Permission and policy gate:** authorize every tool/action on the server using account, role, elevation, scope, tenant, user consent and risk. Model output is never an authorization decision.
7. **Tool/agent execution:** invoke registered typed tools only; validate inputs and outputs; apply timeouts, rate limits, cancellation, resource budgets and idempotency keys. Agents cannot grant permissions or call unregistered tools.
8. **Observation:** capture real tool outputs, task status, exceptions and environment evidence. Treat tool output and external content as untrusted data.
9. **Verification:** evaluate each acceptance criterion against observed results. Do not mark a task successful merely because a tool returned exit code zero.
10. **Recovery/replanning:** retry only when safe and bounded; use alternative tools when authorized; otherwise stop and explain the blocker.
11. **Response synthesis:** produce a natural concise answer grounded in observations, cite research sources where applicable, distinguish facts from uncertainty, and state what was/wasn't done.
12. **Memory update:** extract only useful, appropriate, user-authorized durable preferences/facts; avoid sensitive data by default; support inspect/edit/delete/export/retention controls.
13. **Telemetry and evaluation:** record latency by stage, model/provider, token counts when available, tool duration, retries, verification outcome and user feedback. Redact secrets and personal content from operational logs.

### 17.2 Core contracts to implement in the existing architecture

Use equivalent typed contracts compatible with the project's current language/framework; adapt names to existing modules instead of duplicating them. The following is a behavioural reference, not a requirement to create these exact files:

```python
from dataclasses import dataclass, field
from typing import Any, Literal

Intent = Literal[
    "chat", "answer", "tool_task", "multi_step_task",
    "self_development", "admin_action", "emergency_stop",
    "unsupported_or_unsafe"
]

@dataclass
class TurnInput:
    user_id: str
    conversation_id: str
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    modality: Literal["text", "voice", "image", "screen", "mixed"] = "text"

@dataclass
class TaskStep:
    id: str
    description: str
    capability: str | None = None
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    result_summary: str | None = None

@dataclass
class TaskPlan:
    goal: str
    steps: list[TaskStep]
    acceptance_criteria: list[str]
    risk_level: str
    requires_approval: bool
    budget_seconds: int
    status: str = "proposed"

@dataclass
class TurnResult:
    response: str
    intent: Intent
    task_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

class HarryOrchestrator:
    def handle_turn(self, turn: TurnInput, principal) -> TurnResult:
        # Normalize -> authorize -> context -> classify -> route.
        # Plan only when task complexity requires it.
        # Gate every action -> execute -> observe -> verify -> recover.
        # Synthesize response -> persist authorized memory/events.
        raise NotImplementedError
```

Do not leave production methods as `NotImplementedError`; this contract illustrates the separation of responsibilities. Implement concrete services and dependency injection using the project's established patterns.

### 17.3 Model and knowledge behaviour

- Harry's intelligence comes from the configured language/vision/speech models plus orchestration, context, tools, memory, retrieval, policies and evaluation—not from copying another provider's private implementation.
- Support local and remote model adapters behind a common interface. Keep provider-specific code isolated.
- Support model roles: fast conversation, deep reasoning, coding, vision, transcription and speech synthesis.
- Include provider health checks, timeout/retry policy, context limits, streaming, usage/latency tracking and graceful fallback.
- Retrieval must identify source and freshness; distinguish retrieved evidence from model-generated inference.
- Do not claim a model has read files, searched the web, used a plugin or executed a tool unless that action actually occurred.
- Build an evaluation set from real, privacy-safe project test cases: instruction following, tool selection, factual grounding, refusal boundaries, multilingual understanding, task completion, memory relevance, latency and regression.
- Compare candidate model/config changes against the baseline before activation. Keep the baseline and rollback option.

## 18. Human-aware communication, empathy and helpfulness

The user wants Harry to communicate with warmth, patience and emotional awareness. Implement **human-aware interaction**, not a false claim that Harry is human or has feelings.

Required behaviour:
- Detect conversational cues such as frustration, confusion, urgency, sadness or excitement cautiously; treat them as uncertain signals, never diagnoses or facts about the user.
- Respond with appropriate tone: patient and clear when confused, calm and supportive when distressed, concise during urgency, celebratory without exaggeration when a task succeeds.
- Acknowledge the user's concern before jumping into technical steps when emotional context is apparent.
- Ask one focused clarification only when genuinely necessary; do not repeatedly ask for information already present in the conversation.
- Adapt explanation depth to the user's apparent familiarity; explain technical terms plainly when helpful.
- Remember explicit communication preferences only with appropriate memory controls.
- Be honest about limitations, uncertainty, mistakes and failed operations. Apologize briefly when Harry causes an issue, then explain the correction.
- Do not manipulate, flatter excessively, claim love/loyalty/consciousness, pretend to experience emotions, or encourage emotional dependency.
- Do not infer sensitive traits or store inferred emotional states as durable memory.
- Provide user-controlled language and tone preferences, including multilingual conversation where supported.
- Keep warmth compatible with safety: never agree to unsafe requests merely to appear supportive.

Add tests for empathetic but non-deceptive responses, frustration recovery, uncertainty, crisis/urgent language routing to appropriate safe support, multilingual tone consistency and no sensitive-emotion persistence.

## 19. Additional capabilities to include in the unified remaining-work scope

Inspect the repository and classify every item as already working, partial, missing, blocked or not applicable. Implement missing items where feasible; do not duplicate working modules.

### Conversation and productivity
- Conversation folders, pinning, search, rename, archive, export and delete.
- Conversation branching/forking for exploring alternatives without losing the original.
- Message edit/regenerate, stop generation, retry failed response and copy/share controls.
- Citations/source panel and evidence-aware answers.
- User-controlled memory and per-conversation temporary/private mode.
- Task queue with priorities, deadlines, pause/resume/cancel and notification on completion.
- Calendar/email integrations only through explicitly connected, permission-scoped providers.
- Reminders, recurring workflows and scheduled summaries.
- Search across authorized user files and conversations.

### Multimodal and creative
- Speech-to-text, text-to-speech, continuous conversation and push-to-talk.
- Voice selection and settings, microphone/speaker device selection, audio diagnostics and echo/barge-in handling.
- Image upload, image understanding, OCR and image generation.
- Screen sharing with explicit browser permission and immediate stop.
- Document ingestion, extraction, summarization, comparison and question answering.
- Document generation and preview for PDF, DOCX, XLSX, PPTX, CSV, TXT and Markdown.
- Real video understanding/generation only with an actual supported provider; never fake it with a slideshow.
- Accessible captions/transcripts and user-controlled retention.

### Developer and computer assistance
- Read-only codebase exploration and repository search.
- Explain code, architecture and dependencies.
- Generate patches and review diffs.
- Run approved tests/build/lint commands in isolated workspaces.
- Browser automation and computer-use actions only with user authorization and visible activity.
- Git status, branch, diff, commit and push to dev after tests; approval before external or production-impacting actions.
- Project templates, API integrations and custom plugin creation under the security policy.
- Upgrade changelog, release notes and rollback history.

### Reliability and owner experience
- Command Center and detailed EDITH sections for autonomy, agents, models, voice, memory, plugins, automations, files, diagnostics, security, performance, Git and hosting.
- Real-time task/activity timeline, notification center and error explanations.
- Backups, restore drills and retention policy.
- Health checks, dependency inventory, vulnerability checks and incident log.
- Performance benchmarks by model, route and stage; compare before/after.
- Accessibility audit, responsive layouts, keyboard navigation and reduced-motion support.
- User feedback controls and opt-in product-quality analytics.

## 20. Implementation priority and completion gate

After the existing Command Center, implement the remaining work in this priority:

1. New unified home/chat UI: shell, sidebar/history, emergency stop, theme toggle, robot face, composer and preview panel.
2. Connect the UI to real chat, SSE, voice, attachments, task progress and stop controls.
3. Core intelligence pipeline: context assembly, intent/risk classification, routing, planning, permission gate, tool execution, observation, verification, recovery and response synthesis.
4. Human-aware communication and multilingual consistency.
5. In-chat capability discovery for plugins and skills.
6. Conversational self-development using isolated workspaces and approval gates.
7. EDITH's full management panels and development oversight.
8. Multi-agent orchestration and independent QA/security verification.
9. Screen sharing, vision, document workflows, image generation and any available real video provider.
10. Productivity integrations, automations, notifications and memory controls.
11. Security, reliability, accessibility and performance hardening.
12. Hosted deployment architecture, Android and Windows packaging; obtain approval and cost agreement before paid provisioning or production deployment.

A milestone is complete only when:
- Its backend and frontend paths are genuinely connected.
- Automated tests pass, including authorization and failure cases.
- Relevant browser E2E flows pass.
- Security review and secret scan pass.
- User-visible states represent real backend state.
- Documentation and operator instructions are updated.
- A rollback path exists for changes that alter runtime behaviour.

At each milestone report exact test commands, pass/fail/skip counts, browser evidence, performance baseline and result, limitations, files changed, commit SHA and verified remote dev SHA. Keep a single living feature inventory so no requested feature disappears between milestones.

**Core-code requirement:** implement and test Harry's actual orchestration/intelligence services in this repository. Do not answer this requirement with only a system prompt, UI mockup, architecture diagram or placeholder classes.
