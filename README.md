# robozin — coleta de faturas da Equatorial Goiás

Baixa a fatura mais recente de cada UC na Agência Virtual da Equatorial Goiás,
uma vez por ciclo mensal, sem ser bloqueado.

## A premissa que mudou

O projeto começou apostando que as recusas `#001/#002` eram **independentes**
entre tentativas — logo, metralhar pagava: ~50 tentativas em 51 minutos, pausa
fixa de 20-45 s, IP trocado a cada recusa.

Um teste manual derrubou isso: **um login deu certo e o seguinte, no mesmo
navegador e mesmo IP, já foi recusado; a mesma UC entrou de primeira no
celular.** Se o sucesso queima o par (dispositivo, IP), as tentativas não são
independentes e repetir rápido não aumenta a chance — reduz, porque alimenta a
contagem de velocity que gerou o bloqueio.

Daí a inversão. O recurso escasso deixou de ser tempo e passou a ser
**reputação**, e tempo nós temos de sobra: cada UC precisa de UMA fatura por
mês, ou seja ~30 dias de janela para 1 sucesso.

|                    | antes                     | agora                              |
| ------------------ | ------------------------- | ---------------------------------- |
| Perfil Chrome      | 1 para todas as contas    | 1 por titular, permanente          |
| Troca de conta     | `clear_cookies()`         | troca o processo do Chrome inteiro |
| Tentativas         | ~50 em 51 min             | 1 por UC por dia                   |
| Backoff            | 20-45 s, fixo             | horas/dias, por motivo da falha    |
| Rotação de IP      | a cada recusa             | entre contas, nunca no meio        |
| Ritmo de digitação | sorteado a cada execução  | fixo por persona, para sempre      |
| Logout             | nenhum                    | link "Sair" de verdade             |

Com N UCs, `N/30` logins por dia fecham o ciclo: **2/dia para 60 UCs, 10/dia
para 300**. Nada disso é apertado.

## Por que perfil, e não cookie

`clear_cookies()` apaga cookie mas **não** apaga localStorage nem IndexedDB — e
é ali que o Transmit Security guarda o device ID. Limpar só o cookie produz um
visitante aparentemente novo (sem `visid_incap_`) carregando o device ID de
outro: exatamente a incoerência que o DRS pontua.

Trocar o `--user-data-dir` troca cookie, localStorage, IndexedDB, service
workers e cache **de uma vez e de forma coerente**. Por isso a persona é um
diretório de perfil, não um estado limpo por software.

Pelo mesmo motivo o Chrome é encerrado com jeito (fecha as abas pelo DevTools,
depois `taskkill` sem `/F`): o LevelDB do localStorage só é gravado no shutdown
limpo, e um `/F` perderia o device ID que estamos tentando manter estável.

## Camadas de defesa do site

1. **Imperva Incapsula** — WAF + fingerprint JS (`visid_incap_`, `nlbi_`, `reese84`)
2. **reCAPTCHA Enterprise v3** — score invisível, `action="login"`, token vale ~2 min
3. **Transmit Security (DRS)** — fingerprint de dispositivo → hidden `#tokenTransmit`

Nenhum desses tokens é forjável por HTTP puro. Por isso dirigimos um Chrome real
via CDP em vez de deixar o Playwright lançá-lo (o que acrescentaria
`--enable-automation` e afins).

## De onde vêm as UCs

Com `MF_API_KEY` definida, a lista vem de `GET /api/v1/queues` do Minha Fatura
(UC, CPF/CNPJ e data de nascimento — os três campos do login). Sem chave, vale o
`contas.json` de sempre.

A API **reserva cada tarefa por 30 minutos**. Esse prazo é ignorado de propósito:
aqui uma UC leva dias entre tentativas, e correr para caber na reserva seria
voltar a metralhar — exatamente o que causou o bloqueio. A tarefa não concluída
volta para a fila deles e reaparece na próxima busca; do nosso lado ela é
acumulada em `estado/fila_api.json`, deduplicada por UC, e quem manda no ritmo
continua sendo a agenda local. O cache também é o plano B: API fora do ar ou
chave revogada não param o ciclo em andamento.

```powershell
.venv\Scripts\python.exe api.py           # busca e mostra o que a API serve
.venv\Scripts\python.exe api.py --cache   # só a fila já acumulada em disco
```

Ainda **não relatamos resultado nem enviamos o PDF** de volta — a documentação
recebida cobre só o `/queues`. O `task_id`, o `uc_id` e o `id` de cada tarefa
ficam guardados no cache para o dia em que esses endpoints entrarem.

## Worker do gcenergy (`worker.py`)

Modo em que a fila vem do gcenergy por HTTP, seguindo o guia de integração do
worker: reserva um job (`POST /worker/jobs/reservar`), busca as faturas da UC,
sobe cada PDF no Google Drive e devolve os IDs no `PATCH .../status`. O gcenergy
importa em segundo plano; o worker não acompanha.

```powershell
.venv\Scripts\python.exe worker.py --checar   # chave da API + credencial do Drive, sem reservar nada
.\robo.ps1 -Modo worker                       # laço contínuo
.\robo.ps1 -Modo worker -Rotacionar           # idem, trocando o IP pelo celular
.venv\Scripts\python.exe worker.py --uma      # no máximo uma reserva e sai
.venv\Scripts\python.exe worker.py --job 42   # mostra um job, sem mexer nele
```

Precisa de `GCENERGY_WORKER_KEY` e `GOOGLE_DRIVE_CREDENCIAIS` no
`credenciais.env`. A pasta do Drive (`GOOGLE_DRIVE_PASTA_ID`) precisa dar
**leitura para a conta de serviço do gcenergy** — sem isso o job fecha
`CONCLUIDO` e a importação falha do lado deles (aparece em `importacaoStatus`).

### Um job por vez, e o ritmo continua sendo da agenda

O job tem 60 min de prazo e chega numa ordem que o robô não escolhe. A
estratégia de reputação não muda por isso; ela é aplicada em dois pontos:

- **Antes de reservar**: janela de horário, intervalo e cooldown da agenda.
  Fora deles o worker não reserva — o job espera `PENDENTE` no servidor, sem
  relógio correndo. Não há teto diário de sessões no worker
  (`EQUATORIAL_TETO_DIA` só vale no orquestrador).
- **Depois de reservar, por UC**: em backoff de bloqueio, já com sessão hoje,
  ou com o dispositivo do titular parado depois de um bloqueio → `ERRO`
  `TENTATIVA_ADIADA`. O `motivoErro` diz até quando. Devolver `PENDENTE`
  travaria a fila: ela é servida em ordem e o mesmo job voltaria na reserva
  seguinte. Com `ERRO`, o cron diário recria o job amanhã — uma tentativa por
  UC por dia, que é o ritmo do projeto.

Mês que já está em `faturas/` (de qualquer fluxo) vai sem abrir o portal; job
inteiro em disco conclui sem login. Num job `MESES`, a 2ª via é emitida uma vez
e cada mês pendente sai do mesmo grid; o resto vem do histórico, uma consulta
por ano. PDF já enviado não sobe de novo (`estado/drive_ids.json`).

| Desfecho                                   | PATCH                                    |
| ------------------------------------------ | ---------------------------------------- |
| algum PDF (do disco ou do portal)          | `CONCLUIDO` com os IDs; parcial se faltou mês |
| login barrado (Imperva, reCAPTCHA, #00x)   | `ERRO` `CAPTCHA`                          |
| UC/documento recusados                     | `ERRO` `LOGIN_INVALIDO` / `UC_NAO_ENCONTRADA` |
| portal pediu nascimento e o job não tem    | `ERRO` `DATA_NASCIMENTO_AUSENTE`          |
| portal fora do ar, PDF que não veio        | `ERRO` `DISTRIBUIDORA_INDISPONIVEL`       |
| logou e nenhum mês pedido tem fatura       | `ERRO` `FATURA_INDISPONIVEL`              |
| falha do robô ou do upload no Drive        | `ERRO` `ERRO_INTERNO`                     |
| `Ctrl+C` no meio do job                    | `PENDENTE` (volta para a fila)            |

`409` no PATCH segue o guia: consulta o job e repete só se ele ainda é deste
worker. Se o processo morrer com um job na mão, a próxima largada o devolve à
fila (`estado/worker_job-<X-Worker-Id>.json`) em vez de esperar o timeout.

### Pool de personas

O worker **não** usa um perfil de Chrome por titular — com 1500+ titulares
seriam 1500 perfis para maturar e um dia perdido a cada titular novo. Usa um
**pool fixo** de `GCENERGY_PERSONAS` perfis (padrão 30, em `perfis/pool-NN`),
e cada titular cai **sempre na mesma persona** (hash do CPF/CNPJ). Cada
dispositivo atende um grupo estável de ~1500/30 = 50 contas, e cada conta vê um
dispositivo só — o padrão de um escritório que cuida das contas de um grupo de
clientes, e não o de um dispositivo rodando por centenas de contas.

- **Primeira largada**: enquanto houver persona sem maturar, o worker matura
  uma por vez (30–90 s entre elas) e não reserva nada; depois espera todas
  terem a idade mínima (3–6 h). Com 3 celulares, ~20 min de maturação.
- **Descanso**: a mesma persona só abre outra sessão depois de
  `GCENERGY_PERSONA_DESCANSO_MIN` (padrão 15). Se o job cair numa persona
  descansando, ele espera, dentro dos 60 min do servidor.
- **Bloqueio marca o dispositivo**: `imperva`, `captcha` ou `#00x` param a
  persona pela primeira faixa do backoff (3 h a 3 dias). Os jobs do grupo dela
  terminam em `TENTATIVA_ADIADA` até lá. O IP já troca a cada sessão.
- **Trava por persona** (`estado/travas/`): dois jobs na mesma persona — mesmo
  titular ou mesmo grupo — rodam um depois do outro. O segundo espera, e se a
  espera não couber no prazo, devolve `PENDENTE`. A trava é do processo:
  worker morto não deixa persona travada.
- Mudar `GCENERGY_PERSONAS` remapeia a maior parte dos titulares para outro
  perfil. Escolha o tamanho uma vez.
- Os perfis por titular já existentes em `perfis/` não são tocados: continuam
  com o orquestrador antigo.


### Um worker por celular (`worker_paralelo.py`)

A conta que manda é sessão por dia: cada UC precisa de pelo menos uma por mês.
Com margem de 40% para recusas, 1000 UCs pedem ~47 sessões/dia e 5000, ~230. Um
worker só faz uma sessão por vez, saindo sempre pelo mesmo IP; a escala vem
de IPs, não de pressa.

```powershell
.venv\Scripts\python.exe worker_paralelo.py --dispositivos   # confere os celulares
.\robo.ps1 -Modo worker -Paralelo                            # um worker por celular
```

Cada celular vira um worker inteiro: proxy amarrado ao adaptador dele (mesma
infraestrutura do `coletar_paralelo.py`), rotação de IP só dele,
`X-Worker-Id` próprio (`<GCENERGY_WORKER_ID>-01`, `-02`...) e pasta de
download própria. O pool de personas, `faturas/`, agenda e índice do Drive são
compartilhados, com trava entre processos; na primeira largada os workers
maturam o pool juntos, cada um pelo seu celular.

- **Sem teto diário**: cada worker emenda um job no outro dentro da janela. O
  que ainda segura o ritmo é o limite de uma sessão por UC por dia, o backoff
  por motivo e a rotação de IP entre sessões. O limite seguro por IP não foi
  medido (os únicos dados são 17 e 26 sessões/dia, em setembro): acompanhe a
  taxa de `CAPTCHA` nos jobs.
- **Parada**: `Ctrl+C` devolve os jobs em mãos; `estado/parar-worker.flag`
  para todos entre um job e outro.

## Uso

```powershell
copy credenciais.env.exemplo credenciais.env   # e coloque a MF_API_KEY
# sem API: copy contas.json.exemplo contas.json e preencha as UCs
.\robo.ps1 -Modo maturar               # prepara os perfis novos (não loga)
.\robo.ps1                             # laço contínuo: o modo normal
.\robo.ps1 -Rotacionar                 # idem, trocando o IP pelo celular
.\robo.ps1 -Modo status                # relatório da agenda
.\robo.ps1 -Modo agora -Uc 000176588701258   # força uma UC, ignorando a agenda
```

Antes de gastar uma tentativa:

```powershell
.venv\Scripts\python.exe diagnostico.py   # confere o que o site vai ler do navegador
```

Ele reprova renderer de software, `navigator.webdriver`, fuso fora do Brasil e
janela incoerente com a tela — os erros que queimariam o IP à toa.

## Maturação de perfil

Um perfil recém-criado é o pior cenário para o reCAPTCHA v3: sem cookie do
Google, sem histórico, score no chão. Antes do primeiro login o robô dá um
passeio por alguns sites neutros e **espera 3-6 h** (sorteado por persona) antes
de estrear na Equatorial. Idade de dispositivo é sinal barato de coletar, e
todos os três fornecedores coletam.

A espera era de 10-30 h, por inferência. O que existe de medição aponta mais
baixo: o lote de 04/09/2026 rodou forçado, com perfis de **3-4 h**, e fechou
5/5. 3-6 h fica acima da única idade que já deu certo aqui e devolve um dia
inteiro da janela de 30 — mas continua sendo estimativa, não número calibrado
contra o site. `EQUATORIAL_MATURACAO_H_MIN/MAX` mexe nisso; o valor é sorteado
no nascimento do perfil e gravado no `.persona.json`, então mudar a faixa só
afeta perfis novos.

`-Modo agora` pula essa espera. Use com parcimônia.

O passeio em si custa 30-60 s por perfil; entre um perfil e o seguinte a pausa é
de **30-90 s** (`EQUATORIAL_MATURACAO_MIN/MAX`). Curta porque a maturação não
toca a Equatorial — não há reputação da agência virtual a preservar ali. Não é
zero por causa do Google: dez perfis novos batendo na busca em sequência
imediata, do mesmo IP, é velocity óbvia, e o score do reCAPTCHA é justamente o
que a maturação existe para levantar.

No laço contínuo a maturação continua sendo tarefa de tempo ocioso: **um perfil
por acordada**, com 10-30 min entre elas. Para preparar todos de uma vez use
`-Modo maturar`.

## Ordem de tentativa da coleta

1. **Segunda via da área aberta** (`segunda_via.py`) —
   `AgenciaGO/Serviços/aberto/SegundaVia.aspx` pede só UC + CPF/CNPJ. Sem
   Transmit, sem data de nascimento, sem o par login/sair. É onde a fatura do
   mês corrente está enquanto não for paga, que é o caso normal da coleta.
2. **Histórico de faturas** (`baixar_fatura.py`, com login) — assume quando a
   área aberta não resolve: fatura já paga, mês antigo pedido com `--mes`, ou a
   página exigindo sessão.

Se a área aberta falhou só por exigir login, ela é tentada de novo depois da
autenticação, antes de cair no histórico — continua sendo menos cliques.

Os ids do WebForms mudam de página para página, então `segunda_via.py` descobre
os campos e o botão em tempo de execução (rótulo, placeholder, `name`) e grava
`faturas/estrutura-segundavia-*.json` a cada estado. Para conferir os seletores
em uma corrida real, sem baixar nada:

```powershell
python segunda_via.py --sondar
```

## Coleta paralela — um celular por worker

Plugue 2–4 Androids com **ancoragem USB** e **depuração USB** ligadas e rode uma
extração por aparelho, ao mesmo tempo, cada uma saindo pelo IP da operadora do
seu próprio celular:

```powershell
.venv\Scripts\python.exe dispositivos.py                       # confere o mapa antes
.venv\Scripts\python.exe coletar_paralelo.py --mes 07/2026 --limite 1   # fumaça: 1 titular por celular
.venv\Scripts\python.exe coletar_paralelo.py --mes 07/2026     # o lote inteiro
```

### O que destravou isso

Uma máquina tem **uma** rota padrão. O modo de um celular só (`rede.assumir_rota`,
usado pelo `robo.ps1 -Rotacionar`) empurra o adaptador do tethering para métrica 1
e faz *a máquina inteira* sair pelo 4G — global por construção, e o motivo de o
`maturar_paralelo.py` desligar a rotação nos filhos.

Aqui a saída é escolhida por **socket**, não por rota. O Windows, no modelo de
host forte (padrão desde o Vista), faz busca de rota restrita à interface dona do
endereço quando o socket está amarrado. Medido nesta máquina, com a rota padrão
no Wi-Fi:

```
origem 10.1.22.28  (Wi-Fi)     -> 45.191.204.245
origem 172.25.16.1 (vEthernet) -> OSError [WinError 10051] rede inacessível
```

O `10051` é a prova: o Windows recusou mandar em vez de cair na rota padrão. O
Chrome não sabe amarrar socket, mas fala SOCKS5 — então cada worker sobe um
`proxy_local.ProxyLocal` amarrado ao adaptador do celular dele e passa
`--proxy-server=` ao Chrome. A rota padrão fica no Wi-Fi e **não é preciso
Administrador**.

### Como cada celular é reconhecido

Com N aparelhos, nada no Windows diz qual adaptador é de quem — e errar significa
um worker desligando o dado móvel do celular que o outro está usando no meio de
um login. `dispositivos.py` resolve por uma escada de critérios, do mais barato
ao mais caro: **série USB** (subindo pelos `DEVPKEY_Device_Parent` até
`USB\VID&PID\<série>`, que no Android é o serial do `adb`), **gateway** (o
`192.168.x.129` que o celular usa do lado do cabo e que o Windows recebe como
`NextHop`), e por último a **sonda empírica** (derruba o dado de um aparelho e vê
qual adaptador emudece). Nada disso é cacheado: `ifIndex`, endereço e até o MAC
do RNDIS mudam quando o cabo sai e volta.

### Desligue o Wi-Fi **dos celulares**

A pegadinha que mais custa tempo: **o Android prefere Wi-Fi quando as duas
conexões estão ligadas**. Um celular com dados móveis ativos *e* Wi-Fi conectado
ancora o **Wi-Fi** — e se for a rede da casa, ele sai pelo mesmo IP público do
notebook. O aparelho parece perfeito (adaptador de pé, endereço, IP público
respondendo) e não compra IP nenhum. Medido aqui:

```
SM_G781B     wlan0 10.1.22.15 + rmnet_data0  ->  45.191.204.245   (o IP da casa)
moto_g54_5G  só v4-ccmni1                    ->  177.174.217.194  (a operadora)
```

`dispositivos.py` detecta isso pelo `adb shell ip -4 -o addr` e marca o aparelho
como `ANCORANDO O WI-FI`; `coletar_paralelo.py` o deixa de fora da rodada. A
checagem é no celular, e não comparando IPs públicos, porque medir o IP do
notebook nem sempre funciona (com dois celulares plugados, amarrar na Wi-Fi dele
deu `WSAEHOSTUNREACH` nesta máquina).

### O que o modo paralelo garante

- `--mes` é obrigatório: sem ele quem decide o que falta é a agenda, que é estado
  compartilhado; com ele quem decide é o PDF em `faturas/`, que é por UC.
- A fila é fatiada **por titular**, nunca por UC: as UCs de um documento dividem
  um perfil de Chrome, e dois processos no mesmo perfil se matam.
- Cada worker baixa em `faturas/.baixando-N/`. A captura do download é por
  varredura de diretório, então pasta compartilhada faria um worker arquivar o
  PDF do outro sob a UC errada.
- Rotação de IP só **entre titulares**, como no modo serial; depois de cada
  rotação o proxy é reamarrado, porque o modo avião pode reerguer o tethering em
  outra faixa.
- `Ctrl+C` levanta `estado/parar.flag` e espera os workers fecharem o Chrome com
  jeito (até 120 s cada) — matar no braço corromperia o `localStorage` da persona.
- O pai devolve todos os aparelhos ao fim: modo avião desligado, dado ligado.

### O vazamento de WebRTC, e por que é a preferência e não a flag

O SOCKS5 não carrega UDP, então o STUN do WebRTC sai **direto pela rota
padrão** e devolve um candidato `srflx` com o IP do Wi-Fi — o mesmo nos quatro
workers, que é exatamente a correlação que a rotação de IP existe para quebrar.
Medido nesta máquina, com o proxy amarrado ao Wi-Fi:

| configuração | `srflx` |
| ------------ | ------- |
| sem nada | `45.191.204.245` |
| `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` | `45.191.204.245` |
| pref `webrtc.ip_handling_policy` no `Default/Preferences` | **nenhum** |

A flag de linha de comando virou no-op no Chrome atual: só acrescentaria uma
linha de comando atípica sem comprar nada. `navegador._fechar_vazamento_webrtc`
grava a preferência antes de subir o Chrome (funciona inclusive em perfil novo,
que ainda não tem `Preferences`), e ela é a mesma que qualquer usuário de VPN
corporativa tem ligada.

## Arquivos

| Arquivo             | Papel                                                        |
| ------------------- | ------------------------------------------------------------ |
| `orquestrador.py`   | laço principal: escolhe a conta, sobe o perfil, coleta, dorme |
| `agenda.py`         | orçamento e backoff; estado em `estado/agenda.json`           |
| `personas.py`       | identidade por titular: perfil + traços determinísticos       |
| `navegador.py`      | sobe/derruba o Chrome de uma persona; poda o perfil           |
| `humano.py`         | pausas log-normais, curvas de mouse, cliques com hesitação    |
| `login.py`          | o fluxo de login e a classificação da falha                   |
| `segunda_via.py`    | **caminho primário**: 2ª via pela área aberta, sem login       |
| `baixar_fatura.py`  | consulta o histórico (com login) e baixa o PDF                |
| `rede.py`           | rotação de IP via tethering USB (modo avião pelo `adb`)       |
| `dispositivos.py`   | pareia cada celular com o adaptador de rede dele              |
| `proxy_local.py`    | SOCKS5 local amarrado a uma interface: é o que dá 1 IP por celular |
| `coletar_paralelo.py` | uma extração por celular, em paralelo, para um mês escolhido |
| `historico_consumo.py` | histórico de consumo de uma lista em CSV, gravando em CSV |
| `diagnostico.py`    | auditoria de fingerprint                                      |
| `worker.py`         | worker do gcenergy: reserva job, busca, sobe no Drive, reporta |
| `gcenergy.py`       | cliente HTTP das rotas `/worker/**` (reserva, GET, PATCH + 409) |
| `worker_paralelo.py` | um worker do gcenergy por celular plugado                     |
| `api.py`            | fila de UCs vinda da API do Minha Fatura                       |
| `enviar_drive.py`   | sobe as faturas do mês para o Drive e gera o CSV `uc;mes;ano;link` |
| `maturar_fila.py`   | matura só quem já está na fila local, sem buscar mais tarefas  |
| `coletar_varias.py` | **substituído** — mantido só como referência                  |

## Ajustes (variáveis de ambiente, ou `credenciais.env`)

| Variável                    | Padrão | O que faz                          |
| --------------------------- | ------ | ---------------------------------- |
| `MF_API_KEY`                | (vazio)| chave `X-API-Key`; define a fila pela API |
| `MF_API_URL`                | `https://minha-fatura.vercel.app/api/v1` | base da API |
| `MF_API_LIMIT`              | 50     | tarefas por busca (1–50)           |
| `MF_API_GRUPO`              | (vazio)| filtra por grupo (ex.: `Sicoob`)   |
| `MF_API_GRUPO_BUSCA`        | (vazio)| filtra por grupo de leitura (1–3)  |
| `MF_FONTE`                  | auto   | `json` força o `contas.json`       |
| `EQUATORIAL_HORA_INICIO`    | 8      | início da janela de acesso         |
| `EQUATORIAL_HORA_FIM`       | 22     | fim da janela                      |
| `EQUATORIAL_TETO_UC_DIA`    | 1      | tentativas por UC por dia          |
| `EQUATORIAL_TETO_DIA`       | 10     | teto global de logins por dia (só o orquestrador; o worker não tem) |
| `EQUATORIAL_MATURACAO_H_MIN`| 3      | espera mínima antes do 1º login (h) |
| `EQUATORIAL_MATURACAO_H_MAX`| 6      | espera máxima antes do 1º login (h) |
| `EQUATORIAL_MATURACAO_MIN`  | 30     | pausa mínima entre perfis na maturação (s) |
| `EQUATORIAL_MATURACAO_MAX`  | 90     | pausa máxima entre perfis na maturação (s) |
| `EQUATORIAL_INTERVALO_MIN`  | 0      | intervalo mínimo entre contas (s)  |
| `EQUATORIAL_INTERVALO_MAX`  | 0      | intervalo máximo entre contas (s)  |
| `EQUATORIAL_COOLDOWN_MIN`   | 0      | descanso mínimo após um sucesso (s) |
| `EQUATORIAL_COOLDOWN_MAX`   | 0      | descanso máximo após um sucesso (s) |
| `EQUATORIAL_TESTE_MIN`      | 0      | intervalo mínimo no lote `--agora todas` (s) |
| `EQUATORIAL_TESTE_MAX`      | 0      | intervalo máximo no lote `--agora todas` (s) |
| `EQUATORIAL_ROTACIONAR`     | 0      | `1` liga a rotação de IP (trocada a cada UC) |
| `EQUATORIAL_MES`            | (vazio)| mês da fatura, `MM/AAAA`; vazio = a mais recente |
| `EQUATORIAL_ANOS_ATRAS`     | 3      | anos varridos quando não há mês fixo |
| `EQUATORIAL_DISPOSITIVO`    | (vazio)| serial `adb` do celular deste worker (posto pelo `coletar_paralelo.py`) |
| `EQUATORIAL_IFINDEX`        | (vazio)| adaptador de rede daquele celular   |
| `EQUATORIAL_PROXY`          | (vazio)| SOCKS por onde o Chrome sai; vazio = rota padrão |
| `EQUATORIAL_PORTA_BASE`     | 9222   | base da varredura de porta do CDP (uma por worker) |
| `EQUATORIAL_LARGADA_MAX`    | 90     | atraso aleatório máximo na largada de cada worker (s) |
| `EQUATORIAL_WEBRTC`         | 0      | `1` desliga a preferência que impede o WebRTC de vazar o IP do Wi-Fi |
| `GCENERGY_WORKER_KEY`       | (vazio)| `X-Worker-Key` do worker do gcenergy |
| `GCENERGY_URL`              | `https://api.synexsaas.com.br` | base da API do worker |
| `GCENERGY_WORKER_ID`        | `robozin-<host>` | `X-Worker-Id`: fixo e único por instância |
| `GCENERGY_PRAZO_MIN`        | 45     | busca por job antes de concluir com o que veio (o servidor corta em 60) |
| `GCENERGY_ESPERA_VAZIA`     | 60     | segundos entre consultas com a fila vazia |
| `GCENERGY_CACHE_LOCAL`      | 1      | `0` não reaproveita PDFs já em `faturas/` |
| `GCENERGY_PERSONAS`         | 30     | tamanho do pool de personas do worker (mudar remapeia titulares) |
| `GCENERGY_PERSONA_DESCANSO_MIN` | 15 | minutos entre duas sessões da mesma persona |
| `GOOGLE_DRIVE_CREDENCIAIS`  | (vazio)| caminho do JSON de autorização do Google |
| `GOOGLE_DRIVE_PASTA_ID`     | `1vligcM_szlS_R9SUqq03StdG-HX6FAOX` | pasta de destino no Drive |

### Subir para o Drive

```powershell
.venv\Scripts\pip install google-api-python-client google-auth google-auth-oauthlib
.venv\Scripts\python enviar_drive.py --mes 08/2026 --seco   # confere a lista
.venv\Scripts\python enviar_drive.py --mes 08/2026          # envia e escreve o CSV
```

Sai em `saida/faturas-drive-08-2026.csv`. O script lista a pasta antes de
enviar e reaproveita o arquivo de mesmo nome, então rodar de novo depois de
baixar mais faturas envia só as novas.

O JSON pode ser de **conta de serviço** (sem navegador, mas a pasta precisa
estar num Shared Drive ou compartilhada como editor com o e-mail da conta —
conta de serviço não tem cota própria e o upload em My Drive comum falha
com `storageQuotaExceeded`) ou de **OAuth client**, que abre o navegador uma
vez e guarda o token em `estado/drive_token.json`.

## Histórico de consumo (`historico_consumo.py`)

Fluxo separado do da fatura: entra uma lista de UCs em CSV, sai o histórico de
consumo delas em CSV.

```powershell
python historico_consumo.py ucs.csv                 # saida/historico-consumo.csv
python historico_consumo.py ucs.csv --saida saida/consumo.csv
python historico_consumo.py ucs.csv --refazer       # ignora o que já foi coletado
python historico_consumo.py ucs.csv --limite 3      # só 3 titulares
python historico_consumo.py ucs.csv --sondar        # dumpa a tela logada e sai
```

**Entrada** (`ucs.exemplo.csv`): cabeçalho com `uc` e `cpf_cnpj` — também
aceita `documento`/`doc`/`cpf`/`cnpj` —, separador `,` ou `;`, colunas
opcionais `nasc` e `apelido`. Sem cabeçalho reconhecível, valem as duas
primeiras colunas. Linha inválida é avisada no log e pulada; UC repetida fica
com a primeira ocorrência.

**Saída**: duas tabelas lado a lado.

- `historico-consumo.csv` — uma linha por competência:
  `uc;doc;apelido;competencia;mes;ano;consumo_kwh;dias;media;valor;uc_confirmada;coletado_em;bruto`
- `historico-consumo-status.csv` — uma linha por UC fechada:
  `uc;doc;apelido;status;motivo;linhas;quando`

`bruto` é a linha inteira da tabela do portal em JSON, com o cabeçalho real.
Nada aqui presume as colunas do site: mapeiam-se as conhecidas (competência,
consumo, dias, média, valor) e o resto sobrevive em `bruto` — coluna nova
aparece lá, sem perder coleta e sem exigir outra passada no site.

`uc_confirmada=nao` marca a linha cuja UC o portal não repetiu na tela. A
coleta segue, mas quem lê o CSV sabe que o vínculo linha↔UC não foi conferido —
gravar consumo sob a UC errada é o pior desfecho possível deste fluxo.

### Retomada

Cada UC fechada é escrita e sincronizada no disco (`flush` + `fsync`) na hora.
Worker morto no meio — Chrome caído, tether perdido, Ctrl+C — não custa o que
já veio: rodar o mesmo comando de novo lê o arquivo de status e pula toda UC
com `status=ok`. `--refazer` ignora essa memória e recolhe tudo.

### Uma sessão por titular, não por UC

A tela lista **todas** as UCs do titular no mesmo combo (o
`#CONTENT_comboBoxUC` da 2ª via). Com duas UCs no mesmo CPF, a segunda sai pelo
combo, sem relogar — login é o recurso caro aqui, não o clique. UC que não
aparece no combo volta para a fila e ganha uma sessão própria, em que ela é a
UC do login.

O resto do ciclo é o do orquestrador: uma persona por titular, Chrome próprio,
maturação de perfil novo, rotação de IP entre titulares com
`EQUATORIAL_ROTACIONAR=1` e logout pelo site no fim.

## Backoff por motivo da falha

O motivo determina o castigo — sem isso, o challenge do Imperva (onde insistir é
a pior ação) recebia a mesma pausa que um timeout de rede (onde não há nada a
punir).

| Motivo    | 1ª falha  | 2ª        | 3ª+       |
| --------- | --------- | --------- | --------- |
| `rede`    | 20-60 min | 30 min-2h | 1-4 h     |
| `backend` | 3-7 h     | 10-22 h   | 26-50 h   |
| `captcha` | 12-30 h   | 24-48 h   | 40-72 h   |
| `imperva` | 30-72 h   | 48-96 h   | 72-144 h  |

Depois de um **sucesso**, o IP inteiro descansa 45-120 min — foi exatamente ali
que o bloqueio apareceu no teste manual.

## Limite conhecido

A rotação de IP só tem o tethering do celular. Sob CGNAT o IP novo costuma sair
do mesmo pool, embora o modo avião no aparelho testado (Galaxy S20 FE / Claro)
tenha trocado entre faixas bem diferentes (187.43.x → 191.58.x → 191.56.x). O
espaçamento no tempo é que carrega o peso da estratégia; a rotação é bônus.
