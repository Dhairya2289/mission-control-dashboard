# hermes-customizations

> Keeping a self-hosted fork of an open-source agent framework
> ([Hermes](https://github.com)) alive across upstream updates — **config over
> code**, plus an idempotent restore hook.

This dashboard runs on top of a personal [Hermes](https://github.com) install.
Hermes ships frequent updates, and `hermes update` does a hard reset of the
agent clone to upstream `main` — which silently reverts any local code patches.
Rather than fork-and-drift, I keep customizations durable with two rules:

1. **Prefer config to code.** Anything expressible as configuration goes in
   `~/.hermes/config.yaml`, which lives in the *data* directory — outside the
   git clone — so `hermes update` can't touch it.
2. **For the rest, make re-applying a patch a one-command, fail-safe step.**
   A single `restore_after_update.sh` re-asserts everything after an update.

These are **sanitized excerpts** of my real setup, published as a portfolio
showcase of the pattern. They won't run unmodified — adjust paths, profile
names, channel ids, model names, and the "is my patch present?" checks to your
own install.

## What's here

| File | What it does | Pattern shown |
|------|--------------|---------------|
| `heal_kimchi.py` | Re-asserts a "config-not-code" LLM default (a WAF `User-Agent` + default provider/model) into `config.yaml`. Idempotent; only rewrites on drift. | Config that survives upstream resets |
| `apply_channel_models_patch.py` | Re-inserts a small `gateway/run.py` code patch (per-Discord-channel default model) using **stable anchors + compile-verify + auto-rollback** — never a brittle full-file overwrite. | Safe, repeatable code patching |
| `restore_after_update.sh` | The post-update hook: rebase the `local-customizations` branch onto the new `main`, re-apply the code patch, restart the gateway(s), then flag any config drift. | One-command recovery |

## The "config-not-code" story (worked example)

The kimchi / Cast AI gateway's WAF only serves requests whose `User-Agent` is
the official CLI's (`kimchi/0.1.13`); anything else gets a *fake* `402
"exhausted credits"`. My first fix patched the UA into the agent **code** — and
`hermes update` reverted it, breaking the provider until I noticed.

The durable fix sends that UA from `model.default_headers` in `config.yaml`
instead. `heal_kimchi.py` re-asserts it (and makes kimchi the default
provider/model), so even a fresh `hermes setup` that regenerates the config
self-corrects on the next run. Same idea, zero code in the update's blast radius.

## Usage

```bash
# After every `hermes update`:
./restore_after_update.sh

# Or assert just the config default, reporting only (no writes):
python3 heal_kimchi.py --check
```

## Notes

- **Safety first.** Every script is idempotent and non-destructive: configs are
  only rewritten on drift, the code patch compile-verifies and rolls back on any
  failure, and the restore hook stashes stray changes before touching git.
- **Not included.** My local key-funding probe is intentionally omitted from
  this public release — it reads the credentials file (`~/.hermes/auth.json`)
  and only ever has a place on the machine that owns those keys.
- These touch only my own self-hosted install and my own provider keys.

*MIT-licensed, same as the parent repo.*
