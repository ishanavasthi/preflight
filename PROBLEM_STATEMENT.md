**Hackathon Problem Statement**

**YOUR AI AGENTS ARE A BLACK BOX**

AI is eating software, and nobody can see inside it. We're here to fix that.



**FLYING BLIND**

AI agents are chaining LLM calls, invoking tools, hitting vector DBs, and making decisions autonomously. But when latency spikes, costs explode, or an agent hallucinates in production, you're flying blind. You can't debug what you can't see.



**TOTAL VISIBILITY**

SigNoz gives you full visibility into every AI workflow. Trace each agent step, monitor token costs, and correlate LLM responses with downstream failures. OpenTelemetry-native, so your instrumentation works everywhere. One platform. Every AI signal.

One platform. Every AI signal. Total observability. 🔍

SigNoz is the one-stop open observability platform built for the AI era. Instrument your agents, LLMs, and the tools they call to see traces, metrics, logs, and token cost in one place. Then point your coding agent at MCP, and your agents debug with the same data. Open source, built on OpenTelemetry, the standard your agents already speak, with no proprietary agents or lock-in.

**🛰️**

Self-host SigNoz, free and open source

Run SigNoz yourself with Docker or Kubernetes and start ingesting telemetry in minutes. [See the self-host install guide](https://signoz.io/docs/install/self-host/).

**🔌**

Don't build it alone

SigNoz is OpenTelemetry-native, with integrations for cloud providers (AWS, GCP, Azure), databases, message queues, web frameworks, and more. Browse the [full integrations list](https://signoz.io/docs/integrations/integrations-list/) to find yours and ship instrumented systems faster.



📡 One Platform, Every Signal

**ONE STOP OBSERVABILITY PLATFORM. TOTAL RECALL.**

SigNoz helps you observe every signal your systems and agents emit, such as traces, metrics, logs, and token costs, on one open platform.



AI Agent Tracing

Trace every step of your AI agent: tool calls, LLM requests, retrieval hops, and decision chains in one view.



One-Stop Observability

Traces, metrics, and logs in a single platform. Correlate signals across your entire stack without switching tools.



Flexible Deployment

Self-host SigNoz on your own infrastructure for full control, or use SigNoz to get started in minutes.



OpenTelemetry Native

Built on OpenTelemetry from day one. Instrument any language, any framework. Your telemetry data stays yours forever.



⬢ Three Tracks

**PICK YOUR MISSION**

Three tracks, one platform. Choose the track that fits your skills, or bring your own idea. Every project must use or integrate with [SigNoz](https://github.com/SigNoz/signoz). The example builds below are inspiration only, not requirements.



**Submit your project**

The project submission form is now live. Build across any of the three tracks, then submit your project before the deadline using the form.

[Submit Project](https://forms.gle/xv1TXSiC54MEWujRA)



TRACK 01



**AI &amp; Agent Observability**

Trace, monitor, and debug AI-native systems

EXAMPLE BUILDS

AI agents with E2E observability on SigNoz

Self-hosted inference observability (vLLM)

SRE Sidekick with SigNoz MCP

n8n workflows with E2E observability

Self-healing infra with SigNoz metrics

TRACK 02



**Signals &amp; Dashboards**

OpenTelemetry instrumentation &amp; Query Builder mastery

EXAMPLE BUILDS

Custom OTel auto-instrumentation library

Cross-signal panel for one service

Query Builder vs PromQL/LogQL

Multi-cluster telemetry on one SigNoz

SLO/error-budget dashboard pack

TRACK 03



**Build Your Own**

Observe anything with SigNoz

EXAMPLE BUILDS

Observability for a Slack/Telegram bot or IoT fleet

Monitor a trading bot or data pipeline

Bridge an unsupported data source into SigNoz

Monitor anything weird with a live dashboard

Ecosystem plugin: Backstage, Terraform, or Helm chart



⚖️ How You're Judged

**JUDGING CRITERIA**

01

**Potential Impact**

How effectively does the project address a meaningful problem or unlock a valuable use case with observability?

02

**Creativity &amp; Innovation**

How unique is the idea? Does it push the boundaries of what's possible when you can see inside your systems?

03

**Technical Excellence**

How well is the project implemented? Does it demonstrate strong engineering practices and clean, maintainable code?

04

**Best Use of SigNoz**

How deeply and effectively does the project lean on SigNoz, traces, metrics, logs, dashboards, and alerts?

05

**User Experience**

Is the project intuitive to use? Does it provide a polished experience that users would actually want to adopt?

06

**Presentation Quality**

How clearly is the project presented? Do the demo, README, and submission communicate the problem, solution, and impact?



**Agency Protocols**

1. You can operate solo or assemble your own agency of up to 4 members. Teams can change composition at any time before the hackathon begins.
2. Required tech: Your project must use or integrate with [SigNoz](https://github.com/SigNoz/signoz) for observability. The more deeply you lean on SigNoz and OpenTelemetry, traces, metrics, logs, dashboards, and alerts, the stronger your submission will score.
3. Three tracks, open ideas: Pick one of the three tracks (AI &amp; Agent Observability, Signals &amp; Dashboards, or Build Your Own) or bring your own idea. The example builds listed on the overview page are inspiration only; you are not limited to them.
4. Job interviews do not guarantee a job. Top winners get interview opportunities at SigNoz. These are a genuine chance to showcase your skills, but they do not guarantee a position or offer of employment.
5. You may use templates, third-party tools, frameworks, open-source libraries, public APIs, and publicly available assets (e.g. Creative Commons images, fonts, or music). Your original work built on top of these will be judged.
6. How to submit: Once your project is ready, submit it through the [project submission form](https://forms.gle/xv1TXSiC54MEWujRA) before the deadline. Everything you need to include is listed in the form.
7. Use of AI assistants (ChatGPT, Copilot, etc.) is permitted but must be declared in your submission. Failure to disclose will result in disqualification.
8. Teams can plan and discuss strategy in advance, but coding and design work should begin only after the hackathon starts. Written notes, sketches, and diagrams are permitted beforehand.
9. Teams may consist of 1–4 members.
10. Any intellectual property developed during the hackathon belongs to the team that created it. Teams are encouraged to agree internally on IP ownership.
11. Treat all participants with respect. Harassment, discrimination, or exclusionary behavior of any kind will result in immediate disqualification. If you witness concerning behavior, notify organizers immediately.
12. Failure to follow these rules or the Code of Conduct may result in disqualification from the hackathon.

**SigNoz Field Requirements**

1. Install SigNoz using Foundry. Foundry installs both SigNoz and its MCP server in one step. Follow the [Foundry quickstart](https://signoz.io/docs/install/docker/) to get started.
2. The more SigNoz features you use, the better your chances. Using the SigNoz MCP server, Query Builder, dashboards, and alerts is recommended to maximize your chances of winning. Check out the [resources section](https://www.wemakedevs.org/hackathons/signoz/resources).
3. Make your deployment reproducible. Your repo must include the casting.yaml and casting.yaml.lock. Judges may re-run Foundry against them to reproduce your deployment.



**RESOURCES**

**FOUNDRY**

- [Quickstart](https://signoz.io/docs/install/docker/)
- [Casting file reference](https://github.com/SigNoz/foundry/blob/main/docs/reference/casting-file.md)
- Concepts: [casting](https://github.com/SigNoz/foundry/blob/main/docs/concepts/casting.md), [moldings](https://github.com/SigNoz/foundry/blob/main/docs/concepts/moldings.md), [patches](https://github.com/SigNoz/foundry/blob/main/docs/concepts/patches.md), [annotations](https://github.com/SigNoz/foundry/blob/main/docs/concepts/annotations.md)
- [MCP server](https://github.com/SigNoz/foundry/blob/main/docs/concepts/mcp-server.md)
- [Examples](https://github.com/SigNoz/foundry/tree/main/docs/examples) (including [compose + MCP](https://github.com/SigNoz/foundry/tree/main/docs/examples/docker/compose-mcp))
- [Intro to Foundry](https://signoz.io/blog/introducing-signoz-foundry)

**SigNoz**

- [Docs](https://signoz.io/docs)
- [MCP server](https://signoz.io/docs/ai/signoz-mcp-server/)
- [Instrumentation](https://signoz.io/docs/instrumentation/overview/) (all languages)
- [Query Builder](https://signoz.io/docs/userguide/query-builder-v5/)
- [Dashboards](https://signoz.io/docs/userguide/manage-dashboards/)
- [Alerts](https://signoz.io/docs/alerts/)
- [Logs](https://signoz.io/docs/logs-management/overview/), [host metrics](https://signoz.io/docs/userguide/hostmetrics/)
- LLM / GenAI monitoring: [OpenAI](https://signoz.io/docs/llm/opentelemetry-openai-monitoring/), [Gemini](https://signoz.io/docs/google-gemini-monitoring/), [LiteLLM](https://signoz.io/docs/litellm-observability/), [Traceloop](https://signoz.io/docs/traceloop/), [Langtrace](https://signoz.io/docs/langtrace/)
- [Service accounts / API keys](https://signoz.io/docs/manage/administrator-guide/iam/service-accounts/), [API reference](https://signoz.io/api-reference/)

**SigNoz MCP RESOURCES**

- [Using SigNoz MCP for the Development and Release Lifecycle](https://signoz.io/blog/signoz-mcp-development-release-lifecycle)
- [Using SigNoz MCP for Dashboard Automation](https://signoz.io/blog/signoz-mcp-dashboard-automation)
- [Using SigNoz MCP for Log and Trace Investigation](https://signoz.io/blog/signoz-mcp-log-trace-investigation)
- [Using SigNoz MCP for Incident Response](https://signoz.io/blog/signoz-mcp-incident-response)
- [Automating the On-Call Lifecycle with SigNoz MCP](https://signoz.io/blog/automating-oncall-lifecycle-signoz-mcp)
- [Full-Circle Observability: monitoring a LangChain agent that queries SigNoz MCP](https://signoz.io/blog/monitoring-langchain-agent-querying-signoz-mcp-server)

**OPENTELEMETRY**

- [Start with OpenTelemetry](https://signoz.io/opentelemetry/)
- [Collector](https://opentelemetry.io/docs/collector/)
- [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Demo app (sample telemetry)](https://github.com/SigNoz/opentelemetry-demo-lite)

**DEVELOP WITH AI (CLAUDE)**

- [SigNoz agent skills](https://github.com/SigNoz/agent-skills): official Claude Code plugin (queries, dashboards, alerts, docs, MCP setup)
- [SigNoz MCP server](https://github.com/SigNoz/signoz-mcp-server)



&nbsp;