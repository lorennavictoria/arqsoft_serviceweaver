param(
  [int]$Limit = 40,
  [string]$OutDir = "weaver_q2_out"
)

$ErrorActionPreference = "Stop"
$Root = Join-Path $OutDir "dataset"
Write-Host ">> Saída em: $OutDir | Repositórios: $Limit"
New-Item -ItemType Directory -Force -Path $Root | Out-Null

# 1) Buscar repositórios que usam Service Weaver
$tmp = New-TemporaryFile
Write-Host ">> Buscando repositórios (gh CLI)..."
gh search code 'weaver.Implements[' --language go --json repository --limit $Limit --jq '.[].repository.nameWithOwner' 2>$null | Out-File -Encoding utf8 $tmp
gh search code 'github.com/ServiceWeaver/weaver' --language go --json repository --limit $Limit --jq '.[].repository.nameWithOwner' 2>$null | Out-File -Append -Encoding utf8 $tmp

Get-Content $tmp | Sort-Object -Unique | Set-Content -Encoding utf8 (Join-Path $OutDir "repos.txt")
$reposFile = Join-Path $OutDir "repos.txt"
$repos = Get-Content $reposFile | Where-Object { $_ -ne "" }
Write-Host ">> Repositórios únicos encontrados:" $repos.Count

# 2) Clonar
Write-Host ">> Clonando repositórios para $Root ..."
$cloned = @()
foreach ($r in $repos) {
  $target = Join-Path $Root ($r -replace '/', '__')
  if (Test-Path (Join-Path $target ".git")) { Write-Host "   - já existe: $r"; $cloned += $target; continue }
  try {
    gh repo clone $r $target -- --depth=1 | Out-Null
    $cloned += $target
  } catch {
    Write-Warning "   ! falha ao clonar $r (provável nome de arquivo inválido p/ Windows). Pulando."
    if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
    continue
  }
}


# 3) Gerar analisador em Go (AST)
$anaDir = Join-Path $OutDir "cmd/weaver-metrics"
New-Item -ItemType Directory -Force -Path $anaDir | Out-Null
$goMain = @'
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
	ifaceMethods  map[string]int
	codeListeners int
	components    []string
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
					m := 0
					for _, f := range t.Methods.List {
						if _, ok := f.Type.(*ast.FuncType); ok { m++ }
					}
					info.ifaceMethods[x.Name.Name] = m

				case *ast.StructType:
					for _, f := range t.Fields.List {
						if isWeaverListener(f.Type) { info.codeListeners++ }
						ifname := implementsIfaceName(f.Type)
						if ifname != "" { info.components = append(info.components, ifname) }
					}
				}
			}
			return true
		})
		return nil
	})

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
	case *ast.IndexListExpr:
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
'@
Set-Content -Encoding utf8 (Join-Path $anaDir "main.go") $goMain



# 4) Rodar analisador (gera metrics_raw.csv)
Write-Host ">> Rodando analisador em Go..."

# Caminhos absolutos corretos
$outDirAbs = (Resolve-Path $OutDir).Path
$datasetAbs = Join-Path $outDirAbs "dataset"
$outAbs = Join-Path $outDirAbs "metrics_raw.csv"

# Sanity check: pelo menos 1 repo clonado
if (-not (Test-Path $datasetAbs) -or (Get-ChildItem $datasetAbs -Directory | Measure-Object).Count -eq 0) {
  Write-Warning "Nenhum repositório aproveitável foi clonado. Dica: remova do 'repos.txt' os que falharam ou use WSL."
  exit 1
}

Push-Location $anaDir
go run . -root $datasetAbs -out $outAbs
Pop-Location


# 5) Agregar (Python + pandas)
# ---------- Agregação (Python + pandas) ----------
if (-not (Test-Path (Join-Path $OutDir "metrics_raw.csv"))) {
  throw "metrics_raw.csv não existe. Verifique a etapa do analisador Go."
}

$pyPath = Join-Path $OutDir "summarize_q2.py"
@'
import pandas as pd
from pathlib import Path
root = Path(__file__).parent
raw = pd.read_csv(root / "metrics_raw.csv")
raw["repo"] = raw["repo"].str.replace("__", "/", regex=False)

comp_repo = (raw[raw["component_interface"].notna() & (raw["component_interface"] != "")]
             .groupby("repo")["component_interface"].nunique()
             .rename("n_components").reset_index())

methods = raw[raw["component_interface"].notna() & (raw["component_interface"] != "")]
methods_by_repo = methods.groupby("repo")["methods_in_interface"].agg(
    n_ifaces="count", min_methods="min",
    p25=lambda s: s.quantile(0.25), median_methods="median",
    p75=lambda s: s.quantile(0.75), max_methods="max", mean_methods="mean"
).reset_index()

listeners_pkg = raw.groupby(["repo","package"])["code_listeners"].max().reset_index()
listeners_repo = listeners_pkg.groupby("repo")["code_listeners"].sum().rename("code_listeners_total").reset_index()

summary = comp_repo.merge(methods_by_repo, on="repo", how="outer").merge(listeners_repo, on="repo", how="outer").fillna(0).sort_values("repo")
summary.to_csv(root / "q2_by_repo.csv", index=False)

dist_methods = methods["methods_in_interface"].value_counts().sort_index().rename_axis("methods").reset_index(name="count")
dist_methods.to_csv(root / "q2_methods_distribution.csv", index=False)

dist_components = comp_repo["n_components"].value_counts().sort_index().rename_axis("n_components").reset_index(name="count")
dist_components.to_csv(root / "q2_components_distribution.csv", index=False)

dist_listeners = listeners_repo["code_listeners_total"].value_counts().sort_index().rename_axis("code_listeners_total").reset_index(name="count")
dist_listeners.to_csv(root / "q2_listeners_distribution.csv", index=False)

print("Gerados: q2_by_repo.csv, q2_methods_distribution.csv, q2_components_distribution.csv, q2_listeners_distribution.csv")
'@ | Set-Content -Encoding utf8 $pyPath

# Instala pandas de forma direta (evita importlib.util e “sombras” locais)
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pandas

python $pyPath


Write-Host ">> Pronto!"
Write-Host "CSV principais:"
Write-Host " - $OutDir\metrics_raw.csv"
Write-Host " - $OutDir\q2_by_repo.csv"
Write-Host " - $OutDir\q2_methods_distribution.csv"
Write-Host " - $OutDir\q2_components_distribution.csv"
Write-Host " - $OutDir\q2_listeners_distribution.csv"
