# Chatmail and SimpleX qualification

Run campaigns from this lab checkout. The adapter repositories own the native
protocol harnesses and evidence contracts; use exact provider/core checkouts
under `sources/qualification/` rather than copying those implementations here.
Docker interop and hardware results do not qualify these service transports.

## Prepare a source pair and environment

Select immutable core and provider commits independently of any evidence record.
Keep their public repository URLs, full SHAs, actual SDK versions and dirty-source
checks in `.local/campaigns/targets.json`. Preserve the existing Docker
`sources.lock.json`: a service campaign is a different selected source pair.
Existing checkout mismatches require review; do not reset or clean them silently.

For each transport, use its own environment. The following starts from prepared
exact checkouts, with `TRANSPORT` set to `chatmail` or `simplexchat`:

```bash
TRANSPORT=chatmail
CORE="$PWD/sources/qualification/core"
PROVIDER="$PWD/sources/qualification/$TRANSPORT"
RUN="$PWD/.local/campaigns/$TRANSPORT"
install -d -m 700 "$RUN"
UV_PROJECT_ENVIRONMENT="$RUN/venv" \
  uv sync --project "$CORE" --locked --extra dev
UV_PROJECT_ENVIRONMENT="$RUN/venv" \
  uv export --project "$CORE" --locked --extra dev --no-emit-project --no-hashes \
  --output-file "$RUN/core-constraints.txt"
uv pip install --python "$RUN/venv/bin/python" \
  --constraint "$RUN/core-constraints.txt" --editable "$PROVIDER"
```

Select the uv release declared by the core checkout when reproducing its CI.
Scope the environment override to these preparation commands so the lab's own
Docker CLI continues to use its host `.venv`. After synchronization, invoke the
campaign environment's Python directly; do not let a later implicit sync remove
the editable provider. Record installed core/provider
paths and versions, the declared SDK pins and actual installed SDK versions.
An import or metadata check is preflight, not native qualification.

## Chatmail

Use two dedicated, independently observed native profiles on a selected mail
relay. Stop other clients sharing those profiles. Keep a private fixture under
`.local/campaigns/chatmail/` following the selected provider's
`docs/live-testing.md`; its account and chat IDs must identify the same accepted
encrypted group. The harness does not provision or reconfigure the relay.

The official relay image (`ghcr.io/chatmail/docker`) starts systemd as PID 1,
stops on `SIGRTMIN+3` and declares `VOLUME /sys/fs/cgroup`; started as an
unprivileged container on a cgroup-v2 host it exits immediately without
reaching a running state. The lab does not provide the relay as a container:
a campaign uses a preconfigured relay or a disposable machine that can host
the image's systemd and cgroup requirements.

```bash
RUN="$PWD/.local/campaigns/chatmail"
PROVIDER="$PWD/sources/qualification/chatmail"
MEDRE_CHATMAIL_TEST_CONFIG="$RUN/live.json" "$RUN/venv/bin/python" -m pytest \
  -c "$PROVIDER/pyproject.toml" "$PROVIDER/tests/test_live.py" \
  -m live -q --junitxml="$RUN/junit.xml"
```

Require execution with no skips, independent ingress in both directions, actual
SMTP handoff, process reconnect, admission of history received while MEDRE was
stopped, and a renderer-derived native reply after restart. Retain the peer's
exact quote identity, sender/origin presentation and declared SDK versions.
SMTP acceptance is not recipient delivery. The checked-in older SDK evidence
does not automatically qualify a later RPC client/server pin.

## SimpleX Chat

Use the selected provider's `docs/qualification/native-quoted-replies.md` and
`scripts/qualify_native_quotes.py`. Two distinct native identities and independent
recipient observations are required. This can use dedicated local profiles or
another observation host; provisioning, invitation acceptance and group/contact
selection are explicit campaign steps. A second physical radio is irrelevant.
For a one-host campaign the local messaging server can be a disposable
unprivileged container from the official `simplexchat/smp-server` image —
unprivileged, with memory-backed state and a loopback-only port; the
provider's qualification material owns the exact recipe.

```bash
RUN="$PWD/.local/campaigns/simplexchat"
PROVIDER="$PWD/sources/qualification/simplexchat"
"$RUN/venv/bin/python" "$PROVIDER/scripts/qualify_native_quotes.py" --check-sdk
"$RUN/venv/bin/python" "$PROVIDER/scripts/qualify_native_quotes.py" --template \
  > "$RUN/peer-template.json"
```

Run and retain the SDK check on each observation host. The six campaign cases are
direct quote, group quote, sender/origin attribution, expired/deleted-parent
fallback, restart without duplicate submission, and uncertain submission staying
fenced. Observe outcomes in the independent recipient application. A generated
template starts unqualified; filling booleans is not a substitute for evidence.

Validate the completed metadata against independently selected core/provider
SHAs using `--validate`, `--core-commit` and `--provider-commit`. The validator
checks completeness and consistency; it neither performs the campaign nor
verifies the truth of linked observations. Replies were advertised as fallback
until the completed campaign and promotion review; native replies are now
advertised. This does not automatically qualify a later SDK/source pair. Any new
capability change still requires the campaign and a separate promotion review.

## Completion and publication review

Retain private logs, source/SDK provenance, teardown results and independently
accessible redacted observations under the campaign directory. Mark every
unexecuted or skipped case explicitly. Native profiles are exclusive resources;
teardown must release their process and ledger ownership before another run.

Return a sanitized report identifying exact revisions, actual tests/observations,
limits and remaining release gates. A successful local campaign does not publish
packages, advance capabilities or authorize a tag. Review release artifacts and
the public evidence separately before publication.
