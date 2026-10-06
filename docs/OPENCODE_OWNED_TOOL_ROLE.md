# Owned Gemma native tool role

The runner supports an explicit `executor.config.opencode_tool_role` value of
`owned-gemma-p16v`. This role is bound to the resolved model
`p16v/gemma4:e2b`; selecting that model alone does not enable the role.

The launcher writes the role configuration before `opencode serve` starts.
Native read, edit, Bash, glob, and grep permission families retain `ask`, so
Omnigent's permission evaluator still handles calls. Other tool permissions,
including the `omnigent_*` MCP namespace, are denied. The relay and policy plugin
remain available for policy transport. Provider definitions are preserved;
provider credentials are not copied into the environment overlay.

The role disables workspace OpenCode configuration and pins its model and
permission overlay through `OPENCODE_CONFIG_CONTENT` and `OPENCODE_PERMISSION`.
It overrides inherited config-directory/home flags and gives this role a private
process home, preventing home-discovered agents and plugins from widening tools.
Provider definitions and authentication are seeded before changing the child
environment. This role requires Linux without `/etc/opencode` managed config;
managed policies are rejected rather than bypassed. Additional declared MCP
servers, custom plugins, gateway profiles, conflicting model overrides, and
terminal arguments are rejected rather than silently changed. It currently
accepts only fresh new sessions: cached instances, existing native IDs,
history-carrying forks, and existing XDG config/account state fail before
cached-server teardown. Auth seeding uses a validated snapshot; remote
`wellknown` auth/config producers and unknown auth types are rejected. Adoption must account for this limitation; this
change does not migrate running sessions or enable a lane.

Other OpenCode sessions keep their existing MCP and permission configuration.

## Verification

Run the source fixtures without starting a model or changing a pool:

```sh
OMNIGENT_SKIP_WEB_UI=true uv sync --locked --group lint --group test --extra tracing
.venv/bin/python -m pytest -p no:rerunfailures -o addopts='' \
  tests/runner/test_opencode_owned_gemma_role.py tests/test_opencode_native_provider.py
```

The startup fixture reads the actual generated `opencode.json` at the mocked
server-start boundary and checks the role environment. Its ordinary-session
case verifies unchanged MCP and ASK behavior. Invalid launch and merged-plugin
cases verify that the cached server and forwarder remain untouched.
