# agent-notes-embed on Kubernetes

One shared, warm embedding service for every agent-notes host, so no host has
to load the model (~9s per CLI call) or run its own service (~1 GB each).
Clients reach it at `https://agent-notes-embed.k8s.hraedon.com` through the
internal Traefik ingress, with a cert-manager certificate and a bearer token.

If the service is down, clients fall back to loading the model in-process
(slow, same vectors). A wrong or missing token fails the call instead, and so
does a service on a different model, so a misconfiguration can't go unnoticed.

## Build

Commit first, then from Windows:

```powershell
pwsh -File deploy/k8s/embed/build-image.ps1
```

It builds on `itadmin@mvmcc02` from a `git archive` of the Dockerfile and
`src/agent_notes`, runs the image read-only and non-root, checks `/health`,
that `/embed` refuses a request without the token, and an authenticated
`/embed`, then pushes to `git.hraedon.com/glassbreak/agent-notes-embed` and
prints the digest. `-NoPush` stops after verification; `-AllowDirty` builds
the working tree for testing.

The image is CPU-only torch plus the model baked in at a pinned revision
(`MODEL_REVISION` in the Dockerfile), so pods start offline. The library
versions match the hosts' in-process path; bump them together.

## First deploy

Two secrets live only in the cluster, never in this repository:

```powershell
kubectl apply -f deploy/k8s/embed/namespace.yaml
# Registry pull secret, created however felis-grove-registry was.
kubectl -n agent-notes create secret docker-registry agent-notes-registry --docker-server=git.hraedon.com --docker-username=<user> --docker-password=<token>
# Bearer token: generated here and never printed.
$token = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()
kubectl -n agent-notes create secret generic agent-notes-embed-token --from-literal=token=$token
```

Set the digest in `kustomization.yaml`, then:

```powershell
kubectl apply -k deploy/k8s/embed
kubectl -n agent-notes rollout status deployment/agent-notes-embed --timeout=300s
kubectl -n agent-notes wait --for=condition=Ready certificate/agent-notes-embed-k8s-hraedon-com-tls --timeout=180s
```

## Point hosts at it

On each host, add to the per-user suite.env (`~/.config/agent-suite/suite.env`
on Linux, `%LOCALAPPDATA%\agent-suite\agent-suite\suite.env` on Windows):

```
AGENT_NOTES_EMBED_URL=https://agent-notes-embed.k8s.hraedon.com
AGENT_NOTES_EMBED_TOKEN=<the token>
```

suite.env already holds the DSN, so it is the right place for a credential.
Then `agent-notes doctor` should report `embedding_service ... embedded in Nms`.

## Rotate the token

Replace the secret, restart the deployment, and update each host's suite.env.
Hosts with the old token fail their embed calls until updated; `doctor` names
the 401.
