{{/*
Chart name.
*/}}
{{- define "open-serve.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
*/}}
{{- define "open-serve.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "open-serve.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: open-serve
{{- end }}

{{/*
Ray container image (global default).
*/}}
{{- define "open-serve.image" -}}
{{ .Values.image.registry }}/{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
Model container image — uses per-model override if set, otherwise global default.
Usage: {{ include "open-serve.modelImage" (dict "model" $model "global" $) }}
*/}}
{{- define "open-serve.modelImage" -}}
{{- if .model.image }}
{{- .model.image.registry | default $.global.Values.image.registry }}/{{ .model.image.repository | default $.global.Values.image.repository }}:{{ .model.image.tag | default $.global.Values.image.tag | default $.global.Chart.AppVersion }}
{{- else }}
{{- include "open-serve.image" .global }}
{{- end }}
{{- end }}
