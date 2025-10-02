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
PER_PAGE = 100
DEFAULT_TARGET = 1500
OUT_DIR_DEFAULT = "sw_mining_out"

RE_INTERFACE = re.compile(r'type\s+([A-Za-z0-9_]+)\s+interface\s*\{([^}]*)\}', re.MULTILINE | re.DOTALL)
RE_WEAVER_IMPLEMENTS = re.compile(r'weaver\.Implements\s*\[\s*([^\]]+)\s*\]', re.MULTILINE)
RE_LISTENER_FIELD = re.compile(r'\bweaver\.Listener\b')
RE_IMPORT_PATH = re.compile(r'github\.com/ServiceWeaver/weaver')  # simples e efetivo
RE_WEAVER_RUN_OR_INIT = re.compile(r'\bweaver\.(Run|Init)\b')
RE_RESOURCE_SPEC = re.compile(r'ResourceSpec|resourceSpec|resource_spec', re.IGNORECASE)
RE_TODO = re.compile(r'\b(TODO|FIXME)\b', re.IGNORECASE)
RE_DEPLOY_HINTS = re.compile(r'\b(single|multi|kube|gke|ssh)\b', re.IGNORECASE)

CONFIG_EXTS = ('.yaml', '.yml', '.json', '.toml', '.ini')

class GitHubClient:

    def __init__(self, token: Optional[str] = None, min_sleep: float = 1.0):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.s = requests.Session()
        if self.token:
            self.s.headers.update({"Authorization": f"token {self.token}"})
        self.s.headers.update({"Accept": "application/vnd.github.v3+json"})
        self.min_sleep = min_sleep


    def _sleep_until_reset(self, resp):
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
                pass


    def get(self, url, params=None, raw=False):
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
                # print(f"[ERROR] GET {url} -> {resp.status_code} {resp.text[:300]}")
                time.sleep(3)
                continue

    def _sleep_short(self):
        time.sleep(self.min_sleep)


    def search_code(self, q, per_page=PER_PAGE, page=1):
        url = f"{GITHUB_API}/search/code"
        params = {"q": q, "per_page": per_page, "page": page}
        return self.get(url, params=params)


    def repo_tree_recursive(self, owner, repo, ref="HEAD"):
        url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
        params = {"recursive": "1"}
        resp = self.get(url, params=params, raw=True)
        if resp is None:
            return None
        if resp.status_code == 200:
            return resp.json()
        return None


    def get_blob(self, owner, repo, sha):
        url = f"{GITHUB_API}/repos/{owner}/{repo}/git/blobs/{sha}"
        resp = self.get(url, raw=True)
        if resp is None:
            return None
        if resp.status_code == 200:
            return resp.json()
        return None


    def get_file_contents(self, owner, repo, path, ref=None):
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



def analyze_go_source(content: str):
    interfaces = []
    for m in RE_INTERFACE.finditer(content):
        name = m.group(1)
        body = m.group(2)
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


def analyze_config_text(text: str):
    findings = {
        "listeners_key": bool(re.search(r'\blisteners\.', text, re.IGNORECASE)),
        "resource_spec": bool(RE_RESOURCE_SPEC.search(text)),
        "deploy_hints": list(set(m.group(1).lower() for m in RE_DEPLOY_HINTS.finditer(text))),
        "todos": bool(RE_TODO.search(text)),
        "weaver_strings": bool(re.search(r'weaver', text, re.IGNORECASE)),
        "parse_issues": False,
    }
    if '<<' in text or '>>' in text or 'parse error' in text.lower():
        findings['parse_issues'] = True
    return findings


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

    # (default)
    if not strict:
        return (import_hits > 0) and (impls > 0 or has_listener or uses_run or has_weaver_toml)

    # (strict)
    return (import_hits > 0) and (impls > 0)



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
            if len(items) < PER_PAGE:
                break
            page += 1
            if page > 1000:
                break
    print(f"[discover] descoberta completa. repos encontrados: {len(repos)}")
    return repos


def inspect_repo(client: GitHubClient, full_name: str, strict: bool) -> Dict:
    owner, repo = full_name.split('/')
    print(f"[inspect] {full_name}")
    tree = []
    for ref in ["HEAD", "main", "master", "dev"]:
        tree_json = client.repo_tree_recursive(owner, repo, ref=ref)
        if tree_json and "tree" in tree_json:
            for e in tree_json["tree"]:
                e["branch"] = ref
                tree.append(e)
    if not tree:
        return {"repo": full_name, "error": "no_tree"}

    go_files = [e for e in tree if e['path'].endswith('.go') and e['type'] == 'blob']
    config_files = [e for e in tree if e['path'].endswith(CONFIG_EXTS) and e['type'] == 'blob']
    special_files = [e for e in tree if ('weaver' in e['path'].lower() or 'serviceweaver' in e['path'].lower()) and e['type'] == 'blob']
    candidates = {e['path']: e for e in (go_files + config_files + special_files)}.values()

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

        "import_hits": 0,
        "uses_run_or_init_hits": 0,
    }

    for entry in candidates:
        path = entry['path']
        try:
            blob = client.get_file_contents(owner, repo, path)
            if blob is None:
                continue
            encoding = blob.get('encoding')
            content = ""
            if blob.get('type') == 'file' and 'content' in blob:
                if encoding == 'base64':
                    import base64
                    content = base64.b64decode(blob['content']).decode('utf-8', errors='ignore')
                else:
                    content = blob['content']
            else:
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
            analysis['errors'].append({"path": path, "error": str(e)})
            continue

    analysis['deploy_hints'] = list(analysis['deploy_hints'])
    analysis['is_weaver'] = decide_is_weaver(analysis, strict=strict)
    return analysis


def save_progress(out_dir: Path, repos_list: List[str], results_accum: List[Dict]):
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'repos_list.txt', 'w', encoding='utf-8') as f:
        for r in repos_list:
            f.write(r + '\n')

    with open(out_dir / 'results.jsonl', 'w', encoding='utf-8') as f:
        for rec in results_accum:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')


    weaver_only = [r for r in results_accum if r.get('is_weaver')]
    with open(out_dir / 'results_weaver.jsonl', 'w', encoding='utf-8') as f:
        for rec in weaver_only:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    with open(out_dir / 'repos_weaver.txt', 'w', encoding='utf-8') as f:
        for r in weaver_only:
            f.write(r.get('repo', '') + '\n')


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

    checkpoint = {
        "repos_count": len(repos_list),
        "results_count": len(results_accum),
        "weaver_count": len(weaver_only),
        "timestamp": int(time.time())
    }
    with open(out_dir / 'progress.json', 'w', encoding='utf-8') as f:
        json.dump(checkpoint, f, indent=2)



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

    with open(out_dir / 'repos_list.txt', 'w', encoding='utf-8') as f:
        for r in repos:
            f.write(r + '\n')


    analyzed = set(rec['repo'] for rec in results)
    pbar = tqdm(repos, desc="Repos")
    for repo_full in pbar:
        if repo_full in analyzed:
            pbar.set_postfix_str(f"skipping {repo_full}")
            continue
        try:
            rec = inspect_repo(client, repo_full, strict=args.strict)
            results.append(rec)
            save_progress(out_dir, repos, results)
        except KeyboardInterrupt:
            print("Interrupted by user. Saving progress...")
            save_progress(out_dir, repos, results)
            break
        except Exception as e:
            print(f"[ERR] inspecting {repo_full}: {e}")
            results.append({"repo": repo_full, "error": str(e), "is_weaver": False})
            save_progress(out_dir, repos, results)
            continue
    print("Done. Results saved to:", out_dir.resolve())


if __name__ == "__main__":
    main()