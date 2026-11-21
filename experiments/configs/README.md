# Training and evaluation configuration

Paths are relative to this folder (`experiments/configs/`).

## SWE-agent (`sweagent/`)
Resolved configs (every `base:` include merged) of the three settings in the paper:

| file | used for | context | calls | sampling |
|---|---|---|---|---|
| `train_rollout_c128k80.yaml` | GRPO rollouts and the base-model difficulty measurement (k = 8) | 131,072 | 80 | T 1.0, top_p 0.95, top_k 20 |
| `eval_verified_c128k80.yaml` | SWE-bench Verified: dev-100 and Verified-397 | 131,072 | 80 | same |
| `eval_pro_c256k150.yaml` | SWE-bench Pro (the official SWE-agent tool-use scaffold, repository at /app) | 262,144 | 150 | same |

The file names end with the context length and the call limit (`c128k80`: 128K tokens, 80 calls). The
training-rollout and Verified-evaluation files are identical by design (training uses the evaluation budget).
`agent` is a SWE-agent 1.x `DefaultAgentConfig` body; bundle paths are relative to the SWE-agent checkout, except
`tools/edit_anthropic`, which is the copy in `sweagent/tools/` (the upstream editor without `undo_edit`;
`patched_editor: true`). `budget_and_sampling` keys:

- `served_context_length`: the inference server runs with this context; `server_max_new_tokens`: per-call generation
  cap set on the server; `max_tokens: null` = no client-side cap, so each call generates at most
  min(32768, context - prompt).
- `max_input_tokens`: the episode ends with automatic submission of the current diff before a call whose previous
  prompt exceeded this (context - 4,096).
- `call_limit` with `call_limit_scope: total`: model calls per trajectory, retries included; at the limit the episode
  is submitted.
- `thinking: true`, `reasoning_in_history: true`: Qwen3.5 thinking mode; earlier turns' reasoning is sent again.
- `sanitize_history: true`: the task repository keeps a single commit at the base (no later history to read the fix
  from).
- Model sampling keys (`temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`) are sent per request.

## GRPO (`grpo_hyperparameters.yaml`)
The GRPO hyperparameters of the reported runs: first the rows of the paper's settings table (appendix "Training and
Evaluation Settings"), then the trainer settings that affect results but are not in that table (seed values of each
run, policy-ratio clip, truncated importance sampling, optimizer and rollout details, reward). Framework and hardware
settings are not listed; `concurrent_episodes` is listed because it bounds how stale a rollout can be.

## Evaluation (`eval_splits.json`)
- Task lists: SWE-bench Verified dev-100 (checkpoint selection; key `dev100`) and Verified-397 (the other 400 tasks,
  of which 397 are scored; keys `v400`, `v400_scored`, `v400_not_scored`), and the SWE-bench Pro public set (642
  tasks, 638 scored).
- Protocol (the same appendix of the paper): every GRPO run trains 80 steps; dev-100 pass@1 over 4 attempts at steps
  10, 20, ..., 80; the selected checkpoint is the step with the highest dev-100 pass@1, and a tie goes to the later
  step; that checkpoint is evaluated on Verified-397 (4 attempts) and SWE-bench Pro (2 attempts).
