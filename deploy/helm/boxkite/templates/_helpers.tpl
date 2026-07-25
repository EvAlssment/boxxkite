{{/*
Common labels applied to every object this chart renders.
*/}}
{{- define "boxkite.labels" -}}
app.kubernetes.io/name: boxkite
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}
