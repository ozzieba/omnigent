# Host admission through the server CLI

The ordinary server command can construct the bounded host-admission adapter
using the same database engine as the host store. It is disabled by default.
To mount its authenticated API, use an explicit server configuration:

```yaml
host_admission:
  enabled: true
```

Start with `omnigent server -c server.yaml`. The only supported option is the
strict boolean `enabled`; strings, numeric values and readiness options fail
startup. The adapter uses two workers and limits caller waiting to five seconds.
Database work and inherited owner lookup can continue beyond that wait; this
setting does not impose a database transaction or whole-request deadline.
Application shutdown retains unfinished worker custody through the existing
adapter lifespan.

This configuration does not enroll hosts or enable maintenance. Enrollment and
drain continue to return 503 until a trusted server bootstrap qualifies complete
ingress and replica coverage. YAML keys cannot grant that readiness. Existing
pending and UNKNOWN operations retain their original custody.
