# Copyright (c) 2026 Justinian Pty Ltd
# Released under the MIT License (see LICENSE file)

#!/usr/bin/env python3

import json
import uuid
import argparse
import threading
import queue
import requests
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich import box

try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except Exception:
    PdfReader = None
    PDF_AVAILABLE = False

try:
    from swarm import Swarm, Agent
    SWARM_AVAILABLE = True
except Exception:
    Swarm = None
    Agent = None
    SWARM_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_CLIENT_AVAILABLE = True
except Exception:
    OpenAI = None
    OPENAI_CLIENT_AVAILABLE = False

console = Console()
APP_NAME = "shAIm"
VERSION = "1.0"

CONFIG_DIR = Path.home() / ".shAIm"
CONFIG_FILE = CONFIG_DIR / "config.json"
SCHEDULES_FILE = CONFIG_DIR / "schedules.json"
CONVERSATIONS_DIR = CONFIG_DIR / "conversations"
SKILLS_DIR = CONFIG_DIR / "skills"
UPLOADS_DIR = CONFIG_DIR / "uploads"
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".yaml", ".yml", ".log", ".ini", ".cfg", ".text"}

DEFAULT_CONFIG = {
    "default_profile": "default",
    "auto_save_conversations": False,
    "default_skill_names": [],
    "attachment_max_chars": 24000,
    "profiles": {
        "default": {
            "host": "192.168.1.1",
            "port": 11434,
            "model": "deepseek-r1:8b",
            "system": None,
        }
    },
    "swarm": {
        "enabled": True,
        "base_url": "http://192.168.1.1:11434/v1",
        "api_key": "ollama",
        "default_model": "deepseek-r1:8b",
        "use_local_only": True,
        "max_turns": 12,
    },
    "agents": {
        "planner": {"provider": "ollama", "model": "deepseek-r1:8b", "instructions": "Break tasks into clear steps, delegate work, and keep scope controlled."},
        "builder": {"provider": "ollama", "model": "deepseek-r1:8b", "instructions": "Implement requested technical work carefully and concretely."},
        "reviewer": {"provider": "ollama", "model": "deepseek-r1:8b", "instructions": "Review outputs, identify defects, and request fixes if needed."},
    },
}


def deep_merge(base, override):
    result = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_storage_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def save_config(config):
    ensure_storage_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def load_config():
    ensure_storage_dirs()
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        merged = deep_merge(DEFAULT_CONFIG, loaded)
        if merged != loaded:
            save_config(merged)
        return merged
    except (json.JSONDecodeError, OSError):
        console.print("[yellow]Warning: config file corrupted, using defaults.[/yellow]")
        return json.loads(json.dumps(DEFAULT_CONFIG))


def load_schedules():
    ensure_storage_dirs()
    if not SCHEDULES_FILE.exists():
        save_schedules([])
        return []
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_schedules(schedules):
    ensure_storage_dirs()
    with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump(schedules, f, indent=2)


def get_profile(config, name):
    return config.get("profiles", {}).get(name)


def upsert_profile(config, name, host, port, model, system=None):
    config.setdefault("profiles", {})[name] = {"host": host, "port": port, "model": model, "system": system}
    save_config(config)


def delete_profile(config, name):
    if name in config.get("profiles", {}):
        del config["profiles"][name]
        if config.get("default_profile") == name:
            remaining = list(config["profiles"].keys())
            config["default_profile"] = remaining[0] if remaining else None
        save_config(config)
        return True
    return False


def list_profiles_table(config):
    table = Table(title="Saved Profiles", box=box.SIMPLE_HEAVY, border_style="dark_violet")
    table.add_column("Profile", style="yellow")
    table.add_column("Host", style="cyan")
    table.add_column("Port", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Default", style="magenta")
    for name, p in config.get("profiles", {}).items():
        table.add_row(name, p.get("host", ""), str(p.get("port", "")), p.get("model", ""), "✓" if config.get("default_profile") == name else "")
    console.print(table)


def slugify(value):
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "chat"))
    parts = [p for p in cleaned.split("-") if p]
    return "-".join(parts[:8]) or "chat"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


class ConversationStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, conversation_id):
        return self.root / f"{conversation_id}.json"

    def create(self, title=None, profile_name="default", model=None, system_prompt=None, active_skills=None):
        cid = str(uuid.uuid4())[:12]
        title = (title or f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}").strip()
        return {"id": cid, "title": title, "slug": slugify(title), "created_at": now_iso(), "updated_at": now_iso(), "profile": profile_name, "model": model, "system_prompt": system_prompt, "active_skills": active_skills or [], "attachments": [], "messages": [], "saved": False}

    def save(self, conversation):
        conversation["updated_at"] = now_iso()
        with open(self.path_for(conversation["id"]), "w", encoding="utf-8") as f:
            json.dump(conversation, f, indent=2, ensure_ascii=False)

    def load(self, conversation_id):
        path = self.path_for(conversation_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("attachments", [])
        data["saved"] = True
        return data

    def delete(self, conversation_id):
        path = self.path_for(conversation_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_all(self):
        items = []
        for path in sorted(self.root.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("attachments", [])
                data["saved"] = True
                items.append(data)
            except Exception:
                continue
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return items


class SkillManager:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_skills(self):
        skills = []
        for path in sorted(self.root.glob("*.md")):
            skills.append({"name": path.stem, "path": path, "size": path.stat().st_size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")})
        return skills

    def read_skill(self, name):
        path = self.root / f"{name}.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def build_system_prompt(self, active_skill_names):
        sections = []
        for name in active_skill_names:
            content = self.read_skill(name)
            if content:
                sections.append(f"## Skill: {name}\n{content.strip()}")
        return "\n\n".join(sections).strip()


class AttachmentManager:
    def __init__(self, root, max_chars=24000):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_chars = max_chars

    def conversation_dir(self, conversation_id):
        path = self.root / conversation_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def extract_text(self, path):
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            if not PDF_AVAILABLE:
                raise RuntimeError("PDF support requires pypdf. Install with: pip install pypdf")
            reader = PdfReader(str(path))
            chunks = []
            for i, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                chunks.append(f"\n--- Page {i} ---\n{page_text.strip()}")
            text = "\n".join(chunks).strip()
        elif suffix in TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            raise RuntimeError(f"Unsupported file type: {suffix or '[no extension]'}")
        truncated = False
        if len(text) > self.max_chars:
            text = text[: self.max_chars]
            truncated = True
        return text, truncated

    def attach_file(self, conversation, source_path):
        src = Path(source_path).expanduser().resolve()
        if not src.exists() or not src.is_file():
            raise RuntimeError(f"File not found: {src}")
        conv_dir = self.conversation_dir(conversation["id"])
        attachment_id = str(uuid.uuid4())[:8]
        dest = conv_dir / f"{attachment_id}_{src.name}"
        dest.write_bytes(src.read_bytes())
        text, truncated = self.extract_text(dest)
        record = {"id": attachment_id, "name": src.name, "source_path": str(src), "stored_path": str(dest), "type": src.suffix.lower() or "unknown", "attached_at": now_iso(), "truncated": truncated, "content": text}
        conversation.setdefault("attachments", []).append(record)
        return record

    def drop_attachment(self, conversation, attachment_id):
        removed = None
        kept = []
        for item in conversation.get("attachments", []):
            if item.get("id") == attachment_id and removed is None:
                removed = item
            else:
                kept.append(item)
        conversation["attachments"] = kept
        if removed:
            try:
                stored = removed.get("stored_path")
                if stored and Path(stored).exists():
                    Path(stored).unlink()
            except Exception:
                pass
        return removed


class LocalToolbox:
    def __init__(self, working_dir=None):
        self.working_dir = Path(working_dir or Path.cwd())

    def describe_tools(self):
        return [
            "tool:bash <command>  - run a shell command locally",
            "tool:python <code>   - run Python locally",
            "tool:read <path>     - read a local text file",
            "tool:write <path>    - write a local text file; content prompted after command",
        ]

    def run_bash(self, command):
        proc = subprocess.run(command, shell=True, cwd=self.working_dir, capture_output=True, text=True, timeout=120)
        return (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")

    def run_python(self, code):
        proc = subprocess.run(["python3", "-c", code], cwd=self.working_dir, capture_output=True, text=True, timeout=120)
        return (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")

    def read_file(self, path):
        file_path = (self.working_dir / path).resolve() if not Path(path).is_absolute() else Path(path)
        return file_path.read_text(encoding="utf-8")

    def write_file(self, path, content):
        file_path = (self.working_dir / path).resolve() if not Path(path).is_absolute() else Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {file_path}"


class OllamaClient:
    def __init__(self, host="localhost", port=11434, model="llama3", timeout=120):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.model = model
        self.timeout = timeout
        self.history = []

    def set_history(self, messages):
        self.history = list(messages or [])

    def list_models(self):
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=10)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []

    def check_connection(self):
        try:
            return requests.get(f"{self.base_url}/api/tags", timeout=5).status_code == 200
        except Exception:
            return False

    def stream_chat(self, user_message, system_prompt=None):
        history = list(self.history)
        if system_prompt and not any(m["role"] == "system" for m in history):
            history.insert(0, {"role": "system", "content": system_prompt})
        history.append({"role": "user", "content": user_message})
        payload = {"model": self.model, "messages": history, "stream": True}
        full_response = ""
        try:
            with requests.post(f"{self.base_url}/api/chat", json=payload, stream=True, timeout=self.timeout) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line.decode("utf-8"))
                    if chunk.get("done"):
                        break
                    content = chunk.get("message", {}).get("content", "")
                    full_response += content
                    yield content
        except requests.exceptions.ConnectionError:
            yield None
            raise ConnectionError(f"Could not connect to Ollama at {self.base_url}. Check the server is running and reachable on your network.")
        finally:
            if full_response:
                self.history = history + [{"role": "assistant", "content": full_response}]


def get_swarm_settings(config):
    return config.get("swarm", {})


def build_local_swarm_client(config):
    settings = get_swarm_settings(config)
    return OpenAI(base_url=settings.get("base_url", "http://localhost:11434/v1"), api_key=settings.get("api_key", "ollama"))


def ensure_swarm_ready(config):
    settings = get_swarm_settings(config)
    if not settings.get("enabled", True):
        return False, "Swarm mode is disabled in config."
    if not SWARM_AVAILABLE:
        return False, "OpenAI Swarm is not installed. Install with: pip install git+https://github.com/openai/swarm.git"
    if not OPENAI_CLIENT_AVAILABLE:
        return False, "The openai Python package is not installed. Install with: pip install openai"
    base_url = settings.get("base_url", "http://localhost:11434/v1")
    health_url = base_url[:-3] if base_url.rstrip("/").endswith("/v1") else base_url.rstrip("/")
    tags_url = health_url + "/api/tags"
    try:
        if requests.get(tags_url, timeout=5).status_code != 200:
            return False, f"Local Ollama endpoint not ready: {tags_url}"
    except Exception as e:
        return False, f"Could not reach local Ollama endpoint: {e}"
    return True, f"Swarm is ready at {base_url}"


def get_agent_specs(config):
    return config.get("agents", {})


def print_swarm_agents(config):
    table = Table(title="Swarm Agents", box=box.SIMPLE_HEAVY, border_style="magenta")
    table.add_column("Name", style="yellow")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Instructions", style="white")
    default_model = get_swarm_settings(config).get("default_model", "llama3")
    for name, spec in get_agent_specs(config).items():
        table.add_row(name, spec.get("provider", "ollama"), spec.get("model", default_model), spec.get("instructions", "")[:80])
    console.print(table)


def run_swarm_task_sync(config, task_text):
    ok, message = ensure_swarm_ready(config)
    if not ok:
        return {"ok": False, "error": message, "messages": [], "final_agent": None, "endpoint": get_swarm_settings(config).get("base_url", "http://localhost:11434/v1")}
    agent_specs = get_agent_specs(config)
    local_client = build_local_swarm_client(config)
    swarm_client = Swarm(client=local_client)
    settings = get_swarm_settings(config)
    default_model = settings.get("default_model", "llama3")
    reviewer = Agent(name="Reviewer", model=agent_specs.get("reviewer", {}).get("model", default_model), instructions=agent_specs.get("reviewer", {}).get("instructions", "Review the completed work, identify issues, and finalize the result."))
    def transfer_to_reviewer(): return reviewer
    builder = Agent(name="Builder", model=agent_specs.get("builder", {}).get("model", default_model), instructions=agent_specs.get("builder", {}).get("instructions", "Implement the requested work carefully and provide concrete outputs."), functions=[transfer_to_reviewer])
    def transfer_to_builder(): return builder
    planner = Agent(name="Planner", model=agent_specs.get("planner", {}).get("model", default_model), instructions=agent_specs.get("planner", {}).get("instructions", "Break down the task, decide the workflow, and hand off implementation."), functions=[transfer_to_builder])
    try:
        try:
            response = swarm_client.run(agent=planner, messages=[{"role": "user", "content": task_text}], max_turns=settings.get("max_turns", 12), execute_tools=True, stream=False)
        except TypeError:
            response = swarm_client.run(agent=planner, messages=[{"role": "user", "content": task_text}])
    except Exception as e:
        return {"ok": False, "error": str(e), "messages": [], "final_agent": None, "endpoint": settings.get("base_url", "http://localhost:11434/v1")}
    final_agent = getattr(response, "agent", None)
    response_messages = getattr(response, "messages", None) or (response.get("messages", []) if isinstance(response, dict) else [])
    return {"ok": True, "error": None, "messages": response_messages, "final_agent": getattr(final_agent, "name", final_agent) if final_agent is not None else None, "endpoint": settings.get("base_url", "http://localhost:11434/v1")}


class SwarmTaskManager:
    def __init__(self, schedules=None):
        self.lock = threading.Lock()
        self.events = queue.Queue()
        self.active_task = None
        self.pending_results = []
        self.schedules = schedules or []

    def has_active_task(self):
        with self.lock:
            return self.active_task is not None

    def has_pending_result(self):
        with self.lock:
            return len(self.pending_results) > 0

    def consume_pending_result(self):
        with self.lock:
            return self.pending_results.pop(0) if self.pending_results else None

    def get_pending_count(self):
        with self.lock:
            return len(self.pending_results)

    def get_active_summary(self):
        with self.lock:
            return dict(self.active_task) if self.active_task else None

    def pop_events(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        return items

    def start_now(self, config, task_text, source="manual", schedule_id=None):
        with self.lock:
            if self.active_task is not None:
                return None
            task_id = str(uuid.uuid4())[:8]
            self.active_task = {"id": task_id, "task": task_text, "source": source, "schedule_id": schedule_id, "started_at": now_iso()}
        threading.Thread(target=self._worker, args=(task_id, config, task_text, source, schedule_id), daemon=True).start()
        return task_id

    def _worker(self, task_id, config, task_text, source, schedule_id):
        record = {"id": task_id, "task": task_text, "source": source, "schedule_id": schedule_id, "completed_at": now_iso(), "result": run_swarm_task_sync(config, task_text)}
        with self.lock:
            if self.active_task and self.active_task.get("id") == task_id:
                self.active_task = None
            self.pending_results.append(record)
        self.events.put({"type": "swarm_complete", "task_id": task_id, "source": source, "schedule_id": schedule_id})

    def list_schedules(self):
        with self.lock:
            return [dict(s) for s in self.schedules]

    def add_schedule(self, schedule):
        with self.lock:
            self.schedules.append(schedule)
            save_schedules(self.schedules)

    def remove_schedule(self, schedule_id):
        with self.lock:
            original = len(self.schedules)
            self.schedules = [s for s in self.schedules if s.get("id") != schedule_id]
            changed = len(self.schedules) != original
            if changed:
                save_schedules(self.schedules)
            return changed

    def check_due_schedules(self, config):
        now = datetime.now()
        launched = []
        with self.lock:
            if self.active_task is not None:
                return launched
            for schedule in self.schedules:
                if not schedule.get("enabled", True):
                    continue
                next_run_str = schedule.get("next_run")
                if not next_run_str:
                    continue
                try:
                    next_run = datetime.fromisoformat(next_run_str)
                except ValueError:
                    continue
                if next_run <= now:
                    task_id = str(uuid.uuid4())[:8]
                    self.active_task = {"id": task_id, "task": schedule["task"], "source": "schedule", "schedule_id": schedule["id"], "started_at": now_iso()}
                    schedule["last_run"] = now_iso()
                    if schedule["type"] == "once":
                        schedule["enabled"] = False
                        schedule["next_run"] = None
                    elif schedule["type"] == "interval":
                        schedule["next_run"] = (now + timedelta(minutes=int(schedule.get("interval_minutes", 0)))).isoformat(timespec="seconds")
                    save_schedules(self.schedules)
                    threading.Thread(target=self._worker, args=(task_id, config, schedule["task"], "schedule", schedule["id"]), daemon=True).start()
                    launched.append({"task_id": task_id, "schedule": dict(schedule)})
                    break
        return launched


def create_once_schedule(task_text, run_at_iso):
    return {"id": str(uuid.uuid4())[:8], "type": "once", "task": task_text, "run_at": run_at_iso, "next_run": run_at_iso, "last_run": None, "interval_minutes": None, "enabled": True, "created_at": now_iso()}


def create_interval_schedule(task_text, interval_minutes, start_at_iso=None):
    start_at = datetime.fromisoformat(start_at_iso) if start_at_iso else datetime.now() + timedelta(minutes=interval_minutes)
    ts = start_at.isoformat(timespec="seconds")
    return {"id": str(uuid.uuid4())[:8], "type": "interval", "task": task_text, "run_at": ts, "next_run": ts, "last_run": None, "interval_minutes": int(interval_minutes), "enabled": True, "created_at": now_iso()}


def render_swarm_result(record):
    result = record["result"]
    console.print()
    console.print(Text("Swarm", style="bold magenta"))
    console.print(f"[dim]Task ID:[/dim] {record['id']}")
    console.print(f"[dim]Source:[/dim] {record.get('source', 'manual')}")
    if record.get("schedule_id"):
        console.print(f"[dim]Schedule ID:[/dim] {record['schedule_id']}")
    console.print(f"[dim]Completed:[/dim] {record['completed_at']}")
    console.print(f"[dim]Endpoint:[/dim] {result.get('endpoint', '')}")
    console.print(f"[dim]Task:[/dim] {record['task']}")
    if not result.get("ok"):
        console.print(f"[bold red]Swarm error:[/bold red] {result.get('error', 'Unknown error')}")
        console.print()
        console.print(Rule(style="grey37"))
        return
    console.print("[bold green]Swarm completed.[/bold green]")
    if result.get("final_agent"):
        console.print(f"[dim]Final agent:[/dim] {result['final_agent']}")
    for msg in result.get("messages", []):
        sender = msg.get("sender") or msg.get("role", "assistant")
        content = msg.get("content", "")
        if content:
            console.print()
            console.print(Text(str(sender), style="bold cyan" if str(sender).lower() == "user" else "bold green"))
            console.print(content, soft_wrap=True)
    console.print()
    console.print(Rule(style="grey37"))


def parse_iso_datetime(value):
    return datetime.fromisoformat(value)


def print_schedule_table(manager):
    schedules = manager.list_schedules()
    if not schedules:
        console.print("[dim]No scheduled swarm tasks.[/dim]")
        return
    table = Table(title="Scheduled Swarm Tasks", box=box.SIMPLE_HEAVY, border_style="magenta")
    table.add_column("ID", style="yellow")
    table.add_column("Type", style="cyan")
    table.add_column("Next Run", style="green")
    table.add_column("Enabled", style="magenta")
    table.add_column("Task", style="white")
    for item in schedules:
        table.add_row(item.get("id", ""), item.get("type", ""), str(item.get("next_run", "")), "yes" if item.get("enabled", True) else "no", item.get("task", "")[:80])
    console.print(table)


def print_conversation_table(store, active_id=None):
    rows = store.list_all()
    if not rows:
        console.print("[dim]No saved conversations.[/dim]")
        return
    table = Table(title="Saved Conversations", box=box.SIMPLE_HEAVY, border_style="blue")
    table.add_column("ID", style="yellow")
    table.add_column("Title", style="white")
    table.add_column("Profile", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Updated", style="magenta")
    table.add_column("Active", style="bright_white")
    for row in rows:
        table.add_row(row.get("id", ""), row.get("title", ""), row.get("profile", ""), row.get("model", ""), row.get("updated_at", ""), "✓" if row.get("id") == active_id else "")
    console.print(table)


def print_skills_table(skill_manager, active_skill_names):
    skills = skill_manager.list_skills()
    if not skills:
        console.print(f"[dim]No skills found in {SKILLS_DIR}. Add .md files there to use skills.[/dim]")
        return
    table = Table(title="AI Skills", box=box.SIMPLE_HEAVY, border_style="green")
    table.add_column("Name", style="yellow")
    table.add_column("Updated", style="cyan")
    table.add_column("Active", style="magenta")
    for skill in skills:
        table.add_row(skill["name"], skill["updated_at"], "✓" if skill["name"] in active_skill_names else "")
    console.print(table)


def print_attachments_table(conversation):
    attachments = conversation.get("attachments", [])
    if not attachments:
        console.print("[dim]No attachments in this conversation.[/dim]")
        return
    table = Table(title="Conversation Attachments", box=box.SIMPLE_HEAVY, border_style="cyan")
    table.add_column("ID", style="yellow")
    table.add_column("Name", style="white")
    table.add_column("Type", style="green")
    table.add_column("Attached", style="magenta")
    table.add_column("Truncated", style="cyan")
    for item in attachments:
        table.add_row(item.get("id", ""), item.get("name", ""), item.get("type", ""), item.get("attached_at", ""), "yes" if item.get("truncated") else "no")
    console.print(table)


def build_runtime_system_prompt(state, config):
    parts = []
    if state.get("base_system"):
        parts.append(state["base_system"].strip())
    skill_prompt = state["skill_manager"].build_system_prompt(state["active_skills"])
    if skill_prompt:
        parts.append("The following local skills are active. Follow them carefully.\n\n" + skill_prompt)
    attachments = state["conversation"].get("attachments", []) if state.get("conversation") else []
    if attachments:
        sections = []
        for item in attachments:
            sections.append(f"## Attachment: {item.get('name')}\nAttachment ID: {item.get('id')}\nType: {item.get('type')}\nAttached at: {item.get('attached_at')}\nTruncated: {'yes' if item.get('truncated') else 'no'}\n\nContent:\n{item.get('content', '').strip()}")
        parts.append("The following file attachments are part of the conversation context. Use them when relevant.\n\n" + "\n\n".join(sections))
    tool_lines = state["toolbox"].describe_tools()
    parts.append("The following local user-invoked tools are available in this app:\n" + "\n".join(f"- {line}" for line in tool_lines) + "\nOnly describe or use them when the user explicitly invokes the corresponding /tool command.")
    return "\n\n".join([p for p in parts if p]).strip() or None


def sync_client_from_conversation(client, conversation, runtime_system_prompt):
    messages = []
    if runtime_system_prompt:
        messages.append({"role": "system", "content": runtime_system_prompt})
    messages.extend(conversation.get("messages", []))
    client.set_history(messages)


def is_explicitly_saved(conversation):
    return bool(conversation and conversation.get("saved", False))


def save_active_conversation(state):
    conversation = state.get("conversation")
    if not is_explicitly_saved(conversation):
        return False
    conversation["active_skills"] = list(state["active_skills"])
    conversation["model"] = state["client"].model
    conversation["system_prompt"] = state.get("base_system")
    state["conversation_store"].save(conversation)
    return True


def save_current_conversation_explicit(state, title=None):
    conversation = state.get("conversation")
    if not conversation:
        return False
    if title:
        conversation["title"] = title.strip()
        conversation["slug"] = slugify(conversation["title"])
    conversation["saved"] = True
    return save_active_conversation(state)


def print_banner(client, profile_name, state):
    title = Text(f"            [ {APP_NAME} · v{VERSION} ]", style="bold white on dark_violet")
    subtitle = Text(f"[ The terminal client for self hosted AI ]", style="dim")
    banner = Table.grid(padding=(0, 1))
    banner.add_row(title)
    banner.add_row(subtitle)
    console.print(Panel(banner, border_style="dark_violet", box=box.ROUNDED, expand=False))
    connected = client.check_connection()
    status_style = "bold green" if connected else "bold red"
    status_text = "● Connected" if connected else "● Disconnected"
    info = Table.grid(padding=(0, 2))
    info.add_row("[dim]Profile:[/dim]", f"[bold]{profile_name}[/bold]")
    info.add_row("[dim]Server:[/dim]", f"[cyan]{client.base_url}[/cyan]")
    info.add_row("[dim]Model:[/dim]", f"[yellow]{client.model}[/yellow]")
    info.add_row("[dim]Status:[/dim]", f"[{status_style}]{status_text}[/{status_style}]")
    console.print(info)
    console.print(Rule(style="grey37"))
    console.print("Type /help for a list of commands")


def print_help_models(client):
    models = client.list_models()
    if not models:
        console.print("[red]No models found or server unreachable.[/red]")
        return
    table = Table(title="Available Models", box=box.SIMPLE_HEAVY, border_style="dark_violet")
    table.add_column("Model", style="yellow")
    for m in models:
        table.add_row(m)
    console.print(table)


def print_help():
    console.print(
        "[bold]\n/model <name>[/bold] switch active model\n"
        "[bold]/model[/bold] list available models\n\n"
        "[bold]/profile[/bold] list saved profiles\n"
        "[bold]/profile save <name>[/bold] save current server/model as profile\n"
        "[bold]/profile load <name>[/bold] load a saved profile\n"
        "[bold]/profile delete <name>[/bold] delete a saved profile\n"
        "[bold]/profile default <name>[/bold] set default profile at startup\n\n"
        "[bold]/chat list[/bold] list saved conversations\n"
        "[bold]/chat new [title][/bold] start a new conversation\n"
        "[bold]/chat open <id>[/bold] open a saved conversation\n"
        "[bold]/chat rename <id> <title>[/bold] rename a saved conversation\n"
        "[bold]/chat delete <id>[/bold] delete a saved conversation\n"
        "[bold]/chat save[/bold] save the current conversation\n"
        "[bold]/chat save-as [title][/bold] save current conversation with optional new title\n"
        "[bold]/chat forget[/bold] stop updating the current conversation on disk\n"
        "[bold]/history[/bold] show current conversation history\n\n"
        "[bold]/attach <path>[/bold] attach a PDF or text file to current conversation\n"
        "[bold]/attachments[/bold] list attached files in current conversation\n"
        "[bold]/attach show <id>[/bold] preview extracted attachment text\n"
        "[bold]/attach drop <id>[/bold] remove an attachment\n\n"
        "[bold]/skills[/bold] list available .md skills\n"
        "[bold]/skill use <name>[/bold] activate a skill\n"
        "[bold]/skill drop <name>[/bold] deactivate a skill\n"
        "[bold]/skill show <name>[/bold] preview a skill file\n"
        "[bold]/skill clear[/bold] deactivate all skills\n\n"
        "[bold]/tool bash <command>[/bold] run a shell command locally\n"
        "[bold]/tool python <code>[/bold] run local Python code\n"
        "[bold]/tool read <path>[/bold] read a local text file\n"
        "[bold]/tool write <path>[/bold] write a local text file after prompt\n\n"
        "[bold]/swarm agents[/bold] list configured agents\n"
        "[bold]/swarm doctor[/bold] check local swarm readiness\n"
        "[bold]/swarm run <task>[/bold] start a local multi-agent task in the background\n"
        "[bold]/swarm schedule once[/bold] create a one-off future task\n"
        "[bold]/swarm schedule every[/bold] create a recurring task in minutes\n"
        "[bold]/swarm schedules[/bold] list schedules\n"
        "[bold]/swarm unschedule <id>[/bold] remove a schedule\n"
        "[bold]/swarm status[/bold] show active swarm task status\n"
        "[bold]/swarm show[/bold] display a completed swarm result waiting for review\n\n"
        "[bold]/config[/bold] show raw config file contents\n"
        "[bold]/clear[/bold] clear screen, keep current conversation\n"
        "[bold]/exit[/bold] quit the client"
    )


def check_background_events(state, config):
    manager = state["swarm_manager"]
    for launch in manager.check_due_schedules(config):
        sched = launch["schedule"]
        console.print()
        console.print(f"[bold magenta]Scheduled swarm task started.[/bold magenta] Task ID: {launch['task_id']}")
        console.print(f"[dim]Schedule ID:[/dim] {sched.get('id')} · [dim]Task:[/dim] {sched.get('task')}")
    for event in manager.pop_events():
        if event.get("type") == "swarm_complete":
            console.print()
            console.print("[bold magenta]Your swarm task is complete! Would you like to receive the output now?[/bold magenta]")
            if Confirm.ask("[bold cyan]Show output now[/bold cyan]", default=True):
                record = manager.consume_pending_result()
                if record:
                    render_swarm_result(record)
            else:
                console.print("[dim]Swarm output is waiting. Use /swarm show when you're ready.[/dim]")
                console.print(Rule(style="grey37"))


def start_new_conversation(state, title=None):
    convo = state["conversation_store"].create(title=title, profile_name=state["profile_name"], model=state["client"].model, system_prompt=state.get("base_system"), active_skills=state.get("active_skills", []))
    state["conversation"] = convo
    sync_client_from_conversation(state["client"], convo, build_runtime_system_prompt(state, state["config"]))
    return convo


def open_conversation(state, conversation_id):
    convo = state["conversation_store"].load(conversation_id)
    if not convo:
        return False
    state["conversation"] = convo
    state["active_skills"] = convo.get("active_skills", [])
    sync_client_from_conversation(state["client"], convo, build_runtime_system_prompt(state, state["config"]))
    return True


def handle_tool_command(value, state):
    if not value:
        console.print("[red]Usage: /tool bash <command> | /tool python <code> | /tool read <path> | /tool write <path>[/red]")
        return
    parts = value.split(maxsplit=1)
    action = parts[0].lower()
    payload = parts[1] if len(parts) > 1 else ""
    toolbox = state["toolbox"]
    try:
        if action == "bash":
            output = toolbox.run_bash(payload)
        elif action == "python":
            output = toolbox.run_python(payload)
        elif action == "read":
            output = toolbox.read_file(payload)
        elif action == "write":
            output = toolbox.write_file(payload, Prompt.ask("Content"))
        else:
            console.print(f"[red]Unknown tool action:[/red] {action}")
            return
        console.print(Panel((output or "[no output]")[:12000], title=f"Tool: {action}", border_style="green"))
    except Exception as e:
        console.print(f"[bold red]Tool error:[/bold red] {e}")


def handle_attachment_command(parts, state, client, config):
    attachment_manager = state["attachment_manager"]
    if len(parts) == 1:
        console.print("[dim]Usage: /attach <path> | /attach show <id> | /attach drop <id>[/dim]")
        return
    payload = parts[1].strip()
    if payload.lower().startswith("show "):
        attachment_id = payload.split(maxsplit=1)[1].strip()
        item = next((a for a in state["conversation"].get("attachments", []) if a.get("id") == attachment_id), None)
        if not item:
            console.print(f"[red]No such attachment:[/red] {attachment_id}")
            return
        console.print(Panel(item.get("content", "")[:12000] or "[no content]", title=f"Attachment: {item.get('name')}", border_style="cyan"))
        return
    if payload.lower().startswith("drop "):
        attachment_id = payload.split(maxsplit=1)[1].strip()
        removed = attachment_manager.drop_attachment(state["conversation"], attachment_id)
        if not removed:
            console.print(f"[red]No such attachment:[/red] {attachment_id}")
            return
        sync_client_from_conversation(client, state["conversation"], build_runtime_system_prompt(state, config))
        save_active_conversation(state)
        console.print(f"[bold green]Removed attachment:[/bold green] {removed['name']} ({attachment_id})")
        return
    try:
        record = attachment_manager.attach_file(state["conversation"], payload)
        sync_client_from_conversation(client, state["conversation"], build_runtime_system_prompt(state, config))
        save_active_conversation(state)
        console.print(f"[bold green]Attached file:[/bold green] {record['name']} ({record['id']})")
        if record.get("truncated"):
            console.print("[yellow]Attachment content was truncated to fit the configured context limit.[/yellow]")
        if not is_explicitly_saved(state.get("conversation")):
            console.print("[dim]This conversation is unsaved. The attachment will be lost on exit unless you run /chat save.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Attach error:[/bold red] {e}")


def handle_command(cmd, client, config, state):
    raw = cmd.strip()
    parts = raw.split(maxsplit=1)
    base = parts[0].lower()
    manager = state["swarm_manager"]
    if base in ("/exit", "/quit", "/q"):
        console.print("\n[bold magenta]Goodbye![/bold magenta]")
        return True
    if base == "/clear":
        console.clear()
        print_banner(client, state["profile_name"], state)
    elif base == "/history":
        messages = state["conversation"].get("messages", []) if state.get("conversation") else []
        if not messages:
            console.print("[dim]No conversation history yet.[/dim]")
        for msg in messages:
            role = msg.get("role", "unknown")
            style = {"user": "cyan", "assistant": "green", "system": "dim", "tool": "yellow"}.get(role, "white")
            console.print(f"[{style}]{role}:[/{style}] {msg.get('content', '')[:400]}")
    elif base == "/model":
        if len(parts) == 2:
            client.model = parts[1].strip()
            state["conversation"]["model"] = client.model
            save_active_conversation(state)
            console.print(f"[bold green]Switched model to:[/bold green] {client.model}")
        else:
            print_help_models(client)
    elif base == "/chat":
        if len(parts) == 1:
            print_conversation_table(state["conversation_store"], state["conversation"]["id"] if is_explicitly_saved(state.get("conversation")) else None)
            console.print("[dim]Usage: /chat list | /chat new [title] | /chat open <id> | /chat rename <id> <title> | /chat delete <id> | /chat save | /chat save-as [title] | /chat forget[/dim]")
        else:
            sub = parts[1].split(maxsplit=2)
            action = sub[0].lower()
            if action == "list":
                print_conversation_table(state["conversation_store"], state["conversation"]["id"] if is_explicitly_saved(state.get("conversation")) else None)
            elif action == "new":
                if state["conversation"].get("messages") and not is_explicitly_saved(state.get("conversation")):
                    if not Confirm.ask("Current conversation is unsaved. Start a new one and discard it from memory?", default=False):
                        return False
                convo = start_new_conversation(state, title=sub[1] if len(sub) > 1 else None)
                console.print(f"[bold green]Started new conversation:[/bold green] {convo['title']} ({convo['id']})")
            elif action == "open":
                if len(sub) < 2:
                    console.print("[red]Usage: /chat open <id>[/red]")
                elif open_conversation(state, sub[1]):
                    console.print(f"[bold green]Opened conversation:[/bold green] {state['conversation']['title']}")
                else:
                    console.print(f"[red]No such saved conversation:[/red] {sub[1]}")
            elif action == "rename":
                if len(sub) < 3:
                    console.print("[red]Usage: /chat rename <id> <title>[/red]")
                else:
                    convo = state["conversation_store"].load(sub[1])
                    if not convo:
                        console.print(f"[red]No such saved conversation:[/red] {sub[1]}")
                    else:
                        convo["title"] = sub[2].strip()
                        convo["slug"] = slugify(convo["title"])
                        state["conversation_store"].save(convo)
                        if state["conversation"]["id"] == convo["id"]:
                            state["conversation"] = convo
                        console.print(f"[bold green]Renamed conversation:[/bold green] {convo['title']}")
            elif action == "delete":
                if len(sub) < 2:
                    console.print("[red]Usage: /chat delete <id>[/red]")
                else:
                    target_id = sub[1]
                    if target_id == state["conversation"]["id"] and not Confirm.ask(f"Delete saved conversation {target_id}?", default=False):
                        return False
                    if state["conversation_store"].delete(target_id):
                        console.print(f"[bold green]Deleted conversation:[/bold green] {target_id}")
                        if target_id == state["conversation"]["id"]:
                            start_new_conversation(state)
                            console.print(f"[dim]Started replacement conversation {state['conversation']['id']}[/dim]")
                    else:
                        console.print(f"[red]No such saved conversation:[/red] {target_id}")
            elif action == "save":
                save_current_conversation_explicit(state)
                console.print(f"[bold green]Saved conversation:[/bold green] {state['conversation']['title']} ({state['conversation']['id']})")
            elif action == "save-as":
                save_current_conversation_explicit(state, title=sub[1] if len(sub) > 1 else None)
                console.print(f"[bold green]Saved conversation:[/bold green] {state['conversation']['title']} ({state['conversation']['id']})")
            elif action == "forget":
                was_saved = is_explicitly_saved(state.get("conversation"))
                state["conversation"]["saved"] = False
                if was_saved:
                    console.print("[bold green]Current conversation will no longer be updated on disk.[/bold green]")
                    console.print("[dim]Any already-saved file remains until you delete it with /chat delete <id>.[/dim]")
                else:
                    console.print("[bold green]Current conversation remains unsaved.[/bold green]")
            else:
                console.print("[red]Unknown /chat action.[/red]")
    elif base == "/attach":
        handle_attachment_command(parts, state, client, config)
    elif base == "/attachments":
        print_attachments_table(state["conversation"])
    elif base == "/skills":
        print_skills_table(state["skill_manager"], state["active_skills"])
    elif base == "/skill":
        if len(parts) == 1:
            console.print("[dim]Usage: /skill use <name> | /skill drop <name> | /skill show <name> | /skill clear[/dim]")
        else:
            sub = parts[1].split(maxsplit=1)
            action = sub[0].lower()
            name = sub[1].strip() if len(sub) > 1 else None
            if action == "use":
                if not name:
                    console.print("[red]Usage: /skill use <name>[/red]")
                elif not state["skill_manager"].read_skill(name):
                    console.print(f"[red]Skill not found:[/red] {name} (expected {SKILLS_DIR / (name + '.md')})")
                elif name not in state["active_skills"]:
                    state["active_skills"].append(name)
                    state["conversation"]["active_skills"] = list(state["active_skills"])
                    sync_client_from_conversation(client, state["conversation"], build_runtime_system_prompt(state, config))
                    save_active_conversation(state)
                    console.print(f"[bold green]Activated skill:[/bold green] {name}")
            elif action == "drop":
                if not name:
                    console.print("[red]Usage: /skill drop <name>[/red]")
                elif name in state["active_skills"]:
                    state["active_skills"].remove(name)
                    state["conversation"]["active_skills"] = list(state["active_skills"])
                    sync_client_from_conversation(client, state["conversation"], build_runtime_system_prompt(state, config))
                    save_active_conversation(state)
                    console.print(f"[bold green]Deactivated skill:[/bold green] {name}")
                else:
                    console.print(f"[yellow]Skill not active:[/yellow] {name}")
            elif action == "show":
                content = state["skill_manager"].read_skill(name) if name else None
                if content is None:
                    console.print(f"[red]Skill not found:[/red] {name}")
                else:
                    console.print(Panel(content[:12000], title=f"Skill: {name}", border_style="green"))
            elif action == "clear":
                state["active_skills"] = []
                state["conversation"]["active_skills"] = []
                sync_client_from_conversation(client, state["conversation"], build_runtime_system_prompt(state, config))
                save_active_conversation(state)
                console.print("[bold green]Cleared active skills.[/bold green]")
            else:
                console.print("[red]Unknown /skill action.[/red]")
    elif base == "/tool":
        handle_tool_command(parts[1] if len(parts) > 1 else "", state)
    elif base == "/profile":
        if len(parts) == 1:
            list_profiles_table(config)
            console.print("[dim]Usage: /profile save <name> | /profile load <name> | /profile delete <name> | /profile default <name>[/dim]")
        else:
            sub = parts[1].split(maxsplit=1)
            action = sub[0].lower()
            name = sub[1].strip() if len(sub) > 1 else None
            if action == "save":
                upsert_profile(config, name or state["profile_name"], client.host, client.port, client.model, state.get("base_system"))
                console.print(f"[bold green]Saved profile:[/bold green] {name or state['profile_name']} ({client.host}:{client.port}, {client.model})")
            elif action == "load":
                if not name:
                    console.print("[red]Usage: /profile load <name>[/red]")
                else:
                    p = get_profile(config, name)
                    if not p:
                        console.print(f"[red]No such profile:[/red] {name}")
                    else:
                        client.host = p["host"]
                        client.port = p["port"]
                        client.base_url = f"http://{p['host']}:{p['port']}"
                        client.model = p["model"]
                        state["profile_name"] = name
                        state["base_system"] = p.get("system")
                        state["conversation"]["profile"] = name
                        state["conversation"]["model"] = client.model
                        sync_client_from_conversation(client, state["conversation"], build_runtime_system_prompt(state, config))
                        save_active_conversation(state)
                        console.print(f"[bold green]Loaded profile:[/bold green] {name}")
                        console.clear()
                        print_banner(client, name, state)
            elif action == "delete":
                if not name:
                    console.print("[red]Usage: /profile delete <name>[/red]")
                elif delete_profile(config, name):
                    console.print(f"[bold green]Deleted profile:[/bold green] {name}")
                else:
                    console.print(f"[red]No such profile:[/red] {name}")
            elif action == "default":
                if not name or name not in config.get("profiles", {}):
                    console.print(f"[red]No such profile:[/red] {name}")
                else:
                    config["default_profile"] = name
                    save_config(config)
                    console.print(f"[bold green]Default profile set to:[/bold green] {name}")
            else:
                console.print("[red]Unknown /profile action.[/red]")
    elif base == "/config":
        console.print(f"[dim]Config file:[/dim] {CONFIG_FILE}")
        console.print_json(data=config)
    elif base == "/swarm":
        if len(parts) == 1:
            console.print("[bold]\n/swarm agents[/bold] list configured agents\n[bold]/swarm doctor[/bold] check local Swarm/Ollama readiness\n[bold]/swarm run <task>[/bold] start a multi-agent task in the background\n[bold]/swarm schedule once[/bold] create a one-off task\n[bold]/swarm schedule every[/bold] create a recurring task\n[bold]/swarm schedules[/bold] list schedules\n[bold]/swarm unschedule <id>[/bold] remove a schedule\n[bold]/swarm status[/bold] show active swarm task status\n[bold]/swarm show[/bold] show a completed swarm result waiting for review")
        else:
            sub = parts[1].split(maxsplit=1)
            action = sub[0].lower()
            value = sub[1].strip() if len(sub) > 1 else None
            if action == "agents":
                print_swarm_agents(config)
            elif action == "doctor":
                ok, message = ensure_swarm_ready(config)
                style = "bold green" if ok else "bold red"
                console.print(f"[{style}]{message}[/{style}]")
            elif action == "run":
                if not value:
                    console.print("[red]Usage: /swarm run <task description>[/red]")
                elif manager.has_active_task():
                    active = manager.get_active_summary()
                    console.print(f"[yellow]A swarm task is already running (Task ID {active['id']}).[/yellow]")
                else:
                    task_id = manager.start_now(config, value, source="manual")
                    console.print(f"[bold magenta]Swarm task started in background.[/bold magenta] Task ID: {task_id}")
                    console.print("[dim]You can continue chatting while it runs.[/dim]")
            elif action == "schedule":
                if not value:
                    console.print("[red]Usage: /swarm schedule once | /swarm schedule every[/red]")
                else:
                    sched_type = value.split(maxsplit=1)[0].lower()
                    if sched_type == "once":
                        run_at = Prompt.ask("Run at (ISO format, e.g. 2026-08-15T09:30:00)")
                        task_text = Prompt.ask("Task")
                        try:
                            dt = parse_iso_datetime(run_at)
                            if dt <= datetime.now():
                                console.print("[red]Run time must be in the future.[/red]")
                            else:
                                schedule = create_once_schedule(task_text, dt.isoformat(timespec="seconds"))
                                manager.add_schedule(schedule)
                                console.print(f"[bold green]Scheduled one-off swarm task.[/bold green] ID: {schedule['id']}")
                        except ValueError:
                            console.print("[red]Invalid datetime format. Use ISO format like 2026-08-15T09:30:00[/red]")
                    elif sched_type == "every":
                        minutes = Prompt.ask("Interval in minutes")
                        start_at = Prompt.ask("First run at (optional ISO datetime, leave blank for now + interval)", default="")
                        task_text = Prompt.ask("Task")
                        try:
                            interval_minutes = int(minutes)
                            if interval_minutes <= 0:
                                console.print("[red]Interval must be greater than zero.[/red]")
                            else:
                                start_at_value = None
                                if start_at.strip():
                                    dt = parse_iso_datetime(start_at.strip())
                                    if dt <= datetime.now():
                                        console.print("[red]First run time must be in the future.[/red]")
                                        return False
                                    start_at_value = dt.isoformat(timespec="seconds")
                                schedule = create_interval_schedule(task_text, interval_minutes, start_at_value)
                                manager.add_schedule(schedule)
                                console.print(f"[bold green]Scheduled recurring swarm task.[/bold green] ID: {schedule['id']}")
                        except ValueError:
                            console.print("[red]Interval must be a whole number of minutes.[/red]")
                    else:
                        console.print("[red]Usage: /swarm schedule once | /swarm schedule every[/red]")
            elif action == "schedules":
                print_schedule_table(manager)
            elif action == "unschedule":
                if not value:
                    console.print("[red]Usage: /swarm unschedule <id>[/red]")
                elif manager.remove_schedule(value):
                    console.print(f"[bold green]Removed schedule:[/bold green] {value}")
                else:
                    console.print(f"[red]No such schedule:[/red] {value}")
            elif action == "status":
                active = manager.get_active_summary()
                if active:
                    console.print(f"[bold magenta]Swarm task running.[/bold magenta] Task ID: {active['id']}")
                    console.print(f"[dim]Started:[/dim] {active['started_at']}")
                    console.print(f"[dim]Source:[/dim] {active.get('source', 'manual')}")
                    if active.get("schedule_id"):
                        console.print(f"[dim]Schedule ID:[/dim] {active['schedule_id']}")
                    console.print(f"[dim]Task:[/dim] {active['task']}")
                elif manager.has_pending_result():
                    console.print(f"[bold green]{manager.get_pending_count()} completed swarm result(s) waiting.[/bold green]")
                    console.print("[dim]Use /swarm show to display the next one.[/dim]")
                else:
                    console.print("[dim]No active swarm task.[/dim]")
            elif action == "show":
                record = manager.consume_pending_result()
                if record:
                    render_swarm_result(record)
                else:
                    console.print("[dim]No completed swarm result is waiting.[/dim]")
            else:
                console.print(f"[red]Unknown /swarm action:[/red] {action}")
    elif base == "/help":
        print_help()
    else:
        console.print(f"[red]Unknown command:[/red] {base}")
    return False


def chat_loop(client, config, profile_name, conversation_id=None, conversation_title=None, base_system=None):
    state = {
        "profile_name": profile_name,
        "swarm_manager": SwarmTaskManager(load_schedules()),
        "conversation_store": ConversationStore(CONVERSATIONS_DIR),
        "skill_manager": SkillManager(SKILLS_DIR),
        "attachment_manager": AttachmentManager(UPLOADS_DIR, max_chars=int(config.get("attachment_max_chars", 24000))),
        "toolbox": LocalToolbox(),
        "client": client,
        "config": config,
        "base_system": base_system,
        "active_skills": list(config.get("default_skill_names", [])),
        "conversation": None,
    }
    if conversation_id and state["conversation_store"].load(conversation_id):
        state["conversation"] = state["conversation_store"].load(conversation_id)
        state["active_skills"] = state["conversation"].get("active_skills", state["active_skills"])
    else:
        state["conversation"] = state["conversation_store"].create(title=conversation_title, profile_name=profile_name, model=client.model, system_prompt=base_system, active_skills=state["active_skills"])
    sync_client_from_conversation(client, state["conversation"], build_runtime_system_prompt(state, config))
    print_banner(client, profile_name, state)
    while True:
        check_background_events(state, config)
        try:
            user_input = Prompt.ask("[bold cyan]\nYou[/bold cyan]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[bold magenta]Goodbye![/bold magenta]")
            break
        check_background_events(state, config)
        if not user_input.strip():
            continue
        if user_input.startswith("/"):
            if handle_command(user_input, client, config, state):
                break
            continue
        state["conversation"]["messages"].append({"role": "user", "content": user_input})
        save_active_conversation(state)
        console.print()
        console.print(Text("Assistant", style="bold green"))
        try:
            full_response = ""
            runtime_system_prompt = build_runtime_system_prompt(state, config)
            sync_client_from_conversation(client, state["conversation"], runtime_system_prompt)
            for chunk in client.stream_chat(user_input, system_prompt=runtime_system_prompt):
                if chunk is not None:
                    full_response += chunk
                    console.print(chunk, end="", soft_wrap=True)
            if full_response:
                state["conversation"]["messages"].append({"role": "assistant", "content": full_response})
                save_active_conversation(state)
            console.print()
        except ConnectionError as e:
            state["conversation"]["messages"].pop()
            save_active_conversation(state)
            console.print(f"[bold red]Connection error:[/bold red] {e}")
        except KeyboardInterrupt:
            console.print("\n[yellow]Response interrupted.[/yellow]")
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
        console.print()
        console.print(Rule(style="grey37"))


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - console Ollama LLM chat client")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--system", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--save-as", default=None)
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--conversation", default=None)
    parser.add_argument("--new-chat", default=None)
    args = parser.parse_args()
    config = load_config()
    if args.list_profiles:
        list_profiles_table(config)
        return
    profile_name = args.profile or config.get("default_profile") or "default"
    base_profile = get_profile(config, profile_name) or DEFAULT_CONFIG["profiles"]["default"]
    host = args.host or base_profile["host"]
    port = args.port or base_profile["port"]
    model = args.model or base_profile["model"]
    system = args.system if args.system is not None else base_profile.get("system")
    if args.profile and not get_profile(config, args.profile):
        console.print(f"[yellow]Profile '{args.profile}' not found, using defaults.[/yellow]")
        profile_name = "default"
    if args.save_as:
        upsert_profile(config, args.save_as, host, port, model, system)
        console.print(f"[bold green]Saved new profile:[/bold green] {args.save_as}")
    client = OllamaClient(host=host, port=port, model=model)
    chat_loop(client, config, profile_name, conversation_id=args.conversation, conversation_title=args.new_chat, base_system=system)


if __name__ == "__main__":
    main()
