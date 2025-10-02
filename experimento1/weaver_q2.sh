#!/usr/bin/env bash
set -euo pipefail

LIMIT="${1:-10}"         # quantos repositórios buscar
OUTDIR="${2:-weaver_q2_out}"
ROOT="${OUTDIR}/dataset"

echo ">> Saída em: ${OUTDIR} | Repositórios: ${LIMIT}"
mkdir -p "${ROOT}"

# 1) Descoberta de repositórios (duas consultas para aumentar recall)
TMP_REPOS="$(mktemp)"
echo ">> Buscando repositórios no GitHub (gh CLI)..."
gh search code 'weaver.Implements[' --language go --json repository --limit "${LIMIT}" \
  --jq '.[].repository.nameWithOwner' > "${TMP_REPOS}" || true
gh search code 'github.com/ServiceWeaver/weaver' --language go --json repository --limit "${LIMIT}" \
  --jq '.[].repository.nameWithOwner' >> "${TMP_REPOS}" || true
sort -u "${TMP_REPOS}" > "${OUTDIR}/repos.txt"
NREPOS=$(wc -l < "${OUTDIR}/repos.txt" | tr -d ' ')
echo ">> Repositórios únicos encontrados: ${NREPOS}"

# 2) Clonagem
echo ">> Clonando repositórios para ${ROOT}..."
while read -r r; do
  [ -z "$r" ] && continue
  TARGET="${ROOT}/$(echo "$r" | sed 's|/|__|g')"
  if [ -d "${TARGET}/.git" ]; then
    echo "   - já existe: $r"
    continue
  fi
  gh repo clone "$r" "$TARGET" -- --depth=1 || echo "   ! falha ao clonar $r (ignorando)"
done < "${OUTDIR}/repos.txt"

# 3) Gerar o analisador em Go (AST)
ANADIR="${OUTDIR}/cmd/weaver-metrics"
mkdir -p "${ANADIR}"
cat > "${ANADIR}/main.go" <<'GO'
package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type Row struct {
	Repo          string
	Pkg           string
	CompIface     string
	IfaceMethods  int
	CodeListeners int
}

type pkgInfo struct {
	ifaceMethods  map[string]int // interface -> #métodos
	codeListeners int
	components    []string       // interfaces em weaver.Implements[...]
}

func main() {
	root := flag.String("root", "./dataset", "pasta com os repositórios clonados")
	out  := flag.String("out", "metrics_raw.csv", "CSV de saída")
	flag.Parse()

	var rows []Row
	entries, _ := os.ReadDir(*root)
	for _, e := range entries {
		if !e.IsDir() { continue }
		repoPath := filepath.Join(*root, e.Name())
		repoName := e.Name()
		repoRows := analyzeRepo(repoName, repoPath)
		rows = append(rows, repoRows...)
	}
	if err := writeCSV(*out, rows); err != nil {
		fmt.Fprintf(os.Stderr, "erro ao escrever CSV: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("OK: %s com %d linhas\n", *out, len(rows))
}

func analyzeRepo(repoName, repoPath string) []Row {
	var rows []Row
	fset := token.NewFileSet()
	// Map pacote -> info
	pkgs := map[string]*pkgInfo{}

	_ = filepath.WalkDir(repoPath, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() { return nil }
		if !strings.HasSuffix(path, ".go") { return nil }
		if strings.Contains(path, "/vendor/") || strings.Contains(path, "/.git/") { return nil }

		file, err := parser.ParseFile(fset, path, nil, parser.AllErrors|parser.ParseComments)
		if err != nil { return nil }

		pkg := file.Name.Name
		info := pkgs[pkg]
		if info == nil {
			info = &pkgInfo{ifaceMethods: map[string]int{}}
			pkgs[pkg] = info
		}

		ast.Inspect(file, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.TypeSpec:
				switch t := x.Type.(type) {
				case *ast.InterfaceType:
					// type X interface { ... }
					m := 0
					for _, f := range t.Methods.List {
						if _, ok := f.Type.(*ast.FuncType); ok { m++ }
					}
					info.ifaceMethods[x.Name.Name] = m

				case *ast.StructType:
					// olhar campos do struct
					for _, f := range t.Fields.List {
						// listeners
						if isWeaverListener(f.Type) { info.codeListeners++ }
						// implements
						ifname := implementsIfaceName(f.Type)
						if ifname != "" { info.components = append(info.components, ifname) }
					}
				}
			}
			return true
		})
		return nil
	})

	// mapa global de interfaces -> métodos (pra resolver cross-package best-effort)
	allIfaces := map[string]int{}
	for _, p := range pkgs {
		for name, n := range p.ifaceMethods { allIfaces[name] = n }
	}

	for pkg, info := range pkgs {
		sort.Strings(info.components)
		info.components = dedup(info.components)
		if len(info.components) == 0 && (info.codeListeners > 0) {
			rows = append(rows, Row{
				Repo: repoName, Pkg: pkg, CompIface: "",
				IfaceMethods: 0, CodeListeners: info.codeListeners,
			})
			continue
		}
		for _, comp := range info.components {
			m := info.ifaceMethods[comp]
			if m == 0 {
				if mm, ok := allIfaces[comp]; ok { m = mm }
			}
			rows = append(rows, Row{
				Repo: repoName, Pkg: pkg, CompIface: comp,
				IfaceMethods: m, CodeListeners: info.codeListeners,
			})
		}
	}
	return rows
}

func writeCSV(path string, rows []Row) error {
	f, err := os.Create(path); if err != nil { return err }
	defer f.Close()
	w := csv.NewWriter(f); defer w.Flush()

	header := []string{"repo","package","component_interface","methods_in_interface","code_listeners"}
	if err := w.Write(header); err != nil { return err }
	for _, r := range rows {
		rec := []string{
			r.Repo, r.Pkg, r.CompIface,
			fmt.Sprint(r.IfaceMethods),
			fmt.Sprint(r.CodeListeners),
		}
		if err := w.Write(rec); err != nil { return err }
	}
	return nil
}

func isWeaverListener(t ast.Expr) bool {
	switch tt := t.(type) {
	case *ast.StarExpr:
		return isWeaverListener(tt.X)
	case *ast.SelectorExpr:
		if id, ok := tt.X.(*ast.Ident); ok && id.Name == "weaver" && tt.Sel.Name == "Listener" {
			return true
		}
	}
	return false
}

func implementsIfaceName(t ast.Expr) string {
	switch tt := t.(type) {
	case *ast.IndexExpr:
		if sel, ok := tt.X.(*ast.SelectorExpr); ok {
			if id, ok2 := sel.X.(*ast.Ident); ok2 && id.Name == "weaver" && sel.Sel.Name == "Implements" {
				return typeArgName(tt.Index)
			}
		}
	case *ast.IndexListExpr: // genéricos (Go 1.18+)
		if sel, ok := tt.X.(*ast.SelectorExpr); ok {
			if id, ok2 := sel.X.(*ast.Ident); ok2 && id.Name == "weaver" && sel.Sel.Name == "Implements" && len(tt.Indices) > 0 {
				return typeArgName(tt.Indices[0])
			}
		}
	}
	return ""
}

func typeArgName(e ast.Expr) string {
	switch v := e.(type) {
	case *ast.Ident:
		return v.Name
	case *ast.SelectorExpr:
		return v.Sel.Name
	default:
		return ""
	}
}

func dedup(ss []string) []string {
	if len(ss) == 0 { return ss }
	out := ss[:0]
	prev := ""
	for _, s := range ss {
		if s != prev {
			out = append(out, s)
			prev = s
		}
	}
	return out
}
GO

# 4) Rodar o analisador (gera metrics_raw.csv)
echo ">> Rodando analisador em Go..."
pushd "${ANADIR}" >/dev/null
go run . -root "../../dataset" -out "../../metrics_raw.csv"
popd >/dev/null

# 5) Agregação em Python (distribuições por repositório)
echo ">> Criando agregador em Python..."
cat > "${OUTDIR}/summarize_q2.py" <<'PY'
import pandas as pd
from pathlib import Path

root = Path(__file__).parent
raw = pd.read_csv(root / "metrics_raw.csv")

# Normalizar nome de repositório (volta de dir 'owner__repo')
raw["repo"] = raw["repo"].str.replace("__", "/", regex=False)

# Componentes por repo (distintos)
comp_repo = (
    raw[raw["component_interface"].notna() & (raw["component_interface"] != "")]
    .groupby("repo")["component_interface"].nunique()
    .rename("n_components")
    .reset_index()
)

# Métodos por interface (para distribuição global e média por repo)
methods = raw[raw["component_interface"].notna() & (raw["component_interface"] != "")]
methods_by_repo = methods.groupby("repo")["methods_in_interface"].agg(
    n_ifaces="count",
    min_methods="min",
    p25=lambda s: s.quantile(0.25),
    median_methods="median",
    p75=lambda s: s.quantile(0.75),
    max_methods="max",
    mean_methods="mean",
).reset_index()

# Listeners no código: somar por pacote e repo, depois consolidar por repo
listeners_pkg = raw.groupby(["repo","package"])["code_listeners"].max().reset_index()
listeners_repo = listeners_pkg.groupby("repo")["code_listeners"].sum().rename("code_listeners_total").reset_index()

# Consolidado por repo
summary = comp_repo.merge(methods_by_repo, on="repo", how="outer").merge(listeners_repo, on="repo", how="outer").fillna(0)
summary = summary.sort_values("repo")
summary.to_csv(root / "q2_by_repo.csv", index=False)

# Distribuições globais para gráficos/tabelas rápidas
dist_methods = methods["methods_in_interface"].value_counts().sort_index().rename_axis("methods").reset_index(name="count")
dist_methods.to_csv(root / "q2_methods_distribution.csv", index=False)

dist_components = comp_repo["n_components"].value_counts().sort_index().rename_axis("n_components").reset_index(name="count")
dist_components.to_csv(root / "q2_components_distribution.csv", index=False)

dist_listeners = listeners_repo["code_listeners_total"].value_counts().sort_index().rename_axis("code_listeners_total").reset_index(name="count")
dist_listeners.to_csv(root / "q2_listeners_distribution.csv", index=False)

print("Gerados:")
for f in ["q2_by_repo.csv","q2_methods_distribution.csv","q2_components_distribution.csv","q2_listeners_distribution.csv"]:
    print(" -", f)
PY

echo ">> Rodando agregador (Python)..."
python3 - <<'PY'
import sys, subprocess, os
try:
    import pandas  # noqa
except Exception:
    print("Instalando pandas...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "pandas"])
PY
python3 "${OUTDIR}/summarize_q2.py"

echo ">> Pronto!"
echo "CSV principais:"
echo " - ${OUTDIR}/metrics_raw.csv            (linhas por componente/pacote)"
echo " - ${OUTDIR}/q2_by_repo.csv             (resumo por repositório)"
echo " - ${OUTDIR}/q2_methods_distribution.csv (distribuição global de métodos por interface)"
echo " - ${OUTDIR}/q2_components_distribution.csv (distribuição global de componentes por repo)"
echo " - ${OUTDIR}/q2_listeners_distribution.csv (distribuição global de listeners por repo)"
