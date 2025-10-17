#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import math
from pathlib import Path
from typing import List

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# Colunas numéricas esperadas no CSV de entrada (results_summary.csv)
NUM_COLS = [
    "num_go_files_scanned","num_config_files_scanned","implements_total",
    "interfaces_total","import_hits","uses_run_or_init_hits"
]

# Colunas booleanas esperadas
BOOL_COLS = [
    "is_weaver","has_any_listener_field","has_any_resource_spec","todos_found"
]

def _ensure_outdir(d: Path):
    """Garante que o diretório de saída exista (cria recursivamente)."""
    d.mkdir(parents=True, exist_ok=True)

def load_and_clean(csv_path: Path) -> pd.DataFrame:
    """
    Carrega o CSV, normaliza formatos e tipos:
      - Converte colunas booleanas de 'true'/'false' (string) para bool
      - Converte 'deploy_hints' (string "a,b,c") em lista ['a','b','c'] (nova coluna)
      - Força colunas numéricas para int (coercion segura com NaN->0)
    """
    df = pd.read_csv(csv_path)

    # Normaliza booleanos vindos como string
    for c in BOOL_COLS:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip().str.lower().map({"true": True, "false": False})

    # Normaliza deploy_hints para lista (facilita contagens e gráficos)
    if "deploy_hints" in df.columns:
        def parse_hints(x):
            if pd.isna(x) or str(x).strip() == "":
                return []
            return [h.strip() for h in str(x).split(",") if h.strip() != ""]
        df["deploy_hints_list"] = df["deploy_hints"].apply(parse_hints)

    # Garante tipos numéricos consistentes
    for c in NUM_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    return df

def describe_tables(df: pd.DataFrame, out: Path):
    """
    Gera tabelas descritivas:
      - overview.csv: visão geral da amostra (total, #is_weaver, %)
      - numeric_describe_all.csv: describe() das colunas numéricas
      - numeric_by_is_weaver.csv: estatísticas por classe (is_weaver=True/False)
    """
    # Visão geral da amostra
    overview = pd.DataFrame({
        "metric": ["repos_total", "weaver_true", "weaver_false", "weaver_%"],
        "value": [
            len(df),
            int(df["is_weaver"].sum()),
            int((~df["is_weaver"]).sum()),
            round(100 * df["is_weaver"].mean(), 2)
        ]
    })
    overview.to_csv(out / "overview.csv", index=False)

    # Estatísticas numéricas (geral)
    numeric = df[NUM_COLS].describe().T
    numeric.to_csv(out / "numeric_describe_all.csv")

    # Estatísticas numéricas por classe (is_weaver)
    per_class = df.groupby("is_weaver")[NUM_COLS].agg(["mean","median","std","min","max","sum","count"])
    per_class.to_csv(out / "numeric_by_is_weaver.csv")

def correlations(df: pd.DataFrame, out: Path):
    """
    Calcula correlação de Pearson entre as colunas NUM_COLS e:
      - salva a matriz em CSV
      - salva um heatmap simples (sem definir cores explicitamente, como pedido)
    """
    corr = df[NUM_COLS].corr(method="pearson")
    corr.to_csv(out / "correlations_pearson.csv")

    # Heatmap com matplotlib (sem estilos/cores customizadas)
    fig, ax = plt.subplots(figsize=(7, 6))
    im = ax.imshow(corr.values, aspect="auto")  # retorna imagem da matriz
    ax.set_xticks(range(len(corr.columns)))
    ax.set_xticklabels(corr.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(corr.index)))
    ax.set_yticklabels(corr.index)

    # Anota valores numéricos no heatmap
    for i in range(len(corr.index)):
        for j in range(len(corr.columns)):
            ax.text(j, i, f"{corr.values[i,j]:.2f}", ha="center", va="center")

    ax.set_title("Correlação (Pearson)")
    fig.tight_layout()
    fig.savefig(out / "correlations_pearson.png", dpi=160)
    plt.close(fig)

def topn_tables(df: pd.DataFrame, out: Path, n: int = 15):
    """
    Gera rankings Top-N por diferentes colunas de interesse:
      - implements_total, import_hits, uses_run_or_init_hits, interfaces_total, num_go_files_scanned
    Inclui as colunas NUM_COLS para contexto adicional.
    """
    def topn(col: str, fname: str):
        # Reorganiza colunas: repo, is_weaver, métrica base e demais numéricas
        cols = ["repo","is_weaver", col] + [c for c in NUM_COLS if c != col]
        (df.sort_values(col, ascending=False)[cols].head(n)
           .to_csv(out / fname, index=False))

    topn("implements_total", "top_implements_total.csv")
    topn("import_hits", "top_import_hits.csv")
    topn("uses_run_or_init_hits", "top_uses_run_or_init_hits.csv")
    topn("interfaces_total", "top_interfaces_total.csv")
    topn("num_go_files_scanned", "top_num_go_files.csv")

def deploy_hints_stats(df: pd.DataFrame, out: Path):
    """
    Analisa frequência de 'deploy_hints' (single/multi/kube/gke/ssh):
      - Gera frequências gerais e somente para is_weaver=True
      - Plota um gráfico de barras simples para is_weaver=True
    """
    if "deploy_hints_list" not in df.columns:
        return

    # Explode lista de hints por repo e agrega contagem e % de repos
    def explode_and_count(sub: pd.DataFrame) -> pd.DataFrame:
        expl = sub[["repo","deploy_hints_list"]].explode("deploy_hints_list")
        expl = expl[~expl["deploy_hints_list"].isna() & (expl["deploy_hints_list"] != "")]
        freq = expl["deploy_hints_list"].value_counts().reset_index()
        freq.columns = ["hint","count"]
        total_repos = len(sub)
        freq["pct_repos"] = (freq["count"] / max(total_repos,1) * 100).round(2)
        return freq

    # Frequência geral
    freq_all = explode_and_count(df)
    freq_all.to_csv(out / "deploy_hints_freq_all.csv", index=False)

    # Frequência restrita aos repos classificados como Service Weaver
    freq_weaver = explode_and_count(df[df["is_weaver"] == True])
    freq_weaver.to_csv(out / "deploy_hints_freq_weaver.csv", index=False)

    # Gráfico de barras simples (apenas is_weaver=True)
    if not freq_weaver.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(freq_weaver["hint"], freq_weaver["count"])
        ax.set_title("Deploy hints (is_weaver=True)")
        ax.set_xlabel("hint")
        ax.set_ylabel("count")
        ax.tick_params(axis='x', rotation=30)
        fig.tight_layout()
        fig.savefig(out / "deploy_hints_weaver_bar.png", dpi=160)
        plt.close(fig)

def scatter_plots(df: pd.DataFrame, out: Path):
    """
    Gera dispersões (scatter) relacionando variáveis com implements_total:
      - import_hits vs implements_total
      - interfaces_total vs implements_total
      - num_go_files_scanned vs implements_total
      - uses_run_or_init_hits vs implements_total
    Marca is_weaver=True com 'o' e False com 'x' (sem cores explícitas).
    """
    pairs = [
        ("import_hits", "implements_total"),
        ("interfaces_total", "implements_total"),
        ("num_go_files_scanned", "implements_total"),
        ("uses_run_or_init_hits", "implements_total"),
    ]

    for xcol, ycol in pairs:
        fig, ax = plt.subplots(figsize=(6, 5))
        a = df[df["is_weaver"] == True]
        b = df[df["is_weaver"] == False]
        ax.scatter(a[xcol], a[ycol], marker="o", label="is_weaver=True")
        ax.scatter(b[xcol], b[ycol], marker="x", label="is_weaver=False")
        ax.set_xlabel(xcol)
        ax.set_ylabel(ycol)
        ax.set_title(f"{ycol} vs {xcol}")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / f"scatter_{ycol}_vs_{xcol}.png", dpi=160)
        plt.close(fig)

def save_filtered_views(df: pd.DataFrame, out: Path):
    """
    Exporta dois recortes úteis para inspeção manual:
      - weaver_only.csv: apenas repositórios classificados como is_weaver=True
      - non_weaver_only.csv: apenas os demais (candidatos falsos/ruído)
    """
    df[df["is_weaver"] == True].to_csv(out / "weaver_only.csv", index=False)
    df[df["is_weaver"] == False].to_csv(out / "non_weaver_only.csv", index=False)

def main():
    """
    CLI:
      --in  : caminho para results_summary.csv
      --out : diretório de saída (será criado se não existir)
      --topn: N de linhas nas tabelas de ranking
    Pipeline:
      1) Carrega e saneia CSV
      2) Gera tabelas descritivas e correlações
      3) Gera rankings Top-N
      4) Estatísticas de deploy_hints (+ gráfico)
      5) Scatter plots
      6) Recortes filtrados
    """
    ap = argparse.ArgumentParser(description="Analisa CSV de mineração Service Weaver")
    ap.add_argument("--in", dest="csv_in", required=True, help="Caminho para o CSV (results_summary.csv)")
    ap.add_argument("--out", dest="out_dir", required=True, help="Diretório de saída para tabelas/gráficos")
    ap.add_argument("--topn", type=int, default=15, help="Top-N para tabelas de ranking")
    args = ap.parse_args()

    csv_path = Path(args.csv_in)
    out = Path(args.out_dir)
    _ensure_outdir(out)

    # Carrega e padroniza dataframe
    df = load_and_clean(csv_path)

    # Tabelas e figuras principais
    describe_tables(df, out)
    correlations(df, out)
    topn_tables(df, out, n=args.topn)
    deploy_hints_stats(df, out)
    scatter_plots(df, out)
    save_filtered_views(df, out)

    print(f"[ok] análises geradas em: {out.resolve()}")

if __name__ == "__main__":
    main()
