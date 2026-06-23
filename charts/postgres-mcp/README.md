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
