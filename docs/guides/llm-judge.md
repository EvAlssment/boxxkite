# Standalone rubric-based grading

`boxxkite.llm_judge.LLMJudge` accepts a rubric and an async callable supplied
by the application. The callable receives a prompt and may return a JSON
string or a mapping. The judge validates the response and returns a `Grade`
with a numeric score, non-empty rationale, and SHA-256 transcript hash.

```python
from boxxkite.llm_judge import LLMJudge

async def model_client(prompt: str) -> str:
    return await your_model_sdk_call(prompt)

judge = LLMJudge("Award full credit only when all tests pass.", model_client, model="provider/model")
grade = await judge.grade(tool_call_transcript, final_sandbox_state)
```

The score range defaults to 0–1 and can be changed with `max_score`. Model
clients and provider SDKs are not bundled. Cache use is local to the judge
unless a caller-owned mutable mapping is passed. Keys include the canonical
transcript, rubric, final state, score range, and caller-supplied model
namespace; this prevents reuse when grading inputs differ. Cache contents are
not persisted or shared across processes. The transcript hash covers the
canonical JSON transcript only. Inputs must be JSON-compatible.

Transcripts and final-state payloads may contain sensitive information or
instruction-like content. The caller is responsible for obtaining any needed
consent and redacting content before passing it to the injected provider
client. The grader does not claim prompt-injection resistance, and its score
or rationale must not be used as a security or authorization decision.

This is a standalone API. It does not provide a `boxxkite-eval` harness,
capture transcripts or sandbox state, or establish the correctness of model
judgments.
