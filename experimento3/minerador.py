#!/usr/bin/env python3

import os
import re
import time
import json
import argparse
from typing import List, Dict, Optional, Set
import requests
from tqdm import tqdm
import csv
from pathlib import Path

# ---------------------------------------------
# Consultas de Code Search no GitHub.
# Mistura buscas por import do ServiceWeaver + símbolos típicos (Implements, Listener, Run/Init),
# e também nomes de arquivos de configuração comuns (weaver.toml, deployment.toml).
# ---------------------------------------------
SEARCH_QUERIES = [
    '"github.com/ServiceWeaver/weaver" weaver.Implements language:Go in:file',
    '"github.com/ServiceWeaver/weaver" "weaver.Listener" language:Go in:file',
    '"github.com/ServiceWeaver/weaver" weaver.Run language:Go in:file',
    '"github.com/ServiceWeaver/weaver" weaver.Init language:Go in:file',
    
    'filename:weaver.toml in:path',
    'filename:deployment.toml weaver in:file',
    
    'weaver.Implements language:Go in:file',
]

GITHUB_API = "https://api.github.com"
PER_PAGE = 100                 # tamanho padrão de página suportado pelo endpoint
DEFAULT_TARGET = 1500          # quantidade de repositórios desejada
OUT_DIR_DEFAULT = "sw_mining_out"

# ---------------------------------------------
# Regex de varredura heurística (sem AST) para arquivos Go e configs.
# São aproximadas, mas rápidas e eficientes em larga escala.
# ---------------------------------------------
RE_INTERFACE = re.compile(r'type\s+([A-Za-z0-9_]+)\s+interface\s*\{([^}]*)\}', re.MULTILINE | re.DOTALL)
RE_WEAVER_IMPLEMENTS = re.compile(r'weaver\.Implements\s*\[\s*([^\]]+)\s*\]', re.MULTILINE)
RE_LISTENER_FIELD = re.compile(r'\bweaver\.Listener\b')
RE_IMPORT_PATH = re.compile(r'github\.com/ServiceWeaver/weaver')  # presença do import (simples e robusto)
RE_WEAVER_RUN_OR_INIT = re.compile(r'\bweaver\.(Run|Init)\b')
RE_RESOURCE_SPEC = re.compile(r'ResourceSpec|resourceSpec|resource_spec', re.IGNORECASE)
RE_TODO = re.compile(r'\b(TODO|FIXME)\b', re.IGNORECASE)
RE_DEPLOY_HINTS = re.compile(r'\b(single|multi|kube|gke|ssh)\b', re.IGNORECASE)

# extensões consideradas como “arquivos de configuração”
CONFIG_EXTS = ('.yaml', '.yml', '.json', '.toml', '.ini')

# ---------------------------------------------
# Cliente GitHub: requests.Session + tratamento de rate limit
# ---------------------------------------------
class GitHubClient:

    def __init__(self, token: Optional[str] = None, min_sleep: float = 1.0):
        # Usa token do env se não for passado; melhora muito os limites da API.
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.s = requests.Session()
        if self.token:
            self.s.headers.update({"Authorization": f"token {self.token}"})
        self.s.headers.update({"Accept": "application/vnd.github.v3+json"})
        self.min_sleep = min_sleep

    def _sleep_until_reset(self, resp):
        """
        Se ficarmos sem créditos (Remaining=0), espera até o timestamp de reset informado pelo header.
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
                # Falha ao interpretar headers -> apenas ignora
                pass

    def get(self, url, params=None, raw=False):
        """
        GET com resiliência:
          - 200: retorna JSON (ou Response raw se raw=True)
          - 403/429: possivelmente rate limit/abuse -> espera e re-tenta
          - 404: retorna None (não encontrado)
          - outros códigos: aguarda curto e re-tenta (evita abortar toda a mineração)
        """
        while True:
            resp = self.s.get(url, params=params)
            if resp.status_code == 200:
                self._sleep_short()
                return resp.json() if not raw else resp
            elif resp.status_code in (403, 429):
                print(f"[WARN] status={resp.status_code} for {url}; remaining={resp.headers.get('X-RateLimit-Remaining')}")
                self._sleep_until_reset(resp)
                time.sleep(5)
                continue
            elif resp.status_code == 404:
                return None
            else:
                # Erro transitório: espera curto e tenta novamente
                # print pode ser verboso; mantido comentado para não poluir a saída
                # print(f"[ERROR] GET {url} -> {resp.status_code} {resp.text[:300]}")
                time.sleep(3)
                continue

    def _sleep_short(self):
        # Pausa leve entre requests para ser cordial com a API
        time.sleep(self.min_sleep)

    # ---------- Wrappers de endpoints usados ----------
    def search_code(self, q, per_page=PER_PAGE, page=1):
        url = f"{GITHUB_API}/search/code"
        params = {"q": q, "per_page": per_page, "page": page}
        return self.get(url, params=params)

    def repo_tree_recursive(self, owner, repo, ref="HEAD"):
        """
        Lê árvore recursiva (lista arquivos) de uma ref (branch/commit).
        GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
        params = {"recursive": "1"}
        resp = self.get(url, params=params, raw=True)
        if resp is None:
            return None
        if resp.status_code == 200:
            return resp.json()
        return None

    def get_blob(self, owner, repo, sha):
        """
        Lê blob por SHA (conteúdo geralmente base64).
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
        Lê conteúdo via Contents API; pode retornar o campo 'content' em base64.
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

# ---------------------------------------------
# Heurística de análise de arquivos Go (sem AST):
# extrai interfaces, conta métodos, procura Implements[], Listener,
# presença do import, chamadas Run/Init, etc.
# ---------------------------------------------
def analyze_go_source(content: str):
    interfaces = []
    for m in RE_INTERFACE.finditer(content):
        name = m.group(1)
        body = m.group(2)
        # Aproximação: conta linhas não vazias com '(' como "assinaturas" de métodos da interface
        method_lines = [l for l in body.splitlines() if l.strip() and '(' in l]
        interfaces.append({"name": name, "methods": len(method_lines)})

    implements_count = len(RE_WEAVER_IMPLEMENTS.findall(content))
    has_listener = bool(RE_LISTENER_FIELD.search(content))
    has_import = bool(RE_IMPORT_PATH.search(content))
    uses_run_or_init = bool(RE_WEAVER_RUN_OR_INIT.search(content))
    has_resource_spec = bool(RE_RESOURCE_SPEC.search(content))
    todos = bool(RE_TODO.search(content))
    deploy_hints = set(m.group(1).lower() for m in RE_DEPLOY_HINTS.finditer(content))

    return {
        "interfaces": interfaces,
        "implements_count": implements_count,
        "has_listener": has_listener,
        "has_import": has_import,
        "uses_run_or_init": uses_run_or_init,
        "has_resource_spec": has_resource_spec,
        "todos": todos,
        "deploy_hints": list(deploy_hints),
    }

# ---------------------------------------------
# Heurística de análise de arquivos de configuração:
# listeners.*, resourceSpec, hints de deploy, TODO/FIXME, 'weaver' e sinais de parse quebrado.
# ---------------------------------------------
def analyze_config_text(text: str):
    findings = {
        "listeners_key": bool(re.search(r'\blisteners\.', text, re.IGNORECASE)),
        "resource_spec": bool(RE_RESOURCE_SPEC.search(text)),
        "deploy_hints": list(set(m.group(1).lower() for m in RE_DEPLOY_HINTS.finditer(text))),
        "todos": bool(RE_TODO.search(text)),
        "weaver_strings": bool(re.search(r'weaver', text, re.IGNORECASE)),
        "parse_issues": False,
    }
    # Marcação simples de conteúdo suspeito (frequente em merges/templating mal resolvido)
    if '<<' in text or '>>' in text or 'parse error' in text.lower():
        findings['parse_issues'] = True
    return findings

# ---------------------------------------------
# Regra de decisão "é Service Weaver?" (is_weaver)
# - modo default (não estrito): exige import OU sinais fortes (implements/listener/run/weaver.toml)
# - modo estrito: exige import + Implements (mais conservador)
# ---------------------------------------------
def decide_is_weaver(analysis: Dict, strict: bool = False) -> bool:
    """Decide se o repo realmente implementa Service Weaver."""
    import_hits = analysis.get("import_hits", 0)
    impls = analysis.get("implements_total", 0)
    has_listener = analysis.get("has_any_listener_field", False)
    uses_run = analysis.get("uses_run_or_init_hits", 0) > 0

    has_weaver_toml = any(
        f.get("path", "").lower().endswith("weaver.toml")
        for f in analysis.get("config_findings", [])
    )

    # default (mais inclusivo)
    if not strict:
        return (import_hits > 0) and (impls > 0 or has_listener or uses_run or has_weaver_toml)

    # strict (mais preciso, menos recall)
    return (import_hits > 0) and (impls > 0)

# ---------------------------------------------
# Descoberta de repositórios via Code Search.
# Agrega nomes únicos "owner/repo" até alcançar o alvo (target).
# ---------------------------------------------
def discover_repos(client: GitHubClient, target: int) -> List[str]:
    repos: List[str] = []
    seen: Set[str] = set()
    print("[discover] buscando repositórios via code search...")
    for q in SEARCH_QUERIES:
        page = 1
        while True:
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
            # Quando vier menos que PER_PAGE, encerra paginação dessa consulta
            if len(items) < PER_PAGE:
                break
            page += 1
            if page > 1000:  # limite de segurança
                break
    print(f"[discover] descoberta completa. repos encontrados: {len(repos)}")
    return repos

# ---------------------------------------------
# Inspeção de um repo:
# - obtém a tree (várias refs candidatas)
# - escolhe arquivos de interesse (.go, configs e “especiais” que contenham 'weaver' no path)
# - baixa conteúdo (contents/blob) e aplica as heurísticas
# ---------------------------------------------
def inspect_repo(client: GitHubClient, full_name: str, strict: bool) -> Dict:
    owner, repo = full_name.split('/')
    print(f"[inspect] {full_name}")
    tree = []
    for ref in ["HEAD", "main", "master", "dev"]:
        tree_json = client.repo_tree_recursive(owner, repo, ref=ref)
        if tree_json and "tree" in tree_json:
            for e in tree_json["tree"]:
                e["branch"] = ref  # guarda ref para debug
                tree.append(e)
    if not tree:
        return {"repo": full_name, "error": "no_tree"}

    # Seleção de candidatos por extensão/conteúdo do path
    go_files = [e for e in tree if e['path'].endswith('.go') and e['type'] == 'blob']
    config_files = [e for e in tree if e['path'].endswith(CONFIG_EXTS) and e['type'] == 'blob']
    special_files = [e for e in tree if ('weaver' in e['path'].lower() or 'serviceweaver' in e['path'].lower()) and e['type'] == 'blob']
    candidates = {e['path']: e for e in (go_files + config_files + special_files)}.values()

    # Estado agregado do repositório (usado também pela decisão is_weaver)
    analysis = {
        "repo": full_name,
        "num_go_files_scanned": 0,
        "num_config_files_scanned": 0,
        "implements_total": 0,
        "interfaces_total": 0,
        "interfaces": [],
        "has_any_listener_field": False,
        "has_any_resource_spec": False,
        "deploy_hints": set(),
        "todos_found": False,
        "config_findings": [],
        "errors": [],

        # contagens auxiliares que fortalecem a decisão is_weaver
        "import_hits": 0,
        "uses_run_or_init_hits": 0,
    }

    # Percorre arquivos candidatos e extrai achados
    for entry in candidates:
        path = entry['path']
        try:
            blob = client.get_file_contents(owner, repo, path)
            if blob is None:
                continue
            encoding = blob.get('encoding')
            content = ""
            if blob.get('type') == 'file' and 'content' in blob:
                # Contents API pode vir em base64
                if encoding == 'base64':
                    import base64
                    content = base64.b64decode(blob['content']).decode('utf-8', errors='ignore')
                else:
                    content = blob['content']
            else:
                # Fallback: blob via SHA
                sha = entry.get('sha')
                if sha:
                    blob2 = client.get_blob(owner, repo, sha)
                    if blob2 and 'content' in blob2:
                        import base64
                        content = base64.b64decode(blob2['content']).decode('utf-8', errors='ignore')

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
                if res['has_import']:
                    analysis['import_hits'] += 1
                if res['uses_run_or_init']:
                    analysis['uses_run_or_init_hits'] += 1
            else:
                # Análise de configs
                analysis['num_config_files_scanned'] += 1
                cfg = analyze_config_text(content)
                rec = {
                    "path": path,
                    "listeners": cfg['listeners_key'],
                    "resource_spec": cfg['resource_spec'],
                    "deploy_hints": cfg['deploy_hints'],
                    "parse_issues": cfg['parse_issues'],
                    "todos": cfg['todos'],
                    "weaver_strings": cfg['weaver_strings'],
                }
                analysis['config_findings'].append(rec)
                if cfg['todos']:
                    analysis['todos_found'] = True
                for h in cfg['deploy_hints']:
                    analysis['deploy_hints'].add(h)
                if cfg['resource_spec']:
                    analysis['has_any_resource_spec'] = True
        except Exception as e:
            # Não aborta o repo por erro em um arquivo
            analysis['errors'].append({"path": path, "error": str(e)})
            continue

    analysis['deploy_hints'] = list(analysis['deploy_hints'])
    # Classificação final do repo como “usa Service Weaver” (is_weaver)
    analysis['is_weaver'] = decide_is_weaver(analysis, strict=strict)
    return analysis

# ---------------------------------------------
# Persistência incremental:
# - lista de repos
# - results.jsonl (todos)
# - results_weaver.jsonl (somente os classificados como is_weaver)
# - CSVs de resumo
# - checkpoint progress.json (contagens e epoch)
# ---------------------------------------------
def save_progress(out_dir: Path, repos_list: List[str], results_accum: List[Dict]):
    out_dir.mkdir(parents=True, exist_ok=True)

    # Lista de repositórios descobertos
    with open(out_dir / 'repos_list.txt', 'w', encoding='utf-8') as f:
        for r in repos_list:
            f.write(r + '\n')

    # Todos os resultados (um JSON por linha)
    with open(out_dir / 'results.jsonl', 'w', encoding='utf-8') as f:
        for rec in results_accum:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    # Somente os "is_weaver"
    weaver_only = [r for r in results_accum if r.get('is_weaver')]
    with open(out_dir / 'results_weaver.jsonl', 'w', encoding='utf-8') as f:
        for rec in weaver_only:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    # Lista simples de repos classificados como is_weaver
    with open(out_dir / 'repos_weaver.txt', 'w', encoding='utf-8') as f:
        for r in weaver_only:
            f.write(r.get('repo', '') + '\n')

    # Resumo tabular (todos)
    with open(out_dir / 'results_summary.csv', 'w', newline='', encoding='utf-8') as csvf:
        writer = csv.writer(csvf)
        writer.writerow([
            'repo','is_weaver',
            'num_go_files_scanned','num_config_files_scanned',
            'implements_total','interfaces_total',
            'has_any_listener_field','has_any_resource_spec',
            'import_hits','uses_run_or_init_hits',
            'deploy_hints','todos_found'
        ])
        for rec in results_accum:
            writer.writerow([
                rec.get('repo'),
                rec.get('is_weaver', False),
                rec.get('num_go_files_scanned',0),
                rec.get('num_config_files_scanned',0),
                rec.get('implements_total',0),
                rec.get('interfaces_total',0),
                rec.get('has_any_listener_field',False),
                rec.get('has_any_resource_spec',False),
                rec.get('import_hits',0),
                rec.get('uses_run_or_init_hits',0),
                ','.join(rec.get('deploy_hints',[])),
                rec.get('todos_found',False),
            ])

    # Resumo tabular (apenas is_weaver)
    with open(out_dir / 'results_weaver.csv', 'w', newline='', encoding='utf-8') as csvf:
        writer = csv.writer(csvf)
        writer.writerow([
            'repo',
            'num_go_files_scanned','num_config_files_scanned',
            'implements_total','interfaces_total',
            'has_any_listener_field','has_any_resource_spec',
            'import_hits','uses_run_or_init_hits',
            'deploy_hints','todos_found'
        ])
        for rec in weaver_only:
            writer.writerow([
                rec.get('repo'),
                rec.get('num_go_files_scanned',0),
                rec.get('num_config_files_scanned',0),
                rec.get('implements_total',0),
                rec.get('interfaces_total',0),
                rec.get('has_any_listener_field',False),
                rec.get('has_any_resource_spec',False),
                rec.get('import_hits',0),
                rec.get('uses_run_or_init_hits',0),
                ','.join(rec.get('deploy_hints',[])),
                rec.get('todos_found',False),
            ])

    # Checkpoint mínimo para retomar execuções
    checkpoint = {
        "repos_count": len(repos_list),
        "results_count": len(results_accum),
        "weaver_count": len(weaver_only),
        "timestamp": int(time.time())
    }
    with open(out_dir / 'progress.json', 'w', encoding='utf-8') as f:
        json.dump(checkpoint, f, indent=2)

# ---------------------------------------------
# CLI principal
# ---------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Miner for Service Weaver repos on GitHub (com filtro is_weaver)")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET, help="Número de repositórios para coletar")
    parser.add_argument("--out", type=str, default=OUT_DIR_DEFAULT, help="Diretório de saída")
    parser.add_argument("--min-sleep", type=float, default=1.0, help="Pausa mínima entre requests")
    parser.add_argument("--resume", action="store_true", help="Retomar de out dir existente")
    parser.add_argument("--strict", action="store_true", help="Exigir import + Implements para considerar is_weaver")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("[WARN] GITHUB_TOKEN não definido. Defina para evitar rate limit pesado.")
    client = GitHubClient(token=token, min_sleep=args.min_sleep)

    repos = []
    results = []

    # Retomada: carrega lista de repositórios e resultados prévios se existir
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
                        # Ignora linhas corrompidas
                        pass
        print(f"[resume] loaded {len(repos)} repos and {len(results)} results")

    # Descoberta: completa até atingir o target
    if len(repos) < args.target:
        need = args.target - len(repos)
        found = discover_repos(client, need)
        existing = set(repos)
        for r in found:
            if r not in existing:
                repos.append(r)
                existing.add(r)
            if len(repos) >= args.target:
                break

    # Persiste lista de repositórios (mesmo que parcial)
    with open(out_dir / 'repos_list.txt', 'w', encoding='utf-8') as f:
        for r in repos:
            f.write(r + '\n')

    # Inspeção repositório a repositório com barra de progresso
    analyzed = set(rec['repo'] for rec in results)
    pbar = tqdm(repos, desc="Repos")
    for repo_full in pbar:
        if repo_full in analyzed:
            pbar.set_postfix_str(f"skipping {repo_full}")
            continue
        try:
            rec = inspect_repo(client, repo_full, strict=args.strict)
            results.append(rec)
            # Salva a cada iteração para suportar retomadas e interrupções
            save_progress(out_dir, repos, results)
        except KeyboardInterrupt:
            print("Interrupted by user. Saving progress...")
            save_progress(out_dir, repos, results)
            break
        except Exception as e:
            # Não para a mineração por erro em um repo; registra e continua
            print(f"[ERR] inspecting {repo_full}: {e}")
            results.append({"repo": repo_full, "error": str(e), "is_weaver": False})
            save_progress(out_dir, repos, results)
            continue

    print("Done. Results saved to:", out_dir.resolve())

if __name__ == "__main__":
    main()
