#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
analisar_sw.py

Responde às perguntas:
1) Quais parâmetros de configuração/implantação aparecem com mais frequência?
   a) Frequência de single/multi/kube/gke/ssh
   b) Presença de resourceSpec (CPU/Mem definidos? sim/não)
   c) (LIMITAÇÃO) Presença de atributos de listener (public/hostname/address)
      -> NÃO está disponível no summary/jsonl padrão do minerador. O script gera
         um placeholder e explica a limitação.
   d) Número de "implantações independentes detectadas" por repositório
      -> Heurística: número de *hints* únicos (single/multi/kube/gke/ssh).
         (Opcional) Se houver results.jsonl, também conta quantos arquivos de
         config relevantes existem (weaver.toml / deployment.toml).

2) Distribuição das métricas estruturais por "serviço"
   -> No summary, temos:
      - implements_total (proxy de componentes, porém conta ocorrências)
      - interfaces_total e lista 'interfaces' (apenas no results.jsonl)
      - has_any_listener_field (presença de listeners em código)
   -> O script produz:
      - hist/estatísticas de implements_total por repo
      - hist/estatísticas de interfaces_total por repo
      - (ENRICHED) distribuição de nº de métodos por interface (se results.jsonl)
      - (proxy) presença de listeners por repo

Saídas principais (em --out):
  q1a_deploy_hints_counts.csv            # contagem e % por hint (só is_weaver=True)
  q1b_resource_spec_presence.csv         # presença de resourceSpec (is_weaver=True)
  q1c_listener_attr_presence.csv         # placeholder + explicação da limitação
  q1d_independent_deployments.csv        # heurística por repo + agregados

  q2_components_summary.csv              # estatísticas (implements_total, interfaces_total)
  q2_interfaces_methods_distribution.csv # (se results.jsonl) distribuição de métodos por interface
  q2_listeners_presence.csv              # % de repos com listener (is_weaver=True)

  + gráficos PNG básicos (matplotlib) sem seaborn (se --plots)
"""

import argparse
from pathlib import Path
from typing import List, Dict, Any

import json
import math
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt


# Hints padronizados de estratégia de implantação buscados no minerador
HINTS = ["single", "multi", "kube", "gke", "ssh"]


def _outdir(p: Path):
    """Garante que o diretório de saída exista e o retorna."""
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_summary(p: Path) -> pd.DataFrame:
    """
    Lê o CSV de resumo (results_summary.csv) e normaliza:
      - Booleans: 'true'/'false' -> True/False
      - 'deploy_hints' -> lista normalizada em 'deploy_hints_list'
      - Colunas numéricas para int (com coerção segura)
    """
    df = pd.read_csv(p)
    # normaliza tipos booleanos
    for c in ["is_weaver", "has_any_listener_field", "has_any_resource_spec", "todos_found"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.lower().map({"true": True, "false": False})
    # cria lista de hints (ou lista vazia)
    if "deploy_hints" in df.columns:
        df["deploy_hints_list"] = df["deploy_hints"].fillna("").astype(str).apply(
            lambda s: [x.strip() for x in s.split(",") if x.strip() != ""]
        )
    else:
        df["deploy_hints_list"] = [[] for _ in range(len(df))]
    # força colunas numéricas comuns do summary
    for c in [
        "num_go_files_scanned","num_config_files_scanned","implements_total",
        "interfaces_total","import_hits","uses_run_or_init_hits"
    ]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    return df


def _read_jsonl(p: Path) -> List[Dict[str, Any]]:
    """
    Lê um JSON Lines (results.jsonl) em memória.
    Linhas inválidas são ignoradas silenciosamente.
    """
    out = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def q1a(df: pd.DataFrame, out: Path, plots: bool):
    """
    Q1(a): Frequência de hints de implantação entre repositórios marcados como Service Weaver.
    - Explode deploy_hints por repo, dedup por (repo,hint) para não contar duplicado no mesmo repo.
    - Gera CSV com contagem e % de repos por hint; opcionalmente salva um bar plot.
    """
    sub = df[df["is_weaver"] == True].copy()
    # explode por hint e dedup por repo/hint
    expl = sub[["repo", "deploy_hints_list"]].explode("deploy_hints_list")
    expl = expl.dropna()
    expl["deploy_hints_list"] = expl["deploy_hints_list"].astype(str)
    expl = expl[expl["deploy_hints_list"] != ""]
    expl = expl.drop_duplicates(["repo", "deploy_hints_list"])

    freq = expl["deploy_hints_list"].value_counts().rename_axis("hint").reset_index(name="count")
    total_repos = len(sub)
    freq["pct_repos"] = (freq["count"] / max(total_repos, 1) * 100).round(2)

    # garante a presença de todas as categorias, mesmo sem ocorrências
    for h in HINTS:
        if h not in set(freq["hint"]):
            freq = pd.concat([freq, pd.DataFrame([{"hint": h, "count": 0, "pct_repos": 0.0}])], ignore_index=True)

    freq = freq.sort_values("count", ascending=False)
    freq.to_csv(out / "q1a_deploy_hints_counts.csv", index=False)

    if plots:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(freq["hint"], freq["count"])
        ax.set_title("Deploy hints (is_weaver=True)")
        ax.set_xlabel("hint")
        ax.set_ylabel("count")
        ax.tick_params(axis='x', rotation=20)
        fig.tight_layout()
        fig.savefig(out / "q1a_deploy_hints_counts.png", dpi=160)
        plt.close(fig)


def q1b(df: pd.DataFrame, out: Path):
    """
    Q1(b): Presença de resourceSpec entre repos is_weaver=True.
    Produz um CSV simples com contagens e porcentagem.
    """
    sub = df[df["is_weaver"] == True].copy()
    present = int(sub["has_any_resource_spec"].sum())
    total = len(sub)
    res = pd.DataFrame({
        "metric": ["resourceSpec_present_true", "resourceSpec_present_false", "total_repos", "pct_present"],
        "value": [present, total - present, total, round(100 * present / max(total, 1), 2)]
    })
    res.to_csv(out / "q1b_resource_spec_presence.csv", index=False)


def q1c_placeholder(out: Path):
    """
    Q1(c): Placeholder explicando a limitação de não ter atributos de listener
    (public/hostname/address) no summary/jsonl padrão.
    """
    explanation = [
        {
            "note": (
                "Os atributos de listener (public/hostname/address) NÃO são extraídos pelo summary/jsonl padrão. "
                "Para registrar presença real, seria preciso re-minerar os arquivos de config (weaver.toml/deployment.toml) "
                "procurando chaves `listeners.<NAME>.public`, `hostname`, `address` etc. "
                "Resultado marcado como 'unknown' neste conjunto."
            )
        }
    ]
    pd.DataFrame(explanation).to_csv(out / "q1c_listener_attr_presence.csv", index=False)


def q1d(df: pd.DataFrame, out: Path, jsonl_data: List[Dict[str, Any]]):
    """
    Q1(d): "Implantações independentes detectadas" por repo (heurística).
    - Sinal 1: nº de hints distintos no repo (single/multi/kube/gke/ssh).
    - Sinal 2 (opcional): nº de arquivos de config relevantes (weaver.toml/deployment.toml) encontrados no results.jsonl.
    A pontuação final é o máximo entre (hints distintos) e (qtde de configs relevantes).
    """
    # Heurística 1: número de hints únicos
    df["independent_deployments_hints"] = df["deploy_hints_list"].apply(lambda lst: len(set(lst)))

    # Heurística 2 (opcional, se results.jsonl disponível): contar configs relevantes por repo
    config_files_map = {}  # repo -> qtd de arquivos relevantes
    if jsonl_data:
        for rec in jsonl_data:
            repo = rec.get("repo", "")
            cnt = 0
            for cfg in rec.get("config_findings", []):
                path = (cfg.get("path") or "").lower()
                if path.endswith("weaver.toml") or path.endswith("deployment.toml"):
                    cnt += 1
            config_files_map[repo] = cnt
    df["independent_deployments_configs"] = df["repo"].map(config_files_map).fillna(0).astype(int)

    # Score combinado (máximo entre os dois sinais)
    df["independent_deployments_score"] = df[["independent_deployments_hints", "independent_deployments_configs"]].max(axis=1)

    cols = ["repo", "is_weaver", "independent_deployments_hints", "independent_deployments_configs", "independent_deployments_score"]
    df[cols].to_csv(out / "q1d_independent_deployments.csv", index=False)

    # Agregados apenas para is_weaver=True (média, mediana, máx)
    sub = df[df["is_weaver"] == True]
    agg = pd.DataFrame({
        "metric": ["mean_hints", "median_hints", "max_hints",
                   "mean_configs", "median_configs", "max_configs",
                   "mean_score", "median_score", "max_score"],
        "value": [
            round(sub["independent_deployments_hints"].mean(), 2),
            int(sub["independent_deployments_hints"].median()),
            int(sub["independent_deployments_hints"].max()),
            round(sub["independent_deployments_configs"].mean(), 2),
            int(sub["independent_deployments_configs"].median()),
            int(sub["independent_deployments_configs"].max()),
            round(sub["independent_deployments_score"].mean(), 2),
            int(sub["independent_deployments_score"].median()),
            int(sub["independent_deployments_score"].max()),
        ]
    })
    agg.to_csv(out / "q1d_independent_deployments_agg.csv", index=False)


def q2(df: pd.DataFrame, out: Path, jsonl_data: List[Dict[str, Any]], plots: bool):
    """
    Q2: Distribuições estruturais por "serviço"/repo.
    - Usa proxies do summary: implements_total (componentes), interfaces_total,
      e presença de listeners. Se houver results.jsonl, analisa nº de métodos por interface.
    - Salva CSVs e, opcionalmente (--plots), histogramas.
    """
    # Base por repositório
    base = df[["repo", "is_weaver", "implements_total", "interfaces_total", "has_any_listener_field"]].copy()

    # Estatísticas apenas para repos classificados como Service Weaver
    sub = base[base["is_weaver"] == True]
    stats = {
        "implements_total": {
            "mean": sub["implements_total"].mean(), "median": sub["implements_total"].median(),
            "p90": sub["implements_total"].quantile(0.9), "max": sub["implements_total"].max()
        },
        "interfaces_total": {
            "mean": sub["interfaces_total"].mean(), "median": sub["interfaces_total"].median(),
            "p90": sub["interfaces_total"].quantile(0.9), "max": sub["interfaces_total"].max()
        },
        "listeners_presence_pct": (100 * sub["has_any_listener_field"].mean())
    }
    # Exporta resumo como pares (metric, value)
    pd.DataFrame([
        {"metric": "implements_total_mean", "value": round(stats["implements_total"]["mean"], 2)},
        {"metric": "implements_total_median", "value": int(stats["implements_total"]["median"])},
        {"metric": "implements_total_p90", "value": float(stats["implements_total"]["p90"])},
        {"metric": "implements_total_max", "value": int(stats["implements_total"]["max"])},
        {"metric": "interfaces_total_mean", "value": round(stats["interfaces_total"]["mean"], 2)},
        {"metric": "interfaces_total_median", "value": int(stats["interfaces_total"]["median"])},
        {"metric": "interfaces_total_p90", "value": float(stats["interfaces_total"]["p90"])},
        {"metric": "interfaces_total_max", "value": int(stats["interfaces_total"]["max"])},
        {"metric": "listeners_presence_pct", "value": round(stats["listeners_presence_pct"], 2)}
    ]).to_csv(out / "q2_components_summary.csv", index=False)

    # Histogramas simples (sem seaborn e sem definir cores explicitamente)
    if plots:
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.hist(sub["implements_total"], bins=20)
        ax.set_title("Distribuição de implements_total (is_weaver=True)")
        ax.set_xlabel("implements_total")
        ax.set_ylabel("repos")
        fig.tight_layout()
        fig.savefig(out / "q2_implements_hist.png", dpi=160)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(6, 4))
        ax.hist(sub["interfaces_total"], bins=20)
        ax.set_title("Distribuição de interfaces_total (is_weaver=True)")
        ax.set_xlabel("interfaces_total")
        ax.set_ylabel("repos")
        fig.tight_layout()
        fig.savefig(out / "q2_interfaces_hist.png", dpi=160)
        plt.close(fig)

    # --- Enriquecimento por interface (se results.jsonl) ---
    # Cada rec em results.jsonl pode conter: "interfaces": [{"name":..., "methods":N}, ...]
    methods_rows = []
    if jsonl_data:
        for rec in jsonl_data:
            if not rec.get("is_weaver"):
                continue
            repo = rec.get("repo", "")
            for itf in rec.get("interfaces", []) or []:
                methods_rows.append({"repo": repo, "interface": itf.get("name", ""), "methods": itf.get("methods", 0)})

    if methods_rows:
        df_methods = pd.DataFrame(methods_rows)
        df_methods["methods"] = pd.to_numeric(df_methods["methods"], errors="coerce").fillna(0).astype(int)

        # Estatística descritiva do nº de métodos por interface
        dist = df_methods["methods"].describe().to_frame().reset_index()
        dist.columns = ["stat", "value"]
        dist.to_csv(out / "q2_interfaces_methods_distribution.csv", index=False)

        if plots:
            fig, ax = plt.subplots(figsize=(6, 4))
            ax.hist(df_methods["methods"], bins=range(0, df_methods["methods"].max() + 3))
            ax.set_title("Métodos por interface (todas as interfaces, is_weaver=True)")
            ax.set_xlabel("nº de métodos")
            ax.set_ylabel("interfaces")
            fig.tight_layout()
            fig.savefig(out / "q2_interfaces_methods_hist.png", dpi=160)
            plt.close(fig)

    # Presença de listeners (em % dos repos weaver) exportada separadamente
    pd.DataFrame({
        "metric": ["listeners_present_pct_weaver"],
        "value": [round(stats["listeners_presence_pct"], 2)]
    }).to_csv(out / "q2_listeners_presence.csv", index=False)


def main():
    """
    CLI:
      --summary : caminho para results_summary.csv (obrigatório)
      --jsonl   : caminho opcional para results.jsonl (enriquece 1d e 2)
      --out     : diretório de saída
      --plots   : se passado, salva PNGs além dos CSVs
    """
    ap = argparse.ArgumentParser(description="Análises para responder às questões 1 e 2 da proposta (Service Weaver).")
    ap.add_argument("--summary", required=True, help="Caminho para results_summary.csv")
    ap.add_argument("--jsonl", default="", help="(Opcional) Caminho para results.jsonl para análises enriquecidas")
    ap.add_argument("--out", required=True, help="Diretório de saída")
    ap.add_argument("--plots", action="store_true", help="Salvar gráficos PNG além dos CSVs")
    args = ap.parse_args()

    out = _outdir(Path(args.out))
    df = _read_summary(Path(args.summary))
    jsonl_data = _read_jsonl(Path(args.jsonl)) if args.jsonl else []

    # Q1: frequências e heurísticas de implantação
    q1a(df, out, plots=args.plots)
    q1b(df, out)
    q1c_placeholder(out)           # explica limitação da (1c)
    q1d(df, out, jsonl_data)

    # Q2: distribuição de proxies estruturais e (opcional) métodos por interface
    q2(df, out, jsonl_data, plots=args.plots)

    print(f"[ok] análises geradas em: {out.resolve()}")
    if not jsonl_data:
        print("[nota] results.jsonl não foi informado; (2) fica sem distribuição de métodos por interface.")
        print("      Para enriquecer 1d e 2, rode com: --jsonl caminho/para/results.jsonl")


if __name__ == "__main__":
    main()
