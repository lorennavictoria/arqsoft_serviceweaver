# Service Weaver Mining (README)

Coleta repositórios no GitHub que usam **Service Weaver** (Go), extrai sinais de uso e gera análises em CSV/PNG.

## Requisitos

- Python 3.9+
- Pacotes: `requests`, `tqdm`, `pandas`, `numpy`, `matplotlib`
- (Recomendado) Token do GitHub:
  ```bash
  export GITHUB_TOKEN=ghp_xxx
  ```

Instalação dos pacotes:
```bash
python -m pip install --upgrade pip
python -m pip install requests tqdm pandas numpy matplotlib
```

## Scripts

- `minerador.py` — minerador base (gera `results.jsonl` e `results_summary.csv`).
- `analisar.py` — análises para responder às questões de implantação e métricas estruturais.

## Uso Rápido

### 1) Mineração

Minerador base:
```bash
export GITHUB_TOKEN=ghp_xxx
python minerador.py --target 900 --out ./dados_coletados --min-sleep 1.0 [--strict] [--resume]
```

Saída típica (`./dados_coletados`):
```
repos_list.txt
results.jsonl
results_summary.csv
progress.json
# variante com classificador:
results_weaver.jsonl
results_weaver.csv
repos_weaver.txt
```

### 2) Análise

```bash
python analisar.py   --summary ./dados_coletados/results_summary.csv   --jsonl ./dados_coletados/results.jsonl   --out ./analise   --plots
```

Saída:
```
q1a_deploy_hints_counts.csv
q1b_resource_spec_presence.csv
q1c_listener_attr_presence.csv
q1d_independent_deployments.csv
q1d_independent_deployments_agg.csv
q2_components_summary.csv
q2_interfaces_methods_distribution.csv   # se --jsonl
q2_listeners_presence.csv
*.png                                     # se --plots
```

## Campos Principais (results_summary.csv)

- `repo` | `is_weaver` (na variante)
- `num_go_files_scanned`, `num_config_files_scanned`
- `implements_total`, `interfaces_total`
- `has_any_listener_field`, `has_any_resource_spec`
- `import_hits`, `uses_run_or_init_hits`
- `deploy_hints` (ex.: `single,multi,kube`)

## Limitações

- Atributos de listeners (`public`, `hostname`, `address`) não são extraídos pelos mineradores padrão; o analisador gera um placeholder (`q1c_listener_attr_presence.csv`).
- Heurísticas por regex podem superestimar (`implements_total`, `interfaces_total`) e gerar falsos positivos em `deploy_hints`.

## Licença

Defina a licença do projeto (ex.: MIT) e inclua o arquivo `LICENSE`.
