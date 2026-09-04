<#
Coleta autonoma da fatura mais recente (Windows).

Mantem o Chrome de pe (ele cai em execucoes longas) e reexecuta a coleta ate
conseguir. A pausa entre tentativas de login fica por conta do baixar_fatura.py;
aqui so garantimos que o navegador exista e que a falha de um ciclo nao encerre
o processo.

Uso:
    .\coletar.ps1                      # sem rotacao de IP
    .\coletar.ps1 -Rotacionar          # com tethering USB (exige Administrador)
#>
[CmdletBinding()]
param(
    [switch]$Rotacionar,
    [string]$Script = 'baixar_fatura.py',   # ou coletar_varias.py p/ as 5 UCs
    [int]$Ciclos = $(if ($env:CICLOS) { [int]$env:CICLOS } else { 4 }),
    [int]$Port = $(if ($env:CDP_PORT) { [int]$env:CDP_PORT } else { 9222 })
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# --- credenciais -----------------------------------------------------------
# credenciais.env continua no formato do shell ("export CHAVE=valor"), para o
# arquivo servir aos dois mundos sem duplicacao.
$envFile = Join-Path $PSScriptRoot 'credenciais.env'
if (-not (Test-Path $envFile)) {
    throw "credenciais.env nao existe. Copie credenciais.env.exemplo e preencha."
}
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') {
        $valor = $matches[2].Trim('"').Trim("'")
        Set-Item -Path "env:$($matches[1])" -Value $valor
    }
}

# --- parametros de execucao ------------------------------------------------
if (-not $env:EQUATORIAL_PRAZO)      { $env:EQUATORIAL_PRAZO = '21600' }   # 6h por ciclo
if (-not $env:EQUATORIAL_ESPERA)     { $env:EQUATORIAL_ESPERA = '20' }     # pausa curta e fixa
if (-not $env:EQUATORIAL_ESPERA_MAX) { $env:EQUATORIAL_ESPERA_MAX = '45' }
if (-not $env:EQUATORIAL_DWELL)      { $env:EQUATORIAL_DWELL = '20' }
if (-not $env:CHROME_PROFILE)        { $env:CHROME_PROFILE = Join-Path $PSScriptRoot 'chrome-profile' }
$env:CDP_PORT = "$Port"

if ($Rotacionar) {
    # Set-NetIPInterface exige elevacao. Falhar aqui, antes de qualquer coisa, e
    # muito melhor do que descobrir isso na primeira recusa de login - la o
    # script ja teria uma sessao de meia hora investida.
    $admin = ([Security.Principal.WindowsPrincipal]`
              [Security.Principal.WindowsIdentity]::GetCurrent()`
             ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        throw ("Rotacao de IP exige terminal como Administrador " +
               "(Set-NetIPInterface). Rode sem -Rotacionar ou reabra elevado.")
    }
    # Mesma busca que rede.py faz: PATH, platform-tools no perfil, SDK do Android.
    $adb = @(
        $env:EQUATORIAL_ADB
        (Get-Command adb -ErrorAction SilentlyContinue).Source
        "$env:USERPROFILE\platform-tools\adb.exe"
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $adb) {
        throw ("adb nao encontrado. Baixe o platform-tools em " +
               "https://developer.android.com/tools/releases/platform-tools e " +
               "descompacte em $env:USERPROFILE\platform-tools")
    }
    $env:EQUATORIAL_ADB = $adb
    & $adb devices | Out-Host
    $env:EQUATORIAL_ROTACIONAR = '1'
    Write-Host "Rotacao de IP LIGADA. Enquanto rodar, TODO o trafego da maquina sai pelo celular."
} else {
    $env:EQUATORIAL_ROTACIONAR = '0'
}

$python = if (Test-Path '.venv\Scripts\python.exe') { '.venv\Scripts\python.exe' } else { 'python' }

function Test-Cdp {
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
            "http://127.0.0.1:$Port/json/version" | Out-Null
        return $true
    } catch { return $false }
}

function Confirm-Chrome {
    if (Test-Cdp) { return $true }
    Write-Host "[$(Get-Date -Format HH:mm:ss)] Chrome fora do ar; subindo"
    # Mata so o Chrome deste perfil - nao o navegador pessoal do usuario. O
    # equivalente do pkill -f: filtrar pela linha de comando.
    Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($env:CHROME_PROFILE) } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    Start-Sleep -Seconds 3
    & (Join-Path $PSScriptRoot 'chrome.ps1') -Port $Port -Perfil $env:CHROME_PROFILE
    return (Test-Cdp)
}

New-Item -ItemType Directory -Force 'saida', 'faturas' | Out-Null

try {
    for ($ciclo = 1; $ciclo -le $Ciclos; $ciclo++) {
        Write-Host "########## Ciclo $ciclo/$Ciclos - $(Get-Date -Format 'dd/MM HH:mm:ss') ##########"
        if (-not (Confirm-Chrome)) { Start-Sleep -Seconds 60; continue }

        & $python $Script
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[$(Get-Date -Format HH:mm:ss)] Fatura baixada; encerrando"
            Get-ChildItem faturas\*.pdf -ErrorAction SilentlyContinue |
                Format-Table Name, Length, LastWriteTime
            exit 0
        }
        Write-Host "[$(Get-Date -Format HH:mm:ss)] Ciclo $ciclo sem sucesso"
    }
    Write-Host "Todos os $Ciclos ciclos falharam"
    exit 1
} finally {
    # Rede de seguranca: baixar_fatura.py ja restaura as metricas no seu proprio
    # finally, mas se ele for morto de forma dura (Stop-Process, crash do
    # interpretador) a maquina ficaria roteando tudo pelo 4G. Restaurar duas
    # vezes e inofensivo; nao restaurar nenhuma nao e.
    if ($Rotacionar) {
        Get-NetIPInterface -AddressFamily IPv4 |
            Where-Object { $_.InterfaceMetric -eq 1 -and $_.AutomaticMetric -eq 'Disabled' } |
            ForEach-Object {
                Write-Host "Restaurando metrica automatica em $($_.InterfaceAlias)"
                Set-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4 `
                    -AutomaticMetric Enabled -ErrorAction SilentlyContinue
            }
    }
}
