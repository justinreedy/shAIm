# shAIm - The terminal client for self hosted AI

A terminal client for self hosted AI models with support for Ollama, saved profiles, conversation persistence, local skill files, file attachments, local tool execution, and background multi-agent swarm tasks. 

## Features

- Chat with locally hosted models through the Ollama `/api/chat` and `/api/tags` endpoints. 
- Save and load connection profiles containing host, port, model, and optional system prompt settings. 
- Manage conversations with create, open, rename, delete, save, and forget flows. 
- Attach text or PDF files to a conversation so extracted content becomes part of the runtime context. 
- Load reusable AI skills from local `.md` files stored in the skills directory. 
- Run local tools from the terminal, including bash commands, Python snippets, and local file read/write actions. 
- Launch background swarm tasks with planner, builder, and reviewer agents backed by a local OpenAI-compatible endpoint. 
- Schedule one-off or recurring swarm jobs and review the results later from the terminal UI. 

## Requirements

- Python 3.10 or newer is recommended.
- A running Ollama-compatible server reachable over HTTP, because the script checks model availability via `/api/tags` and sends chats to `/api/chat`. 
- The Python packages used directly by the script are `requests`, `rich`, `openai`, and the OpenAI Swarm package. 

Example `requirements.txt`:

```txt
requests>=2.31.0
rich>=13.7.0
openai>=1.30.0
git+https://github.com/openai/swarm.git
```

## Installation

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies.
4. Make sure Ollama or another compatible local endpoint is running.

```bash
git clone https://github.com/your-username/shAIm.git
cd shAIm
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python shAIm.py
```

## Configuration

When first running, the script will create a hidden `.shAIm` folder in your home directory. This folder contains `config.json` where you can set profile settings, swarm settings, agent definitions, default skill names, and attachment size limits. The hidden folder also contains conversations, uploads, schedules and skills. We recommend updating the `config.json` file directly or via shAIm immediately after first running the script.  

## Usage

Start the client normally:

```bash
python shAIm.py
```

Start with custom options:

```bash
python shAIm.py --host 192.168.1.159 --port 11434 --model deepseek-r1:8b
python shAIm.py --profile default
python shAIm.py --conversation <conversation_id>
python shAIm.py --new-chat "New project planning"
python shAIm.py --save-as office-server
python shAIm.py --list-profiles
```

## Commands

The built-in help covers model switching, profile management, conversation management, attachments, skills, local tools, swarm tasks, schedules, config inspection, and exit commands. 

### Model and profiles

- `/model` — list available models. 
- `/model <name>` — switch the active model. 
- `/profile` — list saved profiles. 
- `/profile save <name>` — save the current host, port, model, and system prompt as a profile. 
- `/profile load <name>` — load a saved profile. 
- `/profile delete <name>` — delete a saved profile. 
- `/profile default <name>` — set the startup default profile. 

### Conversations

- `/chat list` — list saved conversations. 
- `/chat new [title]` — start a new conversation. 
- `/chat open <id>` — open a saved conversation. 
- `/chat rename <id> <title>` — rename a saved conversation. 
- `/chat delete <id>` — delete a saved conversation. 
- `/chat save` — save the current conversation. 
- `/chat save-as [title]` — save with a specific title. 
- `/chat forget` — stop updating the current conversation on disk. 
- `/history` — show current conversation history. 

### Attachments

- `/attach <path>` — attach a PDF or text file to the current conversation. 
- `/attachments` — list current conversation attachments. 
- `/attach show <id>` — preview extracted attachment text. 
- `/attach drop <id>` — remove an attachment. 

### Skills

- `/skills` — list available skill files. 
- `/skill use <name>` — activate a skill. 
- `/skill drop <name>` — deactivate a skill. 
- `/skill show <name>` — preview a skill file. 
- `/skill clear` — deactivate all skills. 

### Local tools

The script exposes a local toolbox for shell commands, Python snippets, and reading or writing local files. 

- `/tool bash <command>` — run a shell command locally. 
- `/tool python <code>` — run local Python code. 
- `/tool read <path>` — read a local text file. 
- `/tool write <path>` — write a local text file after being prompted for content. 

### Swarm tasks

The script includes planner, builder, and reviewer agents and can execute swarm work in the background while normal chat continues. It also supports scheduled swarm runs and a queue of pending results for later review. 

- `/swarm agents` — list configured agents. 
- `/swarm doctor` — verify local swarm readiness. 
- `/swarm run <task>` — start a background swarm task. 
- `/swarm schedule once` — schedule a one-off task. 
- `/swarm schedule every` — schedule a recurring task in minutes. 
- `/swarm schedules` — list schedules. 
- `/swarm unschedule <id>` — remove a schedule. 
- `/swarm status` — show active swarm status. 
- `/swarm show` — display the next completed swarm result. 

## Project structure

A practical repository layout for GitHub would look like this:

```text
shAIm/
├── shAIm.py
├── README.md
├── requirements.txt
└── skills/
    ├── coding.md
    └── writing.md
```

The running application will also create and use its own working files for saved conversations, uploaded attachments, schedules, and configuration during normal operation:

## Notes

- Swarm support is optional at runtime, but the script includes explicit checks for both the `openai` package and the Swarm package before enabling that path. 
- If the local endpoint is unavailable, the client reports a connection error rather than silently failing. 
- Attachment text may be truncated to fit the configured context limit. 

## License

Copyright (c) 2026 Justinian Pty Ltd
Released under the MIT License (see LICENSE file)
