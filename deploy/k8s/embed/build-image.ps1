<#
.SYNOPSIS
    Build, verify and push the agent-notes-embed image on the builder host.

.DESCRIPTION
    Archives the committed Dockerfile and src/agent_notes, builds on the builder
    (itadmin@mvmcc02 by default), runs the image read-only and non-root with a
    throwaway token, checks /health, a token-less /embed (must be 401) and an
    authenticated /embed, then pushes and prints the registry digest for
    kustomization.yaml. -NoPush stops after verification. -AllowDirty builds
    the working tree instead of HEAD (for testing; never for a release).
#>
param(
    [string]$Builder = 'itadmin@mvmcc02',
    [string]$Image = 'git.hraedon.com/glassbreak/agent-notes-embed',
    [switch]$NoPush,
    [switch]$AllowDirty
)
$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
Push-Location $projectPath
try {
    if ($Builder -notmatch '^[a-zA-Z0-9_.@-]+$' -or $Image -notmatch '^[a-z0-9.-]+(/[a-z0-9._-]+)+$') { throw 'Invalid builder or image name.' }
    $paths = @('deploy/k8s/embed/Dockerfile', 'src/agent_notes')
    $revision = (git rev-parse HEAD).Trim()
    if ($revision -notmatch '^[a-f0-9]{40}$') { throw 'Expected a full Git revision.' }
    $dirty = [bool](git status --porcelain -- $paths)
    if ($dirty -and -not $AllowDirty) { throw 'Commit source before building a release (or pass -AllowDirty to test).' }
    $version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"$').Matches[0].Groups[1].Value
    $tag = "$Image`:$version-$($revision.Substring(0,12))"
    if ($dirty) { $tag += '-dirty' }
    $buildDirectory = "/tmp/agent-notes-embed-$revision"
    $archive = Join-Path ([System.IO.Path]::GetTempPath()) "agent-notes-embed-source-$revision.tar"
    if ($dirty) {
        tar --exclude=__pycache__ -cf $archive @paths
    } else {
        git archive --format=tar --output=$archive HEAD @paths
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not archive source.' }
    ssh -o BatchMode=yes $Builder "mkdir -p $buildDirectory"
    if ($LASTEXITCODE -ne 0) { throw 'Builder connection failed.' }
    scp -q $archive "$Builder`:$buildDirectory/source.tar"
    if ($LASTEXITCODE -ne 0) { throw 'Source transfer failed.' }
    $script = @'
set -eu
cd BUILD_DIRECTORY
trap 'cd / && rm -rf BUILD_DIRECTORY' EXIT
tar -xf source.tar
docker build --pull -f deploy/k8s/embed/Dockerfile --build-arg VERSION=APP_VERSION --build-arg REVISION=APP_REVISION -t IMAGE_TAG .
name=agent-notes-embed-verify-SHORT_REVISION
token=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
docker run -d --name "$name" --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m -e AGENT_NOTES_EMBED_TOKEN="$token" -p 127.0.0.1::8766 IMAGE_TAG >/dev/null
trap 'docker rm -f "$name" >/dev/null; cd / && rm -rf BUILD_DIRECTORY' EXIT
port=$(docker port "$name" 8766/tcp | head -1)
ready=0
for attempt in $(seq 1 60); do
  if curl -fsS "http://$port/health"; then ready=1; break; fi
  sleep 1
done
test "$ready" = 1 || { docker logs "$name"; exit 1; }
echo
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{"texts":["x"],"task":"query"}' "http://$port/embed")
test "$code" = 401 || { echo "token-less /embed returned $code, expected 401"; exit 1; }
curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"texts":["postgres tuning","another"],"task":"query"}' "http://$port/embed" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["dim"]==768 and len(d["vectors"])==2 and len(d["vectors"][0])==768, d.keys(); print("embed ok:", d["model"])'
docker image inspect IMAGE_TAG --format 'image size: {{.Size}} bytes'
if [ "PUSH" = 1 ]; then
  docker push IMAGE_TAG >/dev/null
  docker image inspect IMAGE_TAG --format '{{index .RepoDigests 0}}'
fi
'@
    $script = $script.Replace('BUILD_DIRECTORY', $buildDirectory).Replace('APP_VERSION', $version).Replace('APP_REVISION', $revision).Replace('SHORT_REVISION', $revision.Substring(0,12)).Replace('IMAGE_TAG', $tag).Replace('PUSH', $(if ($NoPush) { '0' } else { '1' }))
    # PowerShell ends piped input with CRLF; end on a comment so the stray CR lands in it.
    ($script.Replace("`r`n", "`n") + "`n# end of build script") | ssh -o BatchMode=yes $Builder 'bash -se'
    if ($LASTEXITCODE -ne 0) { throw 'Build, container verification or push failed.' }
    Write-Output "Source revision: $revision$(if ($dirty) { ' (plus uncommitted changes)' })"
    Write-Output "Image tag: $tag"
} finally {
    if ($archive) { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }
    Pop-Location
}
