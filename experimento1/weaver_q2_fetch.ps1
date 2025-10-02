param(
  [string]$OutDir = "weaver_q2_out",
  [int]$Limit = 200,          # candidatos por query nesta rodada (mantenha moderado p/ evitar rate limit)
  [int]$Batch = 100,          # quantos NOVOS repositórios processar por execução
  [string]$Query1 = 'weaver.Implements[ language:Go',
  [string]$Query2 = 'weaver.Listener language:Go',
  [string]$Query3 = 'filename:go.mod "github.com/ServiceWeaver/weaver"'
)

$ErrorActionPreference = "Stop"

# ---------- HTTP / GitHub helpers ----------
$Headers = @{
  "User-Agent"           = "weaver-q2-script"
  "Accept"               = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}
if ($env:GITHUB_TOKEN) { $Headers["Authorization"] = "Bearer $env:GITHUB_TOKEN" }

function Get-Json($url) {
  Invoke-RestMethod -Headers $Headers -Uri $url -Method GET
}

function Search-RepoNames([string]$query, [int]$max) {
  $names = New-Object System.Collections.Generic.List[string]
  $perPage = 100
  for ($page = 1; $page -le 10; $page++) {
    $q = [System.Uri]::EscapeDataString($query)
    $url = "https://api.github.com/search/code?q=$q&per_page=$perPage&page=$page"
    $resp = $null
    try {
      $resp = Invoke-RestMethod -Headers $Headers -Uri $url -Method GET
    } catch {
      $status = try { $_.Exception.Response.StatusCode.Value__ } catch { 0 }
      if ($status -eq 403) {
        Write-Warning "Rate limit na busca ($query, page=$page). Aguardando 70s..."
        Start-Sleep -Seconds 70
        try { $resp = Invoke-RestMethod -Headers $Headers -Uri $url -Method GET } catch { break }
      } else {
        Write-Warning "Falha na busca ($query, page=$page): HTTP $status"; break
      }
    }
    if (-not $resp -or -not $resp.items -or $resp.items.Count -eq 0) { break }
    foreach ($it in $resp.items) {
      if ($it.repository.full_name) { [void]$names.Add($it.repository.full_name) }
    }
    if ($names.Count -ge $max) { break }
    Start-Sleep -Milliseconds 1500
  }
  $names | Select-Object -Unique | Select-Object -First $max
}

function Sanitize-PathPart([string]$name) {
  return ($name -replace '[<>:"/\\|?*]', '_')
}

function Download-RepoFiles($owner, $repo, $dstRoot) {
  try {
    $repoInfo = Get-Json "https://api.github.com/repos/$owner/$repo"
  } catch {
    Write-Warning "Não consegui ler /repos/$owner/$repo"; return $false
  }
  $branch = $repoInfo.default_branch; if (-not $branch) { $branch = "main" }

  try {
    $tree = Get-Json "https://api.github.com/repos/$owner/$repo/git/trees/$branch`?recursive=1"
  } catch {
    Write-Warning "Sem tree para $owner/$repo ($branch)"; return $false
  }
  if (-not $tree.tree) { Write-Warning "Tree vazia: $owner/$repo"; return $false }

  $paths = $tree.tree | Where-Object { $_.type -eq "blob" } | Select-Object -ExpandProperty path
  $wanted = $paths | Where-Object { ($_ -match '\.(go|ya?ml|toml)$') -and ($_ -notmatch '_test\.go$') }
  if (-not $wanted -or $wanted.Count -eq 0) { Write-Warning "Sem arquivos úteis em $owner/$repo"; return $false }

  foreach ($p in $wanted) {
    $parts = $p -split '/'
    $safeParts = $parts | ForEach-Object { Sanitize-PathPart $_ }
    $destDir = $dstRoot
    if ($safeParts.Count -gt 1) {
      foreach ($pp in $safeParts[0..($safeParts.Count-2)]) { $destDir = Join-Path $destDir $pp }
      New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    }
    $destFile = Join-Path $destDir $safeParts[-1]
    $rawUrl = "https://raw.githubusercontent.com/$owner/$repo/$branch/$p"
    try {
      Invoke-WebRequest -Headers $Headers -Uri $rawUrl -OutFile $destFile -UseBasicParsing -ErrorAction Stop | Out-Null
    } catch { Write-Warning "Falha ao baixar $rawUrl (pulando)" }
  }
  return $true
}

# ---------- Descoberta (Code Search, sem pushed:) ----------
Write-Host ">> Descobrindo repositórios via API do GitHub..."
$queries = @($Query1, $Query2, $Query3) | Where-Object { $_ -and ($_.Trim() -ne '') }

$all = @()
foreach ($q in $queries) { $all += Search-RepoNames -query $q -max $Limit }

$all = $all | Select-Object -Unique
if (-not $all -or $all.Count -eq 0) { throw "Nenhum repositório encontrado (verifique o token e as queries)." }

# controle de já vistos
$seenFile = Join-Path $OutDir "repos_seen.txt"
$seen = @()
if (Test-Path $seenFile) { $seen = Get-Content $seenFile | Where-Object { $_ } | Sort-Object -Unique }

$newRepos = $all | Where-Object { $seen -notcontains $_ } | Select-Object -First $Batch
if (-not $newRepos -or $newRepos.Count -eq 0) {
  Write-Warning "Sem novos repositórios. Ajuste queries (ou limpe/amplie 'repos_seen.txt')."
  exit 0
}

# log e atualização de 'vistos'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$runList = Join-Path $OutDir ("repos_run_{0:yyyyMMdd_HHmmss}.txt" -f (Get-Date))
$newRepos | Set-Content -Encoding utf8 $runList
Add-Content -Path $seenFile -Value ($newRepos -join [Environment]::NewLine)

$repos = $newRepos
Write-Host ">> Novos repositórios nesta rodada: $($repos.Count)"

# ---------- Download (sem clone) ----------
$Root = Join-Path $OutDir "dataset"
New-Item -ItemType Directory -Force -Path $Root | Out-Null

Write-Host ">> Baixando arquivos .go/.yaml/.toml para $Root ..."
$okCount = 0
foreach ($r in $repos) {
  if ($r -notmatch '^[^/]+/[^/]+$') { Write-Warning "Nome inválido: '$r'"; continue }
  $owner, $repo = $r.Split('/')
  $target = Join-Path $Root ($owner + "__" + $repo)
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  if (Download-RepoFiles -owner $owner -repo $repo -dstRoot $target) {
    $okCount++; Write-Host "   - OK: $r"
  } else { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
}
if ($okCount -eq 0) { throw "Nenhum repositório útil processado. Verifique rate limit e queries." }

# ---------- Verificar Go ----------
if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
  $goDefault = "C:\Program Files\Go\bin\go.exe"
  if (Test-Path $goDefault) { $env:Path += ";C:\Program Files\Go\bin" }
  if (-not (Get-Command go -ErrorAction SilentlyContinue)) { throw "Go não encontrado. Instale: winget install --id GoLang.Go -e" }
}

# ---------- Analisador em Go (AST) ----------
$anaDir = Join-Path $OutDir "cmd\weaver-metrics"
New-Item -ItemType Directory -Force -Path $anaDir | Out-Null
@'
package main
import(
 "encoding/csv";"flag";"fmt";"go/ast";"go/parser";"go/token";
 "io/fs";"os";"path/filepath";"sort";"strings"
)
type Row struct{ Repo, Pkg, CompIface string; IfaceMethods, CodeListeners int }
type pkgInfo struct{ ifaceMethods map[string]int; codeListeners int; components []string }
func main(){
 root:=flag.String("root","./dataset","pasta com os repositórios baixados")
 out :=flag.String("out","metrics_raw.csv","CSV de saída"); flag.Parse()
 var rows []Row; entries,_:=os.ReadDir(*root)
 for _,e:= range entries{ if !e.IsDir(){continue}
  repoPath:=filepath.Join(*root,e.Name()); repoName:=e.Name()
  rows=append(rows, analyzeRepo(repoName, repoPath)...)
 }
 if err:=writeCSV(*out,rows); err!=nil{ fmt.Fprintf(os.Stderr,"erro ao escrever CSV: %v\n",err); os.Exit(1) }
 fmt.Printf("OK: %s com %d linhas\n", *out, len(rows))
}
func analyzeRepo(repoName, repoPath string) []Row{
 var rows []Row; fset:=token.NewFileSet(); pkgs:=map[string]*pkgInfo{}
 _=filepath.WalkDir(repoPath, func(path string, d fs.DirEntry, err error) error{
  if err!=nil||d.IsDir(){return nil}
  if !strings.HasSuffix(path,".go"){return nil}
  if strings.Contains(path,"/vendor/")||strings.Contains(path,"/.git/"){return nil}
  file,err:=parser.ParseFile(fset,path,nil,parser.AllErrors|parser.ParseComments); if err!=nil{return nil}
  pkg:=file.Name.Name; info:=pkgs[pkg]; if info==nil{ info=&pkgInfo{ifaceMethods:map[string]int{}}; pkgs[pkg]=info}
  ast.Inspect(file, func(n ast.Node) bool{
   switch x:=n.(type){
   case *ast.TypeSpec:
    switch t:=x.Type.(type){
    case *ast.InterfaceType:
     m:=0; for _,f:=range t.Methods.List{ if _,ok:=f.Type.(*ast.FuncType); ok{ m++ } }
     info.ifaceMethods[x.Name.Name]=m
    case *ast.StructType:
     for _,f:=range t.Fields.List{
      if isWeaverListener(f.Type){ info.codeListeners++ }
      ifname:=implementsIfaceName(f.Type); if ifname!=""{ info.components=append(info.components, ifname) }
     }
    }
   }
   return true
  })
  return nil
 })
 allIfaces:=map[string]int{}; for _,p:=range pkgs{ for name,n:=range p.ifaceMethods{ allIfaces[name]=n } }
 for pkg,info:=range pkgs{
  sort.Strings(info.components); info.components=dedup(info.components)
  if len(info.components)==0 && (info.codeListeners>0){
   rows=append(rows, Row{Repo:repoName, Pkg:pkg, CompIface:"", IfaceMethods:0, CodeListeners:info.codeListeners}); continue
  }
  for _,comp:=range info.components{
   m:=info.ifaceMethods[comp]; if m==0{ if mm,ok:=allIfaces[comp]; ok{ m=mm } }
   rows=append(rows, Row{Repo:repoName, Pkg:pkg, CompIface:comp, IfaceMethods:m, CodeListeners:info.codeListeners})
  }
 }
 return rows
}
func writeCSV(path string, rows []Row) error{
 f,err:=os.Create(path); if err!=nil{ return err }; defer f.Close()
 w:=csv.NewWriter(f); defer w.Flush()
 header:=[]string{"repo","package","component_interface","methods_in_interface","code_listeners"}
 if err:=w.Write(header); err!=nil{ return err }
 for _,r:=range rows{
  rec:=[]string{ r.Repo, r.Pkg, r.CompIface, fmt.Sprint(r.IfaceMethods), fmt.Sprint(r.CodeListeners) }
  if err:=w.Write(rec); err!=nil{ return err }
 }
 return nil
}
func isWeaverListener(t ast.Expr) bool{
 switch tt:=t.(type){
 case *ast.StarExpr: return isWeaverListener(tt.X)
 case *ast.SelectorExpr:
  if id,ok:=tt.X.(*ast.Ident); ok && id.Name=="weaver" && tt.Sel.Name=="Listener"{ return true }
 }
 return false
}
func implementsIfaceName(t ast.Expr) string{
 switch tt:=t.(type){
 case *ast.IndexExpr:
  if sel,ok:=tt.X.(*ast.SelectorExpr); ok {
   if id,ok2:=sel.X.(*ast.Ident); ok2 && id.Name=="weaver" && sel.Sel.Name=="Implements" { return typeArgName(tt.Index) }
  }
 case *ast.IndexListExpr:
  if sel,ok:=tt.X.(*ast.SelectorExpr); ok {
   if id,ok2:=sel.X.(*ast.Ident); ok2 && id.Name=="weaver" && sel.Sel.Name=="Implements" && len(tt.Indices)>0 {
    return typeArgName(tt.Indices[0])
   }
  }
 }
 return ""
}
func typeArgName(e ast.Expr) string{
 switch v:=e.(type){
 case *ast.Ident: return v.Name
 case *ast.SelectorExpr: return v.Sel.Name
 default: return ""
 }
}
func dedup(ss []string) []string{
 if len(ss)==0{ return ss }; out:=ss[:0]; prev:=""
 for _,s:=range ss{ if s!=prev{ out=append(out,s); prev=s } }
 return out
}
'@ | Set-Content -Encoding utf8 (Join-Path $anaDir "main.go")

# Rodar analisador
$outDirAbs  = (Resolve-Path $OutDir).Path
$datasetAbs = Join-Path $outDirAbs "dataset"
$outAbs     = Join-Path $outDirAbs "metrics_raw.csv"

if (-not (Test-Path $datasetAbs) -or (Get-ChildItem $datasetAbs -Directory | Measure-Object).Count -eq 0) { throw "Sem dataset baixado. Verifique rate limit e queries." }

Push-Location $anaDir
try {
  $env:GO111MODULE = "auto"
  go build -o weaver-metrics.exe .\main.go
  if (-not (Test-Path .\weaver-metrics.exe)) { throw "Falha ao compilar o analisador Go." }
  .\weaver-metrics.exe -root $datasetAbs -out $outAbs
} finally { Pop-Location }
if (-not (Test-Path $outAbs)) { throw "metrics_raw.csv não foi gerado." }

# ---------- Agregação (Python + pandas) ----------
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

# instalar pandas de forma robusta e rodar
if (Get-Command py -ErrorAction SilentlyContinue) {
  py -3 -m pip install --quiet --upgrade pip
  py -3 -m pip install --quiet pandas
  py -3 $pyPath
} else {
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet pandas
  python $pyPath
}

# resumo final
if (Test-Path (Join-Path $OutDir "repos_seen.txt")) {
  $totalVistos = (Get-Content (Join-Path $OutDir "repos_seen.txt") | Sort-Object -Unique | Measure-Object).Count
  Write-Host ">> Total de repositórios já processados (acumulado): $totalVistos"
}
Write-Host ">> Pronto! Resultados em $OutDir"
Write-Host "   - metrics_raw.csv"
Write-Host "   - q2_by_repo.csv"
Write-Host "   - q2_methods_distribution.csv"
Write-Host "   - q2_components_distribution.csv"
Write-Host "   - q2_listeners_distribution.csv"
