#!/usr/bin/env python3
"""Ysha — assistente pessoal de terminal, pensada para Android/Termux.

Somente biblioteca padrão. Chaves de API ficam em variáveis de ambiente;
arquivos manipulados pela assistente ficam confinados ao diretório de dados.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
APP_DIR = Path(os.environ.get("YSHA_HOME", Path.home() / ".ysha")).expanduser()
WORKSPACE = (APP_DIR / "arquivos").resolve()
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "estado.json"
MAX_HISTORY = 24
MAX_TOOL_ROUNDS = 4
MAX_FILE_BYTES = 500_000

SYSTEM_PROMPT = """Você é Ysha, a assistente pessoal do usuário, em um projeto Python local criado a pedido dele. Sua personalidade é inteligente, organizada, cordial, direta e honesta, inspirada em uma assistente de ficção científica. Não diga que foi desenvolvida pela Sapiens AI, Agnes ou por outro provedor: Agnes (se estiver configurada) é apenas o serviço/modelo que gera respostas, não a criadora da Ysha. Se perguntarem quem a criou, explique que é o projeto pessoal do usuário, implementado no arquivo ysha.py com apoio de IA. Fale em português brasileiro, a menos que a pessoa peça outro idioma. Ajude a planejar e execute ações usando somente as ferramentas disponíveis; não afirme que fez algo sem verificar o resultado. Explique limitações com clareza. Preserve privacidade. Peça autorização antes de ações potencialmente destrutivas, de sobrescrever arquivos ou executar comandos; a aplicação também impõe confirmações. Nunca peça nem revele chaves de API. Para tarefas que exigem informação atual, pesquise. Use a memória apenas para preferências/fatos úteis e duradouros, nunca para segredos. Não invente capacidades, autoria ou resultados."""

DEFAULT_CONFIG = {
    "providers": [
        {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY"},
        {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini", "api_key_env": "OPENROUTER_API_KEY"},
        {"name": "Agnes", "base_url": "https://apihub.agnes-ai.com/v1", "model": "", "api_key_env": "AGNES_API_KEY"},
    ],
    "request_timeout": 45,
    "voice_input": False,
    "voice_output": False,
}

TOOL_SPECS = [
    {"type": "function", "function": {"name": "web_search", "description": "Pesquisar informações atuais na web. Retorne títulos, trechos e URLs encontrados.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Ler arquivo de texto dentro da pasta segura da Ysha.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Criar ou alterar arquivo de texto dentro da pasta segura. Sempre pede confirmação; não use para conteúdo enorme.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "Listar arquivos na pasta segura da Ysha.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Subpasta opcional"}}}}},
    {"type": "function", "function": {"name": "run_command", "description": "Executar um comando simples permitido (lista de segurança restrita). Pedirá confirmação. Não use comandos compostos.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "task_manage", "description": "Adicionar, listar ou concluir tarefas locais.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "list", "done"]}, "text": {"type": "string"}, "task_id": {"type": "integer"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "reminder_manage", "description": "Criar lembrete local com horário, ou listar os lembretes. Formato ISO local YYYY-MM-DD HH:MM.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "list"]}, "text": {"type": "string"}, "when": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "memory_manage", "description": "Ver, adicionar ou remover uma lembrança/preferência duradoura. Peça confirmação ao usuário antes de salvar ou remover.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "add", "remove"]}, "text": {"type": "string"}, "memory_id": {"type": "integer"}}, "required": ["action"]}}},
]


def ensure_store() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    try:
        APP_DIR.chmod(0o700)
    except OSError:
        pass
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not STATE_PATH.exists():
        save_json(STATE_PATH, {"history": [], "tasks": [], "reminders": [], "memories": [], "next_id": 1})


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def load_config() -> dict[str, Any]:
    cfg = load_json(CONFIG_PATH, {})
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})
    # Também acrescenta Agnes a instalações que já tenham um config.json antigo.
    providers = merged.get("providers", [])
    if isinstance(providers, list) and not any(isinstance(p, dict) and p.get("name") == "Agnes" for p in providers):
        providers.append(dict(DEFAULT_CONFIG["providers"][-1]))
    for provider in providers if isinstance(providers, list) else []:
        if isinstance(provider, dict) and provider.get("name") == "Agnes" and not str(provider.get("model", "")).strip() and os.environ.get("AGNES_MODEL"):
            provider["model"] = os.environ["AGNES_MODEL"].strip()
    merged["providers"] = providers
    return merged


def load_state() -> dict[str, Any]:
    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    for k, v in {"history": [], "tasks": [], "reminders": [], "memories": [], "next_id": 1}.items():
        state.setdefault(k, v)
    return state


def persist(state: dict[str, Any]) -> None:
    save_json(STATE_PATH, state)


def ask_yes_no(prompt: str) -> bool:
    try:
        answer = input(prompt + " [s/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"s", "sim", "y", "yes"}


def safe_path(raw: str) -> Path:
    """Resolve a relative path while preventing traversal and symlink escape."""
    candidate = (WORKSPACE / raw).resolve()
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise ValueError("O caminho precisa ficar dentro da pasta segura da Ysha.")
    return candidate


def web_search(query: str) -> dict[str, Any]:
    query = query.strip()
    if not query or len(query) > 300:
        return {"ok": False, "error": "A busca precisa ter entre 1 e 300 caracteres."}
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Ysha/0.1)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            page = resp.read(1_500_000).decode("utf-8", "replace")
    except Exception as exc:
        return {"ok": False, "error": f"Pesquisa indisponível: {type(exc).__name__}."}
    # Extrai resultados sem depender de uma biblioteca HTML externa.
    blocks = re.findall(r'<div[^>]*class="[^\"]*result[^\"]*"[^>]*>(.*?)</div>\s*</div>', page, re.I | re.S)
    results = []
    for block in blocks:
        m = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.I | re.S)
        if not m:
            continue
        href = html.unescape(re.sub(r"&amp;", "&", m.group(1)))
        title = re.sub(r"<.*?>", "", m.group(2), flags=re.S)
        sm = re.search(r'<(?:a|div)[^>]*class="result__snippet"[^>]*>(.*?)</(?:a|div)>', block, re.I | re.S)
        snippet = re.sub(r"<.*?>", "", sm.group(1), flags=re.S) if sm else ""
        if href.startswith("//"):
            href = "https:" + href
        if title:
            results.append({"title": html.unescape(title).strip(), "url": href, "snippet": html.unescape(snippet).strip()})
        if len(results) >= 5:
            break
    return {"ok": True, "query": query, "results": results, "note": "Resultados públicos; confirme detalhes em fontes oficiais."}


def cmd_allowed(command: str) -> tuple[list[str] | None, str | None]:
    if len(command) > 300 or any(x in command for x in (";", "|", "&", ">", "<", "`", "$", "\n")):
        return None, "Comandos compostos, redirecionamentos e expansão de shell são bloqueados."
    try:
        args = shlex.split(command)
    except ValueError:
        return None, "Não consegui interpretar esse comando."
    if not args:
        return None, "Comando vazio."
    allowed = {"pwd", "date", "whoami", "ls", "python", "python3", "git", "df", "free", "uname", "termux-battery-status", "termux-wifi-connectioninfo"}
    if args[0] not in allowed:
        return None, f"'{args[0]}' não está na lista de comandos permitidos."
    if args[0] == "git" and (len(args) < 2 or args[1] not in {"status", "log", "diff", "branch"}):
        return None, "Por segurança, só permito git status, log, diff e branch."
    if args[0] in {"python", "python3"} and len(args) > 1 and args[1] not in {"--version", "-V"}:
        return None, "Execução arbitrária de código Python pelo comando foi bloqueada; posso criar um arquivo e pedir autorização para executá-lo de forma explícita."
    if args[0] == "ls":
        if any(a.startswith("-") and a not in {"-a", "-l", "-la", "-al"} for a in args[1:]):
            return None, "Opções não permitidas para ls."
        for a in args[1:]:
            if not a.startswith("-"):
                try:
                    safe_path(a)
                except ValueError:
                    return None, "O ls só pode acessar a pasta segura da Ysha."
    if len(args) > 8 or any(len(a) > 180 for a in args):
        return None, "Comando muito longo."
    return args, None


def manage_task(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action")
    if action == "add":
        text = str(args.get("text", "")).strip()
        if not text or len(text) > 300:
            return {"ok": False, "error": "Informe uma tarefa de até 300 caracteres."}
        item = {"id": state["next_id"], "text": text, "done": False, "created": dt.datetime.now().astimezone().isoformat(timespec="minutes")}
        state["next_id"] += 1
        state["tasks"].append(item)
        persist(state)
        return {"ok": True, "task": item}
    if action == "list":
        return {"ok": True, "tasks": state["tasks"]}
    if action == "done":
        try:
            task_id = int(args.get("task_id"))
        except (ValueError, TypeError):
            return {"ok": False, "error": "Informe o número da tarefa."}
        for task in state["tasks"]:
            if task["id"] == task_id:
                task["done"] = True
                persist(state)
                return {"ok": True, "task": task}
        return {"ok": False, "error": "Tarefa não encontrada."}
    return {"ok": False, "error": "Ação de tarefa inválida."}


def manage_reminder(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if args.get("action") == "list":
        return {"ok": True, "reminders": state["reminders"]}
    if args.get("action") != "add":
        return {"ok": False, "error": "Ação de lembrete inválida."}
    text, when = str(args.get("text", "")).strip(), str(args.get("when", "")).strip()
    if not text or len(text) > 300:
        return {"ok": False, "error": "Informe o que devo lembrar (até 300 caracteres)."}
    try:
        moment = dt.datetime.strptime(when, "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return {"ok": False, "error": "Use data e hora no formato AAAA-MM-DD HH:MM, horário local."}
    if moment <= dt.datetime.now().astimezone():
        return {"ok": False, "error": "O horário precisa ser no futuro."}
    item = {"id": state["next_id"], "text": text, "when": moment.isoformat(timespec="minutes"), "notified": False}
    state["next_id"] += 1
    state["reminders"].append(item)
    persist(state)
    return {"ok": True, "reminder": item, "note": "Lembrete local salvo. Ele aparece quando Ysha estiver aberta; para alertas do Android com o app fechado, configure integração externa."}


def manage_memory(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action")
    if action == "list":
        return {"ok": True, "memories": state["memories"]}
    if action == "add":
        text = str(args.get("text", "")).strip()
        if not text or len(text) > 500:
            return {"ok": False, "error": "A lembrança precisa ter até 500 caracteres."}
        if not ask_yes_no(f"Ysha quer salvar esta lembrança: {text!r}. Autoriza?"):
            return {"ok": False, "cancelled": True, "error": "O usuário não autorizou salvar."}
        item = {"id": state["next_id"], "text": text}
        state["next_id"] += 1
        state["memories"].append(item)
        persist(state)
        return {"ok": True, "memory": item}
    if action == "remove":
        try:
            memory_id = int(args.get("memory_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Informe o número da lembrança."}
        if not ask_yes_no(f"Ysha quer remover a lembrança {memory_id}. Autoriza?"):
            return {"ok": False, "cancelled": True, "error": "O usuário não autorizou remover."}
        before = len(state["memories"])
        state["memories"] = [x for x in state["memories"] if x.get("id") != memory_id]
        if len(state["memories"]) == before:
            return {"ok": False, "error": "Lembrança não encontrada."}
        persist(state)
        return {"ok": True, "removed": memory_id}
    return {"ok": False, "error": "Ação de memória inválida."}


def dispatch(name: str, args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if name == "web_search":
        return web_search(str(args.get("query", "")))
    if name == "read_file":
        try:
            path = safe_path(str(args.get("path", "")))
            if not path.is_file():
                return {"ok": False, "error": "Arquivo não encontrado."}
            if path.stat().st_size > MAX_FILE_BYTES:
                return {"ok": False, "error": "Arquivo excede o limite de leitura de 500 KB."}
            return {"ok": True, "path": str(path.relative_to(WORKSPACE)), "content": path.read_text(encoding="utf-8")}
        except (OSError, ValueError, UnicodeError) as exc:
            return {"ok": False, "error": f"Não consegui ler o arquivo ({type(exc).__name__})."}
    if name == "list_files":
        try:
            path = safe_path(str(args.get("path", "")))
            if not path.is_dir():
                return {"ok": False, "error": "Pasta não encontrada."}
            items = [{"name": p.name, "type": "pasta" if p.is_dir() else "arquivo"} for p in sorted(path.iterdir())[:100]]
            return {"ok": True, "path": str(path.relative_to(WORKSPACE)) if path != WORKSPACE else ".", "items": items}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"Não consegui listar ({type(exc).__name__})."}
    if name == "write_file":
        raw = str(args.get("path", ""))
        content = str(args.get("content", ""))
        try:
            path = safe_path(raw)
            if not raw or not content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
                return {"ok": False, "error": "Informe caminho e conteúdo (máximo 500 KB)."}
            if not ask_yes_no(f"Ysha quer gravar {path.name!r} em {path.parent}. Autoriza?"):
                return {"ok": False, "cancelled": True, "error": "Gravação cancelada."}
            if path.exists() and not ask_yes_no(f"O arquivo {path.name!r} já existe. Confirma sobrescrever?"):
                return {"ok": False, "cancelled": True, "error": "Sobrescrita cancelada."}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                return {"ok": False, "error": "A verificação após gravar falhou."}
            return {"ok": True, "path": str(path.relative_to(WORKSPACE)), "verified": True}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"Não consegui gravar ({type(exc).__name__})."}
    if name == "run_command":
        command = str(args.get("command", ""))
        argv, error = cmd_allowed(command)
        if error:
            return {"ok": False, "error": error}
        if not ask_yes_no(f"Ysha pede autorização para executar: {command}"):
            return {"ok": False, "cancelled": True, "error": "Comando cancelado."}
        try:
            proc = subprocess.run(argv, cwd=str(WORKSPACE), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=12, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout[-5000:], "stderr": proc.stderr[-2000:], "verified": True}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Comando excedeu o limite de 12 segundos e foi interrompido."}
        except OSError as exc:
            return {"ok": False, "error": f"Falha ao iniciar comando ({type(exc).__name__})."}
    if name == "task_manage":
        return manage_task(state, args)
    if name == "reminder_manage":
        return manage_reminder(state, args)
    if name == "memory_manage":
        return manage_memory(state, args)
    return {"ok": False, "error": "Ferramenta desconhecida."}


def available_providers(config: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for p in config.get("providers", []):
        if not isinstance(p, dict):
            continue
        env_name = str(p.get("api_key_env", ""))
        if env_name and os.environ.get(env_name) and str(p.get("model", "")).strip() and str(p.get("base_url", "")).strip():
            out.append(p)
    return out


def api_chat(provider: dict[str, Any], messages: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    base = str(provider.get("base_url", "")).rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("O endereço do provedor precisa usar HTTPS.")
    env_name = str(provider.get("api_key_env", ""))
    key = os.environ.get(env_name, "")
    if not key:
        raise ValueError("Chave não configurada.")
    payload = {"model": provider["model"], "messages": messages, "tools": TOOL_SPECS, "tool_choice": "auto", "temperature": 0.4}
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "Ysha/0.1"}, method="POST")
    with urllib.request.urlopen(req, timeout=int(config.get("request_timeout", 45))) as resp:
        data = json.loads(resp.read(2_000_000).decode("utf-8"))
    return data["choices"][0]["message"]


def build_messages(state: dict[str, Any], user_text: str) -> list[dict[str, Any]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    memories = state.get("memories", [])
    if memories:
        messages.append({"role": "system", "content": "Lembranças autorizadas do usuário (use apenas quando pertinentes):\n" + "\n".join(f"- {x['text']}" for x in memories[-40:])})
    history = state.get("history", [])[-MAX_HISTORY:]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    return messages


def ask_ai(user_text: str, state: dict[str, Any], config: dict[str, Any]) -> str:
    providers = available_providers(config)
    if not providers:
        return "Ainda não há provedor pronto para uso. Defina a variável de ambiente da chave (por exemplo, AGNES_API_KEY) e preencha um modelo de texto em ~/.ysha/config.json. Posso continuar usando os comandos locais sem IA."
    errors = []
    messages = build_messages(state, user_text)
    provider = None
    response = None
    # O failover só ocorre antes de qualquer ferramenta executar, evitando ações duplicadas.
    for candidate in providers:
        try:
            response = api_chat(candidate, messages, config)
            provider = candidate
            break
        except urllib.error.HTTPError as exc:
            errors.append(f"{candidate.get('name', 'provedor')}: HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
            errors.append(f"{candidate.get('name', 'provedor')}: {type(exc).__name__}")
        except Exception as exc:
            errors.append(f"{candidate.get('name', 'provedor')}: falha inesperada ({type(exc).__name__})")
    if provider is None or response is None:
        return "Não consegui conectar a nenhum provedor de IA configurado. " + "; ".join(errors) + ". Confira internet, modelo e variável de ambiente da chave."
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            calls = response.get("tool_calls") or []
            if not calls:
                answer = response.get("content") or "Não recebi uma resposta em texto do modelo."
                state["history"].extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": answer}])
                state["history"] = state["history"][-MAX_HISTORY:]
                persist(state)
                return str(answer)
            messages.append(response)
            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("argumentos inválidos")
                except (json.JSONDecodeError, ValueError):
                    result = {"ok": False, "error": "Argumentos inválidos."}
                else:
                    result = dispatch(name, args, state)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result, ensure_ascii=False)})
            response = api_chat(provider, messages, config)
        return "Interrompi depois de quatro etapas de ferramenta para evitar um ciclo. Me diga se quer que eu continue de outro jeito."
    except urllib.error.HTTPError as exc:
        return f"A ação local foi processada, mas o provedor {provider.get('name', 'de IA')} falhou ao gerar a resposta final (HTTP {exc.code}). Confira a tarefa/arquivo antes de tentar novamente para evitar duplicá-la."
    except Exception as exc:
        return f"A ação local pode ter sido processada, mas não consegui receber a resposta final do provedor ({type(exc).__name__}). Confira o resultado antes de tentar de novo para evitar duplicá-la."


def check_reminders(state: dict[str, Any]) -> list[str]:
    now = dt.datetime.now().astimezone()
    due = []
    changed = False
    for item in state["reminders"]:
        if item.get("notified"):
            continue
        try:
            when = dt.datetime.fromisoformat(item["when"])
        except (ValueError, KeyError, TypeError):
            continue
        if when <= now:
            due.append(f"Lembrete: {item.get('text', '')} (marcado para {when.strftime('%d/%m %H:%M')})")
            item["notified"] = True
            changed = True
    if changed:
        persist(state)
    return due


def voice_input() -> str | None:
    exe = shutil.which("termux-speech-to-text")
    if not exe:
        print("Entrada de voz indisponível: instale o Termux:API e o pacote termux-api.")
        return None
    try:
        proc = subprocess.run([exe], capture_output=True, text=True, timeout=60, check=False)
        text = proc.stdout.strip()
        return text if proc.returncode == 0 and text else None
    except (OSError, subprocess.TimeoutExpired):
        print("Não consegui capturar a fala.")
        return None


def voice_output(text: str) -> None:
    exe = shutil.which("termux-tts-speak")
    if not exe:
        print("Saída de voz indisponível: instale Termux:API e o pacote termux-api.")
        return
    try:
        subprocess.run([exe, text[:3000]], stdin=subprocess.DEVNULL, timeout=40, check=False, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        print("Não consegui reproduzir a resposta em voz.")


def local_help() -> None:
    print(textwrap.dedent("""\
    Comandos locais:
      /ajuda       mostrar esta ajuda
      /tarefas     listar tarefas
      /lembretes   listar lembretes
      /memoria     listar lembranças salvas
      /voz         ativar/desativar voz (Termux:API)
      /falar       ditar uma mensagem (requer Termux:API)
      /status      mostrar configuração sem revelar chaves
      /sair        encerrar
    Converse normalmente para usar o modelo e as ferramentas. Com modelo configurado,
    Ysha pode pesquisar, ler/criar arquivos em ~/.ysha/arquivos, gerenciar tarefas,
    lembretes e memória; gravações, comandos e mudanças de memória pedem autorização.
    """))


def main() -> int:
    ensure_store()
    if "--self-test" in sys.argv:
        return self_test()
    cfg, state = load_config(), load_state()
    print(f"Ysha {VERSION} — pasta de trabalho: {WORKSPACE}")
    print("Digite /ajuda para ver comandos, /sair para encerrar. Seus dados ficam no aparelho.")
    for msg in check_reminders(state):
        print("\n🔔 " + msg)
    while True:
        try:
            user_text = input("\nVocê: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nYsha: Até mais!")
            return 0
        if not user_text:
            continue
        if user_text == "/sair":
            print("Ysha: Até mais!")
            return 0
        if user_text == "/ajuda":
            local_help()
            continue
        if user_text == "/status":
            print(f"Ysha: Provedores configurados com chave: {len(available_providers(cfg))}; voz de entrada: {'sim' if cfg.get('voice_input') else 'não'}; voz de saída: {'sim' if cfg.get('voice_output') else 'não'}; pasta: {WORKSPACE}")
            continue
        if user_text == "/tarefas":
            print(json.dumps(state["tasks"], ensure_ascii=False, indent=2) if state["tasks"] else "Ysha: Nenhuma tarefa cadastrada.")
            continue
        if user_text == "/lembretes":
            print(json.dumps(state["reminders"], ensure_ascii=False, indent=2) if state["reminders"] else "Ysha: Nenhum lembrete cadastrado.")
            continue
        if user_text == "/memoria":
            print(json.dumps(state["memories"], ensure_ascii=False, indent=2) if state["memories"] else "Ysha: Ainda não salvei lembranças.")
            continue
        if user_text == "/voz":
            cfg["voice_input"] = not cfg.get("voice_input", False)
            cfg["voice_output"] = not cfg.get("voice_output", False)
            save_json(CONFIG_PATH, cfg)
            print(f"Ysha: Voz {'ativada' if cfg['voice_input'] else 'desativada'}. Configure Termux:API para usar.")
            continue
        if user_text == "/falar":
            spoken = voice_input()
            if not spoken:
                continue
            user_text = spoken
            print("Você (voz): " + user_text)
        answer = ask_ai(user_text, state, cfg)
        print("\nYsha: " + answer)
        if cfg.get("voice_output"):
            voice_output(answer)
        for msg in check_reminders(state):
            print("\n🔔 " + msg)
    

def self_test() -> int:
    """Testes locais que não acessam a rede nem executam comandos externos."""
    try:
        ensure_store()
        assert safe_path("teste.txt").parent == WORKSPACE
        try:
            safe_path("../../fora.txt")
            raise AssertionError("path traversal aceito")
        except ValueError:
            pass
        assert cmd_allowed("pwd")[0] == ["pwd"]
        assert cmd_allowed("rm -rf /home")[1]
        assert cmd_allowed("pwd; rm x")[1]
        state = {"tasks": [], "reminders": [], "memories": [], "next_id": 1}
        added = manage_task(state, {"action": "add", "text": "teste"})
        assert added["ok"] and added["task"]["id"] == 1
        assert manage_task(state, {"action": "done", "task_id": 1})["task"]["done"]
        cfg = load_config()
        assert isinstance(cfg.get("providers"), list)
        agnes = next(p for p in cfg["providers"] if p.get("name") == "Agnes")
        assert agnes["base_url"] == "https://apihub.agnes-ai.com/v1" and agnes["api_key_env"] == "AGNES_API_KEY"
        print("Self-test OK: caminhos seguros, bloqueio de comandos, tarefas e configuração Agnes.")
        return 0
    except Exception as exc:
        print(f"Self-test falhou: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
