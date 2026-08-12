# Aletheia final-goal gap analysis — 2026-08-12

## Executive verdict

Aletheia is a credible early **evidence-oriented research operating system**. It is not yet a
reliable autonomous frontier scientist. The strongest part is the anti-overclaiming spine:
deterministic stages, harness-owned verdicts, claims/evidence, confirmatory splits, reproduction,
critics, and provenance events. The largest remaining gaps are not additional agent autonomy. They
are protection against adaptive overfitting across a campaign, hard isolation of authored code,
reliable novelty/SOTA grounding, external replication, and production-grade durability.

Claude and GPT are now interchangeable at the orchestrator/worker boundary. That removes a model
lock-in, but it does not prove scientific or operational parity; both providers still need the same
live evaluation matrix.

## High-priority defects fixed in this change

| Defect | Consequence before | Resolution |
|---|---|---|
| Discovery screened on the full dataset | Candidate selection could see rows later described as confirmation data | Discovery now receives only the harness-owned exploration partition and must reuse the exact split hash downstream |
| Literature retrieval error was fail-open | `grounded=None` could still become a survivor | A survivor now requires `grounded is True`; unavailable retrieval is an explicit rejection |
| Reviewer author was hard-coded incorrectly | Pure Grok discovery could be reviewed by Grok, and a GPT-authored demonstration could be audited by OpenAI | Discovery excludes every actual author; demonstration audit dynamically excludes the configured orchestrator vendor and fails closed if it leaks back in |
| Exploratory prefilter conflated signal with support | A candidate promoted on separation could be labelled `holds=True` even when its pre-registration did not fire | `survives` is now explicitly exploratory; `holds` records only the actual pre-registered rule, control, and probe verdict |
| Claude-specific tool/runtime boundary | GPT could not run scoping, workers, literature tools, or demonstrations | Tools now have one provider-neutral contract, adapted to Claude MCP or strict OpenAI Responses functions |

These repairs close real defects. They do not make exploratory discovery itself confirmatory: only
the untouched confirmation partition can do that.

## P0 blockers before unattended real science

### 1. A campaign can adapt to a reused confirmation set

The explore/confirm seal protects one hypothesis from its own exploration. A later campaign round,
however, is deliberately shaped by the previous confirmation result and can split the same finite
dataset again. Reusing or overlapping confirmation rows turns them into adaptive training feedback.
This is the most important remaining epistemic loophole.

Required design:

- Allocate disjoint, immutable confirmation batches per adaptive round, or acquire fresh data.
- Keep a final never-touched external holdout that is opened once, after the campaign stops.
- Record a dataset-level split ledger so no row/group can silently change roles.
- Add sequential-testing/alpha-spending rules and family-level reporting for all tried hypotheses.
- Treat exhaustion of fresh confirmation data as a hard stop, not a reason to reshuffle.

Acceptance: no hypothesis may be authored using any outcome from rows later used to confirm that
hypothesis; the final headline must survive a one-time external holdout or independent dataset.

### 2. AI-authored code still executes on the host by default

`compute_backend=local` is the default. Model code smoke tests and AI-authored exploration/
demonstration code also use host subprocesses even when the Docker training backend exists. AST
filtering and rlimits contain mistakes; they are not a security boundary against adversarial code.

Required design:

- Make the no-network container the default for every authored-code path, including smoke tests,
  exploration, demonstration, training, and reproduction.
- Refuse unattended real runs if a hard sandbox is unavailable; make host execution an explicit
  development-only override.
- Mount only staged inputs, never the repository or secrets unless strictly required; use a minimal,
  digest-pinned image and a seccomp/AppArmor policy where available.
- Add escape, filesystem, network, fork-bomb, resource-exhaustion, and malicious-library tests.

Acceptance: a deliberately hostile authored program cannot read host files, contact a network,
observe credentials, modify the repository, or outlive its job.

### 3. Novelty and SOTA health are still recall-limited

Discovery now fails closed on a search outage, but “three retrieved papers” is not evidence that the
search covered a field. Search uses abstracts and best-effort APIs; it does not yet model query
coverage, terminology drift, retractions, conflicting results, or full-text claims. A confident
panel can therefore approve false novelty based on a shared incomplete briefing.

Required design:

- Build a versioned paper/method/dataset/metric/claim graph with exact source spans.
- Run systematic query expansion, backward/forward citation traversal, deduplication, date/venue
  coverage checks, and explicit stopping criteria.
- Detect retractions/corrections and distinguish peer-reviewed work from preprints.
- Compare SOTA only when dataset version, split, metric definition, preprocessing, and compute regime
  are compatible; otherwise label the comparison non-comparable.
- Calibrate retrieval recall on known-answer review sets before novelty claims can become strong.

Acceptance: novelty means “survived a documented search protocol with measured coverage,” never
“the current retriever did not find it.”

### 4. Independent review is not yet a universal hard gate

Discovery and AI-demonstration audit now exclude their author vendors. Ordinary direction, design,
results, and RAG-faithfulness panels can still include the orchestrator vendor. The system records a
degraded review and caps claim strength when too few vendors survive, but some gates may continue
after only one usable reviewer. Cross-vendor models can also share the same incomplete evidence and
make correlated errors.

Required design:

- Attach explicit authorship/provenance to every artifact and exclude all author vendors from every
  independent gate that judges it.
- Require the configured independent-vendor floor to pass a hard gate, not merely to earn a stronger
  claim.
- Give reviewers frozen evidence packages and separate retrieval, not hidden author reasoning.
- Track unresolved objections by claim/evidence id; a rebuttal cannot erase an objection without new
  evidence or an explicit reviewer withdrawal.
- Add deterministic checks and expert review for claims whose risk exceeds what model critics can
  validate.

Acceptance: no artifact is called independently reviewed if an author judged it or the independent
vendor floor was not met.

### 5. Data and run provenance are not immutable enough

Data assets record locations and profiles but not a mandatory content hash, version, license,
consent classification, or immutable row identity. Provider/model selection is global configuration,
not a per-run frozen manifest. Dependencies and several model names use moving aliases. A resumed
run can therefore execute under a different environment even though worker cache keys prevent one
class of provider/model collision.

Required design:

- Hash raw data, normalized data, split membership, code, prompts, tool schemas, containers,
  dependencies, provider/model snapshot, and harness version into a signed run manifest.
- Refuse mutation of evidence-bearing inputs after pre-registration; changes create a new lineage.
- Record licenses, usage restrictions, PII/sensitivity, and allowed model-provider destinations.
- Pin dependencies and production model snapshots; upgrades occur through replayable evaluations.

Acceptance: a third party can identify the exact bytes, environment, model, protocol, and decisions
behind every claim.

### 6. Execution and schema management are process-local

Runs, sessions, the event fan-out, and compute backend state rely on in-process tasks/dictionaries.
A process restart can orphan work even though some results are in Postgres. Database startup uses
`create_all()` plus ad-hoc `ALTER TABLE` statements rather than versioned migrations. There is no
repository CI workflow enforcing the existing test/build checks.

Required design:

- Move stage execution to a durable queue with leases, heartbeats, idempotency keys, retries, and
  restart recovery; use durable pub/sub for multi-process SSE.
- Make every state transition transactional and replay-safe.
- Introduce reviewed Alembic migrations, backup/restore drills, and schema compatibility tests.
- Add CI for backend tests, lint, frontend build, migration upgrade/downgrade, and sandbox tests.

Acceptance: killing any API/worker process mid-stage neither loses a completed result nor runs an
irreversible step twice.

## Scientific capability gaps (P1)

### Mechanism and causality

Most implemented domains remain prediction/evaluation tasks. Negative controls and ablations are a
good start but do not establish a causal mechanism. The system needs explicit causal assumptions,
interventions/counterfactuals, competing explanations, identifiability checks, and domain-specific
falsification protocols. A correlation or benchmark improvement must not be promoted to a mechanism.

### Statistical decision layer

Add effect-size uncertainty, power analysis, multiple-comparison control, robustness to researcher
degrees of freedom, equivalence/non-inferiority tests, missing-data policy, subgroup stability, and
predefined stopping rules. Reproduction tolerances must be claim- and domain-specific rather than one
global relative tolerance.

### External replication

Seed reruns and locked-code reproduction are valuable but share code, data, and infrastructure.
Strong findings need a different dataset/site, independent implementation, or hidden benchmark; the
system must preserve negative and failed replications in the final bundle.

### Research-program selection

The belief/EIG loop is built and offline-tested, but it has not demonstrated calibrated predictions
over enough live campaigns. EIG priors need empirical calibration, resource-aware portfolio planning,
and protection against optimizing the EIG proxy instead of scientific value.

### Domain contracts

Discovery is currently materials-specific and now refuses other domains honestly. A scalable domain
plugin needs a typed ontology for valid questions, data requirements, baselines, interventions,
claim rules, external validation, and stopping conditions. “Add another dataset adapter” is not
equivalent to adding scientific competence.

### Evidence-bundle rendering

The claim ledger constrains the write-up, but the target is stronger: every factual sentence,
number, table, and citation in the paper should be generated from a typed evidence object. The bundle
also needs data/model cards, environment lock, all negative results, audit trails, and one-command
reproduction. Publication and external submission remain human-approved.

## Security and governance gaps (P1)

| Surface | Current exposure | Required control |
|---|---|---|
| Retrieved papers, dataset text, and column names | Prompt injection can alter model behavior or scientific framing | Treat all retrieved/data text as untrusted, delimit it structurally, scan instructions, minimize tool permissions, and test adversarial corpora |
| URL dataset registration | Follows arbitrary HTTP(S) destinations; SSRF and oversized downloads are possible for an operator | Public-IP allow policy, DNS/redirect revalidation, content-type/size/time quotas, checksums, malware/archive scanning |
| Uploads and archives | Whole upload is read into memory; extraction/expanded size and file count are not quota-bound | Stream uploads, enforce compressed/uncompressed quotas, safe extraction, filename policy, per-run storage limits |
| Local-path datasets | An operator can point the service at arbitrary host-readable paths | Workspace-root allowlist or explicit admin capability; never expose this in a shared deployment |
| Secrets and sensitive research data | No explicit data-to-provider policy or redaction boundary | Secret broker, scoped credentials, egress policy, data classification, provider allowlist, audit log, retention/deletion rules |
| Dependencies/models | Mostly unpinned packages and moving model aliases | Lockfiles/SBOM, signed digest-pinned images, vulnerability scanning, model snapshot and eval-based upgrades |
| Outward actions | GitHub/publishing boundaries exist but need end-to-end policy tests | Central capability policy, dry-run previews, idempotency, approval records, and least-privilege short-lived credentials |

## Claude/GPT parity: current state and remaining work

Implemented now:

- `ALETHEIA_ORCHESTRATOR_PROVIDER=claude|openai` selects the scientist and isolated workers;
  `ALETHEIA_OPENAI_AUTH_MODE=subscription|api_key` selects ChatGPT/Codex allowance or metered API.
- GPT API mode uses Responses with `store=false`, full local output replay, strict functions, tool
  gates and bounded loops. Subscription mode uses official non-interactive Codex CLI, a cached
  ChatGPT login, strict control output, empty temporary workspaces, disabled built-in tools, and the
  same locally executed/gated Aletheia tools. Cache provenance distinguishes both transports.
- Scoping tools, memory, dataset inspection/request, literature search, discovery code authoring, RAG
  generation, and one-shot runs share the same provider-neutral tool/runtime boundary.
- AI-demonstration audit excludes whichever vendor authored it.

Still required before claiming parity:

- A full live GPT scoping run, materials campaign, tool-heavy survey, interruption/recovery test,
  and resume test against both a ChatGPT subscription and an OpenAI API project.
- A fixed Claude-vs-GPT evaluation set measuring stage success, valid JSON, tool correctness,
  scientific defects caught, latency, tokens, and total cost.
- Exact OpenAI dollar accounting and hard per-run spend enforcement; the adapter currently records
  exact tokens and relies on configured stage estimates for USD.
- Streaming/interrupt UX parity and a per-run immutable provider/model manifest.
- Structured-output schemas for JSON-producing stages, rather than prompt-only JSON plus fallback.

## Recommended delivery sequence

1. **Production safety gate:** containerize every authored-code execution path; freeze run/data/model
   manifests; add migrations and durable jobs.
2. **Epistemic seal v2:** fresh confirmation batches, a final external holdout, sequential testing,
   and family-level disclosure of every attempted hypothesis.
3. **Grounding and independent review:** measured literature coverage, comparable SOTA records,
   universal author exclusion, and a hard independent-vendor floor.
4. **Provider parity release:** run the same live acceptance suite on Claude and GPT; publish the
   deltas and pin approved configurations.
5. **Scientific reach:** causal/mechanistic domain contracts, external replication, calibrated
   campaign planning, and evidence-only paper rendering.

## Final-goal acceptance bar

Aletheia should not call itself a reliable autonomous frontier scientist until it can repeatedly:

1. Form a question that survives a measured, auditable prior-art search.
2. Pre-register a discriminating experiment without access to its confirmation evidence.
3. Adapt across rounds without reusing confirmation information improperly.
4. Execute all authored code inside a hard sandbox with immutable provenance.
5. Produce a statistically defensible result that survives independent external replication.
6. Pass genuinely author-independent review with unresolved objections visible.
7. Render a complete claim-to-evidence bundle whose conclusions cannot exceed the ledger.
8. Recover safely from process failure and reproduce under a pinned environment/provider snapshot.

Until those conditions are met, the honest product description is: **a well-guarded, increasingly
capable research operating system that can automate and audit experiments, not an institutionally
reliable autonomous scientist.**
