# files/

Per-model chat-template Jinja files live here.

When a `serveModels` entry sets `chatTemplate: <filename>.jinja`, the chart
loads `files/<filename>.jinja` into a `chat-template-<model>` ConfigMap
(see `templates/configmap-chat-templates.yaml`) and mounts it at
`/templates` inside the Ray head and worker pods, where vLLM can pick it up
for tool/function calling.

Add one Jinja file per model that needs a custom chat template, e.g.
`tool_chat_template_qwen3-8b.jinja`.
