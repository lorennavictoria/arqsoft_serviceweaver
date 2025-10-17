<# 
Script Q2 — mineração e análise de projetos Service Weaver (Go)

Fluxo:
1) Busca repositórios no GitHub Code Search por 3 queries (sem filtro pushed:)
2) Deduplica nomes e controla repositórios já processados (repos_seen.txt)
3) Baixa só arquivos úteis (.go/.yaml/.yml/.toml) via raw.githubusercontent (sem git clone)
4) Gera um analisador em Go (AST) que extrai métricas (components/listeners/métodos)
5) Compila e executa o analisador -> metrics_raw.csv
6) Roda Python/pandas para agregações e distribuições -> q2_*.csv

Observações:
- Respeita rate limit (esperas e retry em 403)
- Usa token via env GITHUB_TOKEN se disponível
- Evita _test.go
- Garante caminho seguro em disco (sanitize)
#>

param(
  # Diretório de saída raiz
  [string]$OutDir = "weaver_q2_out",

  # Máximo de repositórios por query (coleta por página, limitado a 10 páginas * 100)
  [int]$Limit = 200,

  # Quantos repositórios NOVOS processar nesta execução (amostra incremental)
  [int]$Batch = 100,

  # Três queries de Code Search no GitHub (Go/Service Weaver)
  [string]$Query1 = 'weaver.Implements[ language:Go',
  [string]$Query2 = 'weaver.Listener language:Go',
  [string]$Query3 = 'filename:go.mod "github.com/ServiceWeaver/weaver"'
)

# Interrompe execução ao primeiro erro não tratado
$ErrorActionPreference = "Stop"

# ---------- HTTP / GitHub helpers ----------
# Cabeçalhos padrão para a API do GitHub
$Headers = @{
  "User-Agent"           = "weaver-q2-script"
  "Accept"               = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}

# Autenticação opcional via token de ambiente (aumenta limites e confiabilidade)
if ($env:GITHUB_TOKEN) { $Headers["Authorization"] = "Bearer $env:GITHUB_TOKEN" }

# Pequeno helper para GET JSON
function Get-Json($url) {
  Invoke-RestMethod -Headers $Headers -Uri $url -Method GET
}

# Busca nomes "owner/repo" via Code Search (por arquivo), com paginação e retry em 403
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
      # Se bater rate limit (403), espera 70s e tenta 1x; caso contrário, aborta essa query
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
    # Pausa curta para evitar limites/agressividade
    Start-Sleep -Milliseconds 1500
  }
  # Dedup e corte ao máximo solicitado
  $names | Select-Object -Unique | Select-Object -First $max
}

# Normaliza partes de caminho para evitar caracteres inválidos no Windows
function Sanitize-PathPart([string]$name) {
  return ($name -replace '[<>:"/\\|?*]', '_')
}

# Baixa arquivos (sem fazer clone) com base na tree do repositório em sua default branch
function Download-RepoFiles($owner, $repo, $dstRoot) {
  try {
    $repoInfo = Get-Json "https://api.github.com/repos/$owner/$repo"
  } catch {
    Write-Warning "Não consegui ler /repos/$owner/$repo"; return $false
  }
  $branch = $repoInfo.default_branch; if (-not $branch) { $branch = "main" }

  try {
    # Obtém a árvore (conteúdo) recursivamente da branch
    $tree = Get-Json "https://api.github.com/repos/$owner/$repo/git/trees/$branch`?recursive=1"
  } catch {
    Write-Warning "Sem tree para $owner/$repo ($branch)"; return $false
  }
  if (-not $tree.tree) { Write-Warning "Tree vazia: $owner/$repo"; return $false }

  # Filtra blobs (arquivos) e separa só extensões úteis, excluindo *_test.go
  $paths = $tree.tree | Where-Object { $_.type -eq "blob" } | Select-Object -ExpandProperty path
  $wanted = $paths | Where-Object { ($_ -match '\.(go|ya?ml|toml)$') -and ($_ -notmatch '_test\.go$') }
  if (-not $wanted -or $wanted.Count -eq 0) { Write-Warning "Sem arquivos úteis em $owner/$repo"; return $false }

  foreach ($p in $wanted) {
    # Reconstrói diretórios respeitando hierarquia, porém sanitizando nomes
    $parts = $p -split '/'
    $safeParts = $parts | ForEach-Object { Sanitize-PathPart $_ }
    $destDir = $dstRoot
    if ($safeParts.Count -gt 1) {
      foreach ($pp in $safeParts[0..($safeParts.Count-2)]) { $destDir = Join-Path $destDir $pp }
      New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    }
    $destFile = Join-Path $destDir $safeParts[-1]

    # Faz download direto do raw (evita git clone)
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

# Coleta todos os nomes "owner/repo" das queries e deduplica
$all = @()
foreach ($q in $queries) { $all += Search-RepoNames -query $q -max $Limit }
$all = $all | Select-Object -Unique
if (-not $all -or $all.Count -eq 0) { throw "Nenhum repositório encontrado (verifique o token e as queries)." }

# ---------- Controle de já vistos (incremental) ----------
$seenFile = Join-Path $OutDir "repos_seen.txt"
$seen = @()
if (Test-Path $seenFile) { $seen = Get-Content $seenFile | Where-Object { $_ } | Sort-Object -Unique }

# Seleciona apenas NOVOS repositórios (não vistos) até o limite Batch
$newRepos = $all | Where-Object { $seen -notcontains $_ } | Select-Object -First $Batch
if (-not $newRepos -or $newRepos.Count -eq 0) {
  Write-Warning "Sem novos repositórios. Ajuste queries (ou limpe/amplie 'repos_seen.txt')."
  exit 0
}

# Loga a rodada e atualiza 'repos_seen.txt'
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
  } else {
    # Remove diretório vazio se falhou baixar algo útil
    Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue
  }
}
if ($okCount -eq 0) { throw "Nenhum repositório útil processado. Verifique rate limit e queries." }

# ---------- Verificar Go instalado ----------
# Tenta localizar 'go'; se não, injeta caminho padrão do Windows; se persistir, aborta com dica de instalação
if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
  $goDefault = "C:\Program Files\Go\bin\go.exe"
  if (Test-Path $goDefault) { $env:Path += ";C:\Program Files\Go\bin" }
  if (-not (Get-Command go -ErrorAction SilentlyContinue)) { throw "Go não encontrado. Instale: winget install --id GoLang.Go -e" }
}

# ---------- Analisador em Go (AST) ----------
# Gera o arquivo main.go do analisador sob OutDir\cmd\weaver-metrics
$anaDir = Join-Path $OutDir "cmd\weaver-metrics"
New-Item -ItemType Directory -Force -Path $anaDir | Out-Null

@'
... (GO CODE GERADO ABAIXO, TAMBÉM COMENTADO) ...
'@ | Set-Content -Encoding utf8 (Join-Path $anaDir "main.go")

# Ajusta caminhos absolutos e executa build + run do analisador
$outDirAbs  = (Resolve-Path $OutDir).Path
$datasetAbs = Join-Path $outDirAbs "dataset"
$outAbs     = Join-Path $outDirAbs "metrics_raw.csv"

if (-not (Test-Path $datasetAbs) -or (Get-ChildItem $datasetAbs -Directory | Measure-Object).Count -eq 0) { throw "Sem dataset baixado. Verifique rate limit e queries." }

Push-Location $anaDir
try {
  $env:GO111MODULE = "auto" # permite build simples sem go.mod local
  go build -o weaver-metrics.exe .\main.go
  if (-not (Test-Path .\weaver-metrics.exe)) { throw "Falha ao compilar o analisador Go." }
  .\weaver-metrics.exe -root $datasetAbs -out $outAbs
} finally { Pop-Location }
if (-not (Test-Path $outAbs)) { throw "metrics_raw.csv não foi gerado." }

# ---------- Agregação (Python + pandas) ----------
# Gera script Python que sumariza por repo e cria distribuições
$pyPath = Join-Path $OutDir "summarize_q2.py"
@'
... (PYTHON CODE GERADO ABAIXO, TAMBÉM COMENTADO) ...
'@ | Set-Content -Encoding utf8 $pyPath

# Instala pip/pandas de forma robusta em Windows (py launcher) ou genérico (python)
if (Get-Command py -ErrorAction SilentlyContinue) {
  py -3 -m pip install --quiet --upgrade pip
  py -3 -m pip install --quiet pandas
  py -3 $pyPath
} else {
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet pandas
  python $pyPath
}

# ---------- Resumo final ----------
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
