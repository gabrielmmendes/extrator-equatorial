<#
Ponto de entrada do robo (Windows).

Substitui o coletar.ps1 no fluxo de varias UCs. A diferenca de fundo: o
coletar.ps1 mantinha UM Chrome de pe e reexecutava a coleta ate conseguir; aqui
quem manda e o orquestrador.py, que sobe e derruba um Chrome POR PERSONA e
espalha as contas ao longo do mes. Nao existe mais "ciclo que falha e repete
agora" - a repeticao virou agenda, em escala de horas.

O coletar.ps1 continua servindo para o caso de uma UC unica e imediata.

Uso:
    .\robo.ps1                       # laco continuo (modo normal)
    .\robo.ps1 -Rotacionar           # idem, trocando o IP pelo celular
    .\robo.ps1 -Modo status          # relatorio da agenda
    .\robo.ps1 -Modo maturar         # so prepara perfis novos
    .\robo.ps1 -Modo uma             # roda no maximo uma conta e sai
    .\robo.ps1 -Modo agora -Uc 000176588701258   # forca uma UC
#>
[CmdletBinding()]
param(
    [switch]$Rotacionar,
    [ValidateSet('laco', 'uma', 'maturar', 'status', 'agora')]
    [string]$Modo = 'laco',
    [string]$Uc,
    # Mes da fatura (MM/AAAA). Vazio = a mais recente. So vale com -Modo agora:
    # no laco quem escolhe o mes e a agenda.
    [string]$Mes
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# credenciais.env agora carrega so os ajustes (horarios, tetos). As contas em si
# vivem em contas.json, porque dezenas de UCs nao cabem em variavel de ambiente.
$envFile = Join-Path $PSScriptRoot 'credenciais.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') {
            Set-Item -Path "env:$($matches[1])" -Value $matches[2].Trim('"').Trim("'")
        }
    }
}
# A fila de UCs vem da API do Minha Fatura quando ha MF_API_KEY; contas.json so
# e exigido quando nao ha chave (uso manual, ou API fora do ar de proposito).
$viaApi = $env:MF_API_KEY -and $env:MF_FONTE -ne 'json'
if (-not $viaApi -and -not (Test-Path (Join-Path $PSScriptRoot 'contas.json'))) {
    throw ("Sem MF_API_KEY e sem contas.json: nao ha de onde tirar as UCs. " +
           "Defina a chave da API em credenciais.env ou crie o contas.json.")
}

if ($Rotacionar) {
    # Set-NetIPInterface exige elevacao. Falhar aqui, antes de qualquer coisa, e
    # melhor do que descobrir no meio de uma sessao ja investida.
    # Sem Admin nao da para FORCAR a rota (Set-NetIPInterface), mas isso so
    # importa quando o tether perde na metrica para o Wi-Fi. A rotacao em si e
    # feita pelo adb, que nao pede elevacao - entao aviso e sigo, em vez de
    # abortar uma execucao que provavelmente funcionaria.
    $admin = ([Security.Principal.WindowsPrincipal]`
              [Security.Principal.WindowsIdentity]::GetCurrent()`
             ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        Write-Warning ("Sem Administrador: nao da para forcar a rota pelo " +
            "celular. Confira com 'python rede.py' se o IP publico ja e da " +
            "operadora; se for, o tether ja e a rota padrao e esta tudo certo.")
    }
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
$argumentos = switch ($Modo) {
    'laco'    { @() }
    'uma'     { @('--uma') }
    'maturar' { @('--maturar') }
    'status'  { @('--status') }
    'agora'   {
        if (-not $Uc) { throw "-Modo agora exige -Uc <numero da UC>" }
        @('--agora', $Uc)
    }
}
if ($Mes) {
    if ($Modo -ne 'agora') { throw "-Mes so vale com -Modo agora" }
    $argumentos += @('--mes', $Mes)
}

New-Item -ItemType Directory -Force 'faturas', 'saida', 'estado', 'perfis' | Out-Null

try {
    & $python 'orquestrador.py' @argumentos
    exit $LASTEXITCODE
} finally {
    # Rede de seguranca: o orquestrador ja restaura as metricas no proprio
    # finally, mas se ele for morto de forma dura a maquina ficaria roteando
    # tudo pelo 4G. Restaurar duas vezes e inofensivo; nao restaurar nao e.
    # So os adaptadores de tethering USB entram no filtro. "Metrica 1 com
    # AutomaticMetric desligada" tambem descreve o Fortinet SSL VPN Virtual
    # Ethernet Adapter desta maquina, que o cliente de VPN configura assim de
    # proposito: rodar isto devolvia a metrica automatica dele e derrubava a
    # rota da VPN sem nada no log ligando uma coisa a outra.
    if ($Rotacionar) {
        $tethers = @(Get-NetAdapter | Where-Object {
            $_.PnPDeviceID -like 'USB\*' -and
            $_.InterfaceDescription -match 'RNDIS|Remote NDIS|Tethering|NCM|Internet Sharing'
        } | Select-Object -ExpandProperty ifIndex)
        Get-NetIPInterface -AddressFamily IPv4 |
            Where-Object { $tethers -contains $_.ifIndex -and
                           $_.InterfaceMetric -eq 1 -and $_.AutomaticMetric -eq 'Disabled' } |
            ForEach-Object {
                Write-Host "Restaurando metrica automatica em $($_.InterfaceAlias)"
                Set-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4 `
                    -AutomaticMetric Enabled -ErrorAction SilentlyContinue
            }
    }
}
