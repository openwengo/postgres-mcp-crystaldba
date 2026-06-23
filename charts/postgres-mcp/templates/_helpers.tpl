{{/*
Expand the name of the chart.
*/}}
{{- define "postgres-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "postgres-mcp.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "postgres-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "postgres-mcp.labels" -}}
helm.sh/chart: {{ include "postgres-mcp.chart" . }}
{{ include "postgres-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels.
*/}}
{{- define "postgres-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "postgres-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Create the name of the service account to use.
*/}}
{{- define "postgres-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "postgres-mcp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Create the generated database secret name.
*/}}
{{- define "postgres-mcp.generatedDatabaseSecretName" -}}
{{- if .Values.database.secretNameOverride -}}
{{- .Values.database.secretNameOverride -}}
{{- else -}}
{{- printf "%s-database" (include "postgres-mcp.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Create the legacy DATABASE_URI secret name.
*/}}
{{- define "postgres-mcp.databaseSecretName" -}}
{{- if .Values.database.existingSecret -}}
{{- .Values.database.existingSecret -}}
{{- else -}}
{{- include "postgres-mcp.generatedDatabaseSecretName" . -}}
{{- end -}}
{{- end -}}

{{/*
Create the DATABASE_CONNECTIONS secret name.
*/}}
{{- define "postgres-mcp.databaseConnectionsSecretName" -}}
{{- if .Values.database.existingConnectionsSecret -}}
{{- .Values.database.existingConnectionsSecret -}}
{{- else -}}
{{- include "postgres-mcp.generatedDatabaseSecretName" . -}}
{{- end -}}
{{- end -}}
