# Runbook

For the person on the machine at the time, including you in six months. It
assumes nothing about having read the architecture plan.

Three things run on the 5090 box, and they are started three different ways.
That is worth knowing before anything else, because "restart the server" means
a different command for each:

| What | How it runs | Comes back after a reboot because |
|---|---|---|
| vLLM (the model, port 8000) | container, `docker compose` | `restart: unless-stopped` |
| syslab-server (the app, port 8080) | systemd, `syslab-server@syslab` | `systemctl enable` |
| cloudflared (the tunnel) | container, `docker compose` | `restart: unless-stopped`, and only when `COMPOSE_PROFILES=public` |

Everything below is run from `/home/syslab/syslab-server` unless it says
otherwise.

---

## Start, stop, restart

```bash
# the model
docker compose up -d vllm
docker compose stop vllm
docker compose restart vllm

# the app
sudo systemctl start syslab-server@syslab
sudo systemctl stop syslab-server@syslab
sudo systemctl restart syslab-server@syslab      # after editing .env or pulling code

# the tunnel
docker compose up -d cloudflared
docker compose stop cloudflared

# everything the compose file owns, including the tunnel if .env enables it
docker compose up -d
```

`docker compose down` also removes the containers. That is fine — they are
disposable and the model cache is a named volume — but it is not the command
for "stop it for a minute", and `down -v` deletes the model cache, which means
re-downloading tens of gigabytes.

## Read the logs

```bash
docker compose logs -f vllm                        # the model
journalctl -u syslab-server@syslab -f              # the app
docker compose logs -f cloudflared                 # the tunnel
journalctl -u syslab-server@syslab --since "1 hour ago" --no-pager
```

vLLM prints a great deal at startup and then goes quiet. The line that means it
is actually ready is `Application startup complete`; anything before that and a
request will simply be refused.

## Is it healthy?

```bash
curl -s localhost:8000/v1/models | head            # vLLM, no auth
curl -s -H "Authorization: Bearer $GATEWAY_TOKEN" localhost:8080/v1/models
python scripts/check_remote.py                     # the whole surface, from outside
python scripts/check_api_compat.py                 # the /v1 contract still holds
python scripts/check_gateway_isolation.py          # inference cannot reach tenant files
```

`/v1/models` on port 8080 must answer `syslab-default` and must never contain
the real model name. If it does, something has bypassed the alias table and the
website is one deploy away from being pinned to a model name that will change.

---

## Publishing it: Cloudflare Tunnel

Done once. The tunnel dials out to Cloudflare and holds the connection open, so
**no port is forwarded, no firewall rule is opened, and the box needs no static
address**. If you find yourself opening a port, something has gone wrong with
this plan rather than with the tunnel.

1. **Cloudflare Zero Trust → Networks → Tunnels → Create a tunnel.** Pick
   `Cloudflared`. Name it for the machine, not the service — the tunnel outlives
   any one thing behind it.
2. Copy the token it shows. Do not run the `docker run` command it offers; this
   repo already has the service. Put the token in `.env`:

   ```
   COMPOSE_PROFILES=public
   CLOUDFLARE_TUNNEL_TOKEN=<the token>
   ```

3. **Add a public hostname** on that tunnel: a subdomain you own, service
   `HTTP`, URL `host.docker.internal:8080`. That URL is the app on the host, not
   a container — the app still runs from a virtualenv, and
   `docker-compose.yml` gives cloudflared a `host.docker.internal` mapping so it
   can reach it.
4. **Put Access in front of it.** Zero Trust → Access → Applications → Add a
   self-hosted application on that hostname. Then create a **service token**
   (Access → Service Auth) and a policy whose action is *Service Auth* and whose
   include rule is that token. The website sends the token's two headers
   (`CF-Access-Client-Id`, `CF-Access-Client-Secret`) alongside its own
   `Authorization: Bearer`. They do not collide, and neither replaces the other:
   Access decides whether the request reaches the box at all, `GATEWAY_TOKENS`
   decides whether it may spend the GPU.
5. **Then, and only then**, set the two settings that assume the tunnel exists:

   ```
   PUBLIC_MODE=true
   TRUST_CLIENT_IP_HEADER=true
   ```

   `TRUST_CLIENT_IP_HEADER` is the one to think about. Behind the tunnel every
   request arrives from the cloudflared container, so without it the login
   throttle counts the whole internet into a single bucket and eight wrong
   tokens from anyone locks out everyone. With it on but port 8080 still
   reachable directly, anyone can evade the throttle by varying one header.
   **Turn it on only when the app is genuinely unreachable except through the
   tunnel.** Check that, don't assume it:

   ```bash
   sudo ss -lntp | grep 8080        # what is listening, and where from
   ```

6. `docker compose up -d && sudo systemctl restart syslab-server@syslab`, then
   run `python scripts/check_remote.py` against the public hostname.

### Turning it off again

Comment out `COMPOSE_PROFILES` in `.env`, `docker compose stop cloudflared`,
and set `PUBLIC_MODE` and `TRUST_CLIENT_IP_HEADER` back to `false`. Deleting
the tunnel in the dashboard invalidates the token, which is the right move if
the token has leaked; the hostname and the Access policy survive, so re-creating
it is a token change and nothing else.

---

## Rolling back

**A bad commit in this repo.** The app is the only thing that runs from source:

```bash
git log --oneline -10
git checkout <good-commit>
sudo systemctl restart syslab-server@syslab
```

**A bad model or vLLM version.** Both are in `docker-compose.yml`, both are
pinned by digest, so rolling back is editing one line and:

```bash
docker compose up -d vllm          # recreates, because the image string changed
docker compose logs -f vllm
```

Changing the *model* costs a download the first time and nothing thereafter —
the Hugging Face cache is a bind-mounted volume at
`/home/syslab/.cache/huggingface` and survives `docker compose down`. Changing
the model also changes behaviour, so it is a `CHANGELOG.md` entry and a
`docs/models.md` edit, per this repo's own rule.

**Something the website depends on.** Do not roll the website back to work
around this server. Run `python scripts/check_api_compat.py` first: if it
reports a breaking change, the fix is here, and it is the change that broke the
contract rather than anything the website did.

---

## Common failures

**vLLM will not start, `CUDA out of memory`.** Something else is holding the
card. `nvidia-smi` shows what. Ollama is the usual culprit — it is a dev-laptop
tool and should not be running on this box; `sudo systemctl stop ollama`. This
exact thing produced a 2 tok/s benchmark once, because Ollama had quietly fallen
back to CPU with vLLM holding 23 GB.

**The model answers, but every tool call fails with HTTP 400.** The container
was started without `--enable-auto-tool-choice --tool-call-parser hermes`. Those
are not optional and not a performance setting: without them `tool_choice`
returns 400 for every value except `"none"`, which is the entire feature the
website's agent depends on. They are in `docker-compose.yml`; confirm they
survived whatever edit came last.

**Answers are slow and end mid-sentence, `finish_reason: "length"`.** Qwen3 is
thinking, and spent the token budget on it. Requests from this app send
`chat_template_kwargs: {"enable_thinking": false}`; a caller reaching vLLM
directly on port 8000 does not, and gets the default.

**`/v1` returns 503 "no tokens configured".** `GATEWAY_TOKENS` is empty in
`.env`. It fails closed on purpose: an inference plane with no credential is not
an open one.

**Everything returns 401 after a restart, including you.** `APP_TOKEN` changed,
which signs out every device. Cookies are set from the token used at sign-in, so
this is expected rather than broken; sign in again.

**Port 8080 is already in use.** Almost always a hand-started `python -m
app.main` from an SSH session that is still alive alongside the systemd copy.
`sudo ss -lntp | grep 8080` names the process.

**Port 8000 is already in use.** vLLM owns 8000. If the app is trying to claim
it, `APP_PORT` is wrong in `.env` — it must not be 8000 on this machine.

**The tunnel is up but the hostname 502s.** cloudflared reached Cloudflare and
cannot reach the app. Either the app is down (`systemctl status`) or the public
hostname points somewhere the container cannot resolve — it must be
`host.docker.internal:8080` while the app runs on the host.

**The tunnel container restarts in a loop.** The token is missing, invalid or
revoked — all three look the same from outside, and `docker compose logs
cloudflared` distinguishes them. Check `CLOUDFLARE_TUNNEL_TOKEN` in `.env`
first, since an empty one reaches cloudflared as an empty string rather than
being caught by compose. (Compose *could* catch it, with `${VAR:?}`, but it
interpolates the whole file before filtering by profile, so that would break
`docker compose up -d vllm` on every machine that has no tunnel.)

---

## What this does not cover

Jobs are in memory and do not survive a restart of the app. A transcription or
an embedding run that was in flight is gone, and nothing will tell the person
who asked for it. Persistent jobs are a named deferred step in the architecture
plan; until then, restart the app when it is idle if you have the choice.
