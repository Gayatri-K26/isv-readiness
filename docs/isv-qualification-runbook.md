# ISV Qualification Runbook

This is the command reference for a new ISV. Replace every value in angle
brackets. Keep credential values in environment variables; do not put them in
commands, YAML, API specifications, or reports.

## 1. Prepare the provider evidence

Have these ready before starting:

- the domains the product owns;
- an OpenAPI or equivalent machine-readable interface specification;
- the provider API endpoint;
- names of required credential and non-secret input environment variables;
- a representative live environment with the claimed resources available;
- an installed and authenticated Codex or Claude CLI.

## 2. Install `gapctl`

Run on the operator workstation:

```bash
uv tool install \
  "git+https://<approved-repository>/isv-readiness.git@<release-tag>"

gapctl --help
```

## 3. Initialize the workspace

The following example claims four infrastructure domains. Declare only domains
the product actually owns.

```bash
gapctl init <provider-name> \
  --workspace ./<provider-name>-readiness \
  --domains bare_metal,kubernetes,slurm,observability \
  --api https://<provider-api-host>/<base-path> \
  --api-spec /absolute/path/to/<provider-api-spec>.yaml \
  --auth PROVIDER_CLIENT_ID \
  --auth PROVIDER_CLIENT_SECRET \
  --input PROVIDER_REGION

cd ./<provider-name>-readiness
```

`init` clones `ai-cloud-validation` into the workspace, records its exact commit,
imports the provider specification and complete NCP Software Reference Guide,
and creates the initial profile and provider scaffolding. Later commands do not
silently update the pinned validation checkout.

Verify the generated state:

```bash
git -C ai-cloud-validation rev-parse HEAD
sed -n '1,220p' isv-project.yaml
sed -n '1,260p' solution-profile.yaml
```

## 4. Qualify the declared scope

```bash
gapctl qualify
```

Review:

```bash
sed -n '1,320p' \
  .gapctl/qualification/solution-profile.proposed.yaml
```

Correct unsupported ownership or coverage claims in that proposed file, then
run the same command again:

```bash
gapctl qualify
```

Approve only when the displayed hash matches the reviewed proposal. There is no
separate approval command.

Use Claude instead of Codex when needed:

```bash
gapctl qualify --generator claude
```

## 5. Check the live environment

Run only the checks that match the claimed platform.

Kubernetes:

```bash
kubectl cluster-info
kubectl get nodes -o wide
kubectl get nodes \
  -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,GPU:.status.allocatable.nvidia\.com/gpu'
kubectl get pods -A
```

GPU Operator:

```bash
kubectl get daemonsets -n <gpu-operator-namespace>
kubectl get pods -n <gpu-operator-namespace> -o wide
```

Slurm:

```bash
scontrol show config | grep '^ClusterName'
sinfo -h -o '%P|%G|%D|%a'
sinfo -N -h -o '%N|%P|%G|%t'
```

GPU access:

```bash
ssh <gpu-node> nvidia-smi -L

srun -p <gpu-partition> -N1 -n1 --gres=gpu:1 \
  --container-image=<known-gpu-container-image> \
  python3 -c 'import torch; print(torch.cuda.get_device_name(0))'
```

Provider API with mutual TLS:

```bash
curl \
  --cert "$PROVIDER_CLIENT_CERT" \
  --key "$PROVIDER_CLIENT_KEY" \
  --cacert "$PROVIDER_CA_BUNDLE" \
  -o /dev/null \
  -w 'HTTP %{http_code}\n' \
  "https://<provider-api-host>/<known-resource-path>"
```

Do not use `curl -k`; a successful insecure request is not TLS-validation
evidence.

If private endpoints require local forwarding, keep the tunnel in a separate
workstation terminal:

```bash
ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L <local-api-port>:localhost:<remote-api-port> \
  -L <local-kubernetes-port>:localhost:<remote-kubernetes-port> \
  <jump-or-head-host>
```

On macOS, verify the listeners from another terminal:

```bash
lsof -nP -iTCP:<local-api-port> -sTCP:LISTEN
lsof -nP -iTCP:<local-kubernetes-port> -sTCP:LISTEN
```

## 6. Export runtime values

Use the exact names declared during `init`:

```bash
export PROVIDER_CLIENT_ID="<value>"
export PROVIDER_CLIENT_SECRET="<value>"
export PROVIDER_REGION="<value>"

# Export additional declared paths or topology inputs when required.
export PROVIDER_CA_BUNDLE="/absolute/path/to/ca.pem"
export PROVIDER_CLIENT_CERT="/absolute/path/to/client.pem"
export PROVIDER_CLIENT_KEY="/absolute/path/to/client.key"
export KUBECONFIG="/absolute/path/to/kubeconfig"
```

These values must remain in the process environment. Project files contain only
their names.

## 7. Generate, review, and run validation

```bash
gapctl validate
```

For each proposed patch:

1. confirm it edits only provider-owned scripts or configuration;
2. compare lifecycle behavior and result fields with the pinned NVIDIA
   contract and provider interface;
3. reject placeholders, fabricated values, TLS bypasses, or broadened scope;
4. approve the exact reviewed patch;
5. authorize the live run only when the target environment is ready.

If a patch is rejected or a real capability fails, correct the provider or
environment and run the same command again:

```bash
gapctl validate
```

To use Claude:

```bash
gapctl validate --generator claude
```

## 8. Inspect the evidence

```bash
sed -n '1,260p' gaps.json

LATEST_RUN="$(ls -1dt .gapctl/runs/* | head -1)"
sed -n '1,260p' "$LATEST_RUN/run.json"
less "$LATEST_RUN/isvctl.log"
```

Classify each remaining failure before changing code:

- **provider adapter defect**: the provider interface supports the contract but
  the script maps or verifies it incorrectly;
- **target capability gap**: the live platform does not provide the required
  behavior;
- **upstream suite defect**: the NVIDIA test itself has an ordering, cleanup, or
  contract problem.

Do not alter the pinned NVIDIA suite or generate fake results to make a failure
pass.

## 9. Publish

Publish only after every owned domain has a successful current live run and no
blocking gaps:

```bash
export ISV_SERVICE_ENDPOINT="<NVIDIA-supplied-endpoint>"
export ISV_SSA_ISSUER="<NVIDIA-supplied-issuer>"
export ISV_CLIENT_ID="<NVIDIA-supplied-client-id>"
export ISV_CLIENT_SECRET="<NVIDIA-supplied-client-secret>"

gapctl publish \
  --lab-id <NVIDIA-supplied-lab-id> \
  --isv-software-version <provider-version>
```

There is no bundle step.
