#!/usr/bin/env python3
"""
minerador_2.py

Minera repositórios GitHub buscando usos de Service Weaver e extrai métricas estruturais e
configurações para responder às perguntas da proposta.

Uso:
  export GITHUB_TOKEN=ghp_xxx
  python minerador_2.py --target 1500 --out ./sw_output

Saída:
  ./sw_output/
    repos_list.txt           # lista de repositórios coletados
    progress.json            # checkpoint para retomar
    results.jsonl            # um JSON por linha com os dados extraídos
    results_summary.csv      # CSV resumido
"""
import os
import re
import time
import json
import argparse
from typing import List, Dict, Optional, Set
import requests
from tqdm import tqdm
import csv
import math
from pathlib import Path

# ---------- Configuráveis ----------
# Padrões de busca usados no GitHub Code Search para encontrar indícios de Service Weaver
SEARCH_PATTERNS = [
    'weaver.Implements',   # implementações de componentes
    'weaver.Listener',     # listener como campo de struct
    'weaver.ResourceSpec', # uso de especificação de recursos
    'listeners.',          # chave de configuração listeners.*
    'weaver.NewListener',  # possíveis criações de listener
    'weaver.Deploy',       # APIs que podem indicar deployment
    'serviceweaver',       # fallback genérico (strings)
]
GITHUB_API = "https://api.github.com"
PER_PAGE = 100  # tamanho máximo de página para endpoints que suportam
DEFAULT_TARGET = 1500
OUT_DIR_DEFAULT = "sw_mining_out"

# Regexes para análise de arquivos Go (heurísticas de parsing simplificadas)
RE_INTERFACE = re.compile(r'type\s+([A-Za-z0-9_]+)\s+interface\s*\{([^}]*)\}', re.MULTILINE | re.DOTALL)
RE_WEAVER_IMPLEMENTS = re.compile(r'weaver\.Implements\s*\[\s*([^\]]+)\s*\]', re.MULTILINE)
RE_LISTENER_FIELD = re.compile(r'weaver\.Listener', re.MULTILINE)
RE_RESOURCE_SPEC = re.compile(r'ResourceSpec|resourceSpec|resource_spec', re.IGNORECASE)
RE_TODO = re.compile(r'\b(TODO|FIXME)\b', re.IGNORECASE)
RE_DEPLOY_HINTS = re.compile(r'\b(single|multi|kube|gke|ssh)\b', re.IGNORECASE)

# Extensões consideradas "arquivos de configuração"
CONFIG_EXTS = ('.yaml', '.yml', '.json', '.toml', '.ini')

# ---------- Utilitários HTTP com rate-limit handling ----------
class GitHubClient:
    """
    Cliente simples para a API do GitHub com:
      - Session persistente
      - Autenticação via token se disponível
      - Esperas (sleep) automáticas ao atingir rate limit
      - Helpers para endpoints usados (search, tree, blob, contents)
    """
    def __init__(self, token: Optional[str] = None, min_sleep: float = 1.0):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.s = requests.Session()
        if self.token:
            self.s.headers.update({"Authorization": f"token {self.token}"})
        self.s.headers.update({"Accept": "application/vnd.github.v3+json"})
        self.min_sleep = min_sleep

    def _sleep_until_reset(self, resp):
        """
        Se os headers indicarem que o limite acabou (Remaining=0), aguarda até o timestamp de reset.
        """
        reset = resp.headers.get("X-RateLimit-Reset")
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and reset is not None:
            try:
                rem = int(remaining)
                reset_ts = int(reset)
                now = int(time.time())
                if rem <= 0 and reset_ts > now:
                    wait = reset_ts - now + 2
                    print(f"[rate-limit] remaining=0. Sleeping {wait}s until reset.")
                    time.sleep(wait)
            except Exception:
                # Em caso de headers inesperados, apenas ignora
                pass

    def get(self, url, params=None, raw=False):
        """
        GET com tratamento de:
          - 200: retorna JSON (ou resp raw)
          - 403/429: possivelmente rate limit/abuse -> espera e tenta novamente
          - 404: retorna None
          - outros: registra erro, aguarda curto e tenta de novo
        """
        while True:
            resp = self.s.get(url, params=params)
            if resp.status_code == 200:
                self._sleep_short()
                return resp.json() if not raw else resp
            elif resp.status_code in (403, 429):
                print(f"[WARN] status={resp.status_code} for {url}; headers: {resp.headers.get('X-RateLimit-Remaining')}")
                self._sleep_until_reset(resp)
                time.sleep(5)  # backoff adicional
                continue
            elif resp.status_code == 404:
                return None
            else:
                print(f"[ERROR] GET {url} -> {resp.status_code} {resp.text[:300]}")
                time.sleep(3)
                continue

    def _sleep_short(self):
        # Pausa curta entre chamadas para ser "polite" com a API
        time.sleep(self.min_sleep)

    # Wrappers de conveniência para endpoints específicos
    def search_code(self, q, per_page=PER_PAGE, page=1):
        url = f"{GITHUB_API}/search/code"
        params = {"q": q, "per_page": per_page, "page": page}
        return self.get(url, params=params)

    def repo_tree_recursive(self, owner, repo, ref="HEAD"):
        """
        Obtém a tree recursiva da ref informada (branch/commit/HEAD).
        GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
        params = {"recursive": "1"}
        resp = self.get(url, params=params, raw=True)
        if resp is None:
            return None
        if resp.status_code == 200:
            return resp.json()
        # 422 e outros erros: retorna None silenciosamente
        return None

    def get_blob(self, owner, repo, sha):
        """
        Obtém blob por SHA (conteúdo base64).
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/git/blobs/{sha}"
        resp = self.get(url, raw=True)
        if resp is None:
            return None
        if resp.status_code == 200:
            return resp.json()
        return None

    def get_file_contents(self, owner, repo, path, ref=None):
        """
        Obtém conteúdo de um arquivo via Contents API (pode vir base64).
        GET /repos/{owner}/{repo}/contents/{path}?ref=...
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        params = {}
        if ref:
            params['ref'] = ref
        resp = self.get(url, params=params, raw=True)
        if resp is None:
            return None
        if resp.status_code == 200:
            return resp.json()
        return None

# ---------- Parsing heuristics ----------
def analyze_go_source(content: str):
    """
    Analisa conteúdo Go (heurístico, sem AST formal):
      - Extrai interfaces e conta métodos (linha com '(' dentro do corpo)
      - Conta quantos weaver.Implements[...] aparecem
      - Detecta presença de weaver.Listener
      - Detecta termos de resourceSpec
      - Marca TODO/FIXME e possíveis hints de deploy (single/multi/kube/gke/ssh)
    Retorna um dicionário com métricas simples.
    """
    interfaces = []
    for m in RE_INTERFACE.finditer(content):
        name = m.group(1)
        body = m.group(2)
        # Heurística: conta linhas não vazias com '(' como "assinatura" de método
        method_lines = [l for l in body.splitlines() if l.strip() and '(' in l]
        interfaces.append({"name": name, "methods": len(method_lines)})
    implements_count = len(RE_WEAVER_IMPLEMENTS.findall(content))
    has_listener = bool(RE_LISTENER_FIELD.search(content))
    has_resource_spec = bool(RE_RESOURCE_SPEC.search(content))
    todos = bool(RE_TODO.search(content))
    deploy_hints = set(m.group(1).lower() for m in RE_DEPLOY_HINTS.finditer(content))
    return {
        "interfaces": interfaces,
        "implements_count": implements_count,
        "has_listener": has_listener,
        "has_resource_spec": has_resource_spec,
        "todos": todos,
        "deploy_hints": list(deploy_hints),
    }

def analyze_config_text(text: str):
    """
    Analisa texto de arquivos de configuração procurando:
      - listeners.*
      - resourceSpec/resource_spec
      - hints de deploy (single/multi/kube/gke/ssh)
      - TODO/FIXME
      - ocorrências de 'weaver'
      - sinais grosseiros de problemas de parse (ex.: '<<', '>>', 'parse error')
    """
    findings = {
        "listeners_key": bool(re.search(r'listeners\.', text, re.IGNORECASE)),
        "resource_spec": bool(RE_RESOURCE_SPEC.search(text)),
        "deploy_hints": list(set(m.group(1).lower() for m in RE_DEPLOY_HINTS.finditer(text))),
        "todos": bool(RE_TODO.search(text)),
        "weaver_strings": bool(re.search(r'weaver', text, re.IGNORECASE)),
    }
    # Heurística simples para marcar possíveis erros de parse em arquivos de conf.
    if '<<' in text or '>>' in text or 'parse error' in text.lower():
        findings['parse_issues'] = True
    else:
        findings['parse_issues'] = False
    return findings

# ---------- Main mining logic ----------
def discover_repos(client: GitHubClient, target: int) -> List[str]:
    """
    Descobre repositórios usando múltiplos padrões de busca.
    Retorna lista de 'owner/repo' (únicos) até atingir 'target'.
    Faz duas passadas: com 'language:Go' e sem filtro (para capturar docs/exemplos).
    """
    repos: List[str] = []
    seen: Set[str] = set()
    print("[discover] buscando repositórios via code search...")
    for pattern in SEARCH_PATTERNS:
        page = 1
        # 1ª passada: restringe a Go, onde normalmente está o código do Service Weaver
        while True:
            q = f'{pattern} in:file'
            q_lang = q + " language:Go"
            result = client.search_code(q_lang, per_page=PER_PAGE, page=page)
            if not result:
                break
            items = result.get('items', [])
            for it in items:
                full_name = it.get('repository', {}).get('full_name')
                if full_name and full_name not in seen:
                    repos.append(full_name)
                    seen.add(full_name)
                    if len(repos) >= target:
                        print(f"[discover] alvo atingido: {target} repositorios")
                        return repos
            # Se retornou menos que PER_PAGE, provavelmente acabou
            if len(items) < PER_PAGE:
                break
            page += 1
            if page > 10_000:  # proteção absurda (não deve ocorrer)
                break
        # 2ª passada: sem language, pode capturar configs/readmes/outros
        page = 1
        while True:
            q = f'{pattern} in:file'
            result = client.search_code(q, per_page=PER_PAGE, page=page)
            if not result:
                break
            items = result.get('items', [])
            for it in items:
                full_name = it.get('repository', {}).get('full_name')
                if full_name and full_name not in seen:
                    repos.append(full_name)
                    seen.add(full_name)
                    if len(repos) >= target:
                        print(f"[discover] alvo atingido: {target} repositorios")
                        return repos
            if len(items) < PER_PAGE:
                break
            page += 1
            if page > 1000:
                break
    print(f"[discover] descoberta completa. repos encontrados: {len(repos)}")
    return repos

def inspect_repo(client: GitHubClient, full_name: str) -> Dict:
    """
    Inspeciona um repositório:
      - Obtém árvore recursiva de vários refs candidatos (HEAD/main/master/dev)
      - Seleciona arquivos relevantes (.go, configs, e "especiais" contendo 'weaver')
      - Baixa conteúdos via Contents API (ou blob por SHA) e roda análises heurísticas
    Retorna dicionário com as métricas/coletas do repositório.
    """
    owner, repo = full_name.split('/')
    print(f"[inspect] {full_name}")
    tree = []
    # Tenta diferentes referências para cobrir casos em que HEAD não resolve
    for ref in ["HEAD", "main", "master", "dev"]:
        tree_json = client.repo_tree_recursive(owner, repo, ref=ref)
        if tree_json and "tree" in tree_json:
            for e in tree_json["tree"]:
                # Anexa a branch/ref para referência posterior (pode ajudar debug)
                e["branch"] = ref
                tree.append(e)
    if not tree:
        return {"repo": full_name, "error": "no_tree"}

    # Filtra candidatos por tipo/ extensão
    go_files = [e for e in tree if e['path'].endswith('.go') and e['type'] == 'blob']
    config_files = [e for e in tree if e['path'].endswith(CONFIG_EXTS) and e['type'] == 'blob']
    # Também pega qualquer arquivo que cite 'weaver' no caminho (heurística ampla)
    special_files = [e for e in tree if ('weaver' in e['path'].lower() or 'serviceweaver' in e['path'].lower()) and e['type'] == 'blob']
    # União de todos os candidatos (usando path como chave para evitar duplicatas)
    candidates = {e['path']: e for e in (go_files + config_files + special_files)}.values()

    # Estrutura de saída por repositório
    analysis = {
        "repo": full_name,
        "num_go_files_scanned": 0,
        "num_config_files_scanned": 0,
        "implements_total": 0,
        "interfaces_total": 0,
        "interfaces": [],  # lista de {name, methods}
        "has_any_listener_field": False,
        "has_any_resource_spec": False,
        "deploy_hints": set(),
        "todos_found": False,
        "config_findings": [],
        "errors": [],
    }

    # Percorre todos os arquivos candidatos e extrai informações
    for entry in candidates:
        path = entry['path']
        try:
            blob = client.get_file_contents(owner, repo, path)
            if blob is None:
                continue
            # Contents API pode retornar 'content' base64
            encoding = blob.get('encoding')
            content = ""
            if blob.get('type') == 'file' and 'content' in blob:
                if encoding == 'base64':
                    import base64
                    content = base64.b64decode(blob['content']).decode('utf-8', errors='ignore')
                else:
                    content = blob['content']
            else:
                # Fallback: tenta obter via blob SHA
                sha = entry.get('sha')
                if sha:
                    blob2 = client.get_blob(owner, repo, sha)
                    if blob2 and 'content' in blob2:
                        import base64
                        content = base64.b64decode(blob2['content']).decode('utf-8', errors='ignore')

            # Decide análise com base na extensão
            if path.endswith('.go'):
                analysis['num_go_files_scanned'] += 1
                res = analyze_go_source(content)
                analysis['implements_total'] += res['implements_count']
                analysis['interfaces_total'] += len(res['interfaces'])
                analysis['interfaces'].extend(res['interfaces'])
                if res['has_listener']:
                    analysis['has_any_listener_field'] = True
                if res['has_resource_spec']:
                    analysis['has_any_resource_spec'] = True
                if res['todos']:
                    analysis['todos_found'] = True
                for h in res['deploy_hints']:
                    analysis['deploy_hints'].add(h)
            else:
                analysis['num_config_files_scanned'] += 1
                cfg = analyze_config_text(content)
                # Registra achados de config (ex.: se tinha listeners.*, resourceSpec, etc.)
                if cfg['listeners_key']:
                    analysis['config_findings'].append({
                        "path": path, "listeners": True,
                        "resource_spec": cfg['resource_spec'],
                        "deploy_hints": cfg['deploy_hints'],
                        "parse_issues": cfg['parse_issues'],
                        "todos": cfg['todos']
                    })
                elif cfg['weaver_strings'] or cfg['resource_spec'] or cfg['deploy_hints']:
                    analysis['config_findings'].append({
                        "path": path, "listeners": False,
                        "resource_spec": cfg['resource_spec'],
                        "deploy_hints": cfg['deploy_hints'],
                        "parse_issues": cfg['parse_issues'],
                        "todos": cfg['todos']
                    })
                if cfg['todos']:
                    analysis['todos_found'] = True
                for h in cfg['deploy_hints']:
                    analysis['deploy_hints'].add(h)
                if cfg['resource_spec']:
                    analysis['has_any_resource_spec'] = True
        except Exception as e:
            # Não interrompe o processamento do repositório por um arquivo ruim
            analysis['errors'].append({"path": path, "error": str(e)})
            continue

    # Converte o set de deploy_hints em lista serializável
    analysis['deploy_hints'] = list(analysis['deploy_hints'])
    return analysis

# ---------- I/O & resume ----------
def save_progress(out_dir: Path, repos_list: List[str], results_accum: List[Dict]):
    """
    Salva:
      - repos_list.txt (lista dos repositórios)
      - results.jsonl (um JSON por linha, para facilitar reprocessamento/append)
      - results_summary.csv (resumo tabular)
      - progress.json (checkpoint minimalista com contagens e timestamp)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Lista de repositórios
    with open(out_dir / 'repos_list.txt', 'w', encoding='utf-8') as f:
        for r in repos_list:
            f.write(r + '\n')

    # Resultados detalhados (um json por linha)
    with open(out_dir / 'results.jsonl', 'w', encoding='utf-8') as f:
        for rec in results_accum:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    # CSV de resumo (campos planos principais)
    with open(out_dir / 'results_summary.csv', 'w', newline='', encoding='utf-8') as csvf:
        writer = csv.writer(csvf)
        writer.writerow(['repo','num_go_files_scanned','num_config_files_scanned','implements_total','interfaces_total','has_any_listener_field','has_any_resource_spec','deploy_hints','todos_found'])
        for rec in results_accum:
            writer.writerow([
                rec.get('repo'),
                rec.get('num_go_files_scanned',0),
                rec.get('num_config_files_scanned',0),
                rec.get('implements_total',0),
                rec.get('interfaces_total',0),
                rec.get('has_any_listener_field',False),
                rec.get('has_any_resource_spec',False),
                ','.join(rec.get('deploy_hints',[])),
                rec.get('todos_found',False)
            ])

    # Checkpoint simples para retomar
    checkpoint = {
        "repos_count": len(repos_list),
        "results_count": len(results_accum),
        "timestamp": int(time.time())
    }
    with open(out_dir / 'progress.json', 'w', encoding='utf-8') as f:
        json.dump(checkpoint, f, indent=2)

# ---------- CLI ----------
def main():
    """
    CLI principal:
      --target: quantos repositórios coletar (descoberta)
      --out: diretório de saída
      --min-sleep: delay entre requests (ser gentil c/ API)
      --resume: tentar retomar a partir de uma execução anterior
    """
    parser = argparse.ArgumentParser(description="Miner for Service Weaver repos on GitHub")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET, help="Number of repos to collect")
    parser.add_argument("--out", type=str, default=OUT_DIR_DEFAULT, help="Output directory")
    parser.add_argument("--min-sleep", type=float, default=1.0, help="min sleep between requests")
    parser.add_argument("--resume", action="store_true", help="Resume from existing out dir")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Aviso se não houver token (limites muito baixos)
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("[WARN] GITHUB_TOKEN not set. You may hit very low rate limits. Strongly recommend setting GITHUB_TOKEN env var.")
    client = GitHubClient(token=token, min_sleep=args.min_sleep)

    repos = []
    results = []

    # Se --resume, tenta carregar dados anteriores
    if args.resume:
        repos_path = out_dir / 'repos_list.txt'
        results_path = out_dir / 'results.jsonl'
        if repos_path.exists():
            with open(repos_path, 'r', encoding='utf-8') as f:
                repos = [l.strip() for l in f if l.strip()]
        if results_path.exists():
            with open(results_path, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        results.append(json.loads(line))
                    except:
                        pass
        print(f"[resume] loaded {len(repos)} repos and {len(results)} results")

    # Descoberta de repositórios até atingir o alvo
    if len(repos) < args.target:
        need = args.target - len(repos)
        found = discover_repos(client, need)
        # Merge preservando unicidade
        existing = set(repos)
        for r in found:
            if r not in existing:
                repos.append(r)
                existing.add(r)
            if len(repos) >= args.target:
                break

    # Persiste a lista de repos cedo (útil para retomar)
    with open(out_dir / 'repos_list.txt', 'w', encoding='utf-8') as f:
        for r in repos:
            f.write(r + '\n')

    # Inspeção repo-a-repo (pula os já analisados ao retomar)
    analyzed = set(rec['repo'] for rec in results)
    pbar = tqdm(repos, desc="Repos")
    for repo_full in pbar:
        if repo_full in analyzed:
            pbar.set_postfix_str(f"skipping {repo_full}")
            continue
        try:
            rec = inspect_repo(client, repo_full)
            results.append(rec)
            # Salva a cada repo para permitir retomar em caso de interrupção
            save_progress(out_dir, repos, results)
        except KeyboardInterrupt:
            print("Interrupted by user. Saving progress...")
            save_progress(out_dir, repos, results)
            break
        except Exception as e:
            # Em caso de erro inesperado num repo, registra e continua
            print(f"[ERR] inspecting {repo_full}: {e}")
            results.append({"repo": repo_full, "error": str(e)})
            save_progress(out_dir, repos, results)
            continue

    print("Done. Results saved to:", out_dir.resolve())

if __name__ == "__main__":
    main()
