{{/*
Common labels applied to every object this chart renders.
*/}}
{{- define "boxxkite.labels" -}}
app.kubernetes.io/name: boxxkite
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}
