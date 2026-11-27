# Hermes Agent on Argos (homelab)

In this homelab every model call goes through Argos, the model hub on the
Unraid box. Argos then shows each call in its metrics and captures, which
Overseer uses later. This file is fork-only, so upstream syncs never touch it.

Hermes runs on the NUC with `network_mode: host`, so it cannot use the Docker
name `argos`. Use the Unraid box's LAN address instead.

## Config

Put this in `~/.hermes/config.yaml` on the NUC (or merge it into the `model:`
and `auxiliary:` blocks you already have):

```yaml
model:
  provider: "custom"                        # any OpenAI-compatible endpoint
  base_url: "http://192.168.20.5:8080/v1"   # GUESS: the Unraid box's LAN IP; Argos' port
  default: "CHANGE-ME"                      # a model name from Argos' config (its /v1/models)

# Side tasks (titles, compression, web summaries, vision) also go to Argos.
# "main" means "the endpoint above". Vision needs a model that takes images.
auxiliary:
  vision:           { provider: "main", model: "" }
  web_extract:      { provider: "main", model: "" }
  title_generation: { provider: "main", model: "" }
  session_search:   { provider: "main", model: "" }
  compression:      { provider: "main", model: "" }
```

And in `~/.hermes/.env`:

```bash
# Only if Argos has apiKeys set. Otherwise any value works.
OPENAI_API_KEY=sk-your-argos-key
# Leave these unset, or "auto" picks OpenRouter before Argos:
# OPENROUTER_API_KEY=
```

Browser tasks (`browser_task`, browser-use) can use an Argos model too. Install
browser-use first (see the Browser docs, "Browser tasks"), then:

```yaml
browser:
  task:
    model: gemma-4-31b                      # an Argos model; Gemma 4 31B passed our checks
    base_url: "http://192.168.20.5:8080/v1"
```

## Check it

```bash
curl -s http://192.168.20.5:8080/v1/models | head   # Argos answers from the NUC
hermes chat -q "say hi"                             # then look for it in Argos' Activity page
```

## What goes wrong

- **Connection refused:** Argos publishes 8080 on the host by default
  (`ARGOS_PORT`). Check the Unraid IP and that the port is published.
- **401 from Argos:** Argos has `apiKeys` set; put one in `OPENAI_API_KEY`.
- **Calls still reach OpenRouter:** `OPENROUTER_API_KEY` is set, and an
  auxiliary task left on `auto` prefers it. Unset it, or set each task to `main`.
- **Model not found:** `default` must match a model name in Argos' config, not a
  file name.
