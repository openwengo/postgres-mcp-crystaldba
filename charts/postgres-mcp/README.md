# Postgres MCP Helm Chart

This chart deploys Postgres MCP Pro using the SSE transport by default.

## Database Secret

Use an existing Kubernetes secret:

```bash
kubectl create secret generic postgres-mcp-database \
  --from-literal=DATABASE_URI='postgresql://user:password@postgres:5432/dbname'

helm upgrade --install postgres-mcp ./charts/postgres-mcp \
  --set database.existingSecret=postgres-mcp-database
```

Or let the chart create a secret from `database.uri`:

```bash
helm upgrade --install postgres-mcp ./charts/postgres-mcp \
  --set-string database.uri='postgresql://user:password@postgres:5432/dbname'
```

## Ingresses

`ingresses` is a map so you can create multiple ingress resources with different classes:

```yaml
ingresses:
  public:
    enabled: true
    className: nginx
    hosts:
      - host: postgres-mcp.example.com
        paths:
          - path: /
            pathType: Prefix
  internal:
    enabled: true
    className: internal-nginx
    hosts:
      - host: postgres-mcp.internal.example.com
        paths:
          - path: /
            pathType: Prefix
```

## Streamable HTTP Auth for Audit

OAuth 2.1/JWT authentication is available on HTTP transports and is used for
audit identity only. It does not change the static database connection access
model. Enable streamable HTTP and provide auth settings via `extraEnv`:

```yaml
mcp:
  transport: streamable-http

extraEnv:
  - name: MCP_ENABLE_OAUTH21
    value: "true"
  - name: MCP_UNIFIED_AUTH
    value: "true"
  - name: POSTGRES_MCP_EXTERNAL_URL
    value: "https://postgres-mcp.example.com"
  - name: POSTGRES_MCP_OAUTH_PROXY_STORAGE_BACKEND
    value: valkey
  - name: POSTGRES_MCP_OAUTH_PROXY_VALKEY_HOST
    value: valkey.example.com
```

For machine JWTs, configure `FASTMCP_SERVER_AUTH_JWT_*` or `MCP_JWT_ISSUERS`.
For human OAuth, configure `GOOGLE_OAUTH_CLIENT_ID` and
`GOOGLE_OAUTH_CLIENT_SECRET`.

## HPA and PDB

Enable autoscaling and disruption budgets independently:

```yaml
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10

pdb:
  enabled: true
  minAvailable: 1
```
