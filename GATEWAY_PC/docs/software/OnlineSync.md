# Telemetria online e sincronização multidispositivo (Sprint 18)

## 1. Diagnóstico do sistema existente

| Item | Situação encontrada |
|---|---|
| Linguagens | Python 3 (biblioteca padrão), HTML, CSS e JavaScript sem framework |
| Build | Nenhum. O gateway serve os arquivos diretamente |
| Execução | `INICIAR_ECOTAM.bat` ou `python ecotam_gateway.py`; servidor em `127.0.0.1:8765` |
| Dependência opcional | `pyserial` para ler a porta COM |
| Banco oficial | **InfluxDB OSS 2.x local** (`http://127.0.0.1:8086`), measurement `ecotam_telemetry`, tags `device`, `source`, `session_id`, timestamp em ms (UTC) |
| Cópia local | CSV diário em `dados/` (`ecotam_AAAAMMDD_v3.csv` ou `_s17_v2.csv`) |
| Antena | ESP32 RX ligado por USB serial (COM3, 115200 baud). Recebe do TX por ESP-NOW e imprime um log em texto; `RxLogParser` monta amostras V2 (64 B) e V3 (84 B) |
| Sessão | `SESSION_ID` gerado a cada execução do gateway; não havia início, pausa ou fim de trajetória |
| Interface | Tempo real (plano de voo XY local, BME280, GPS, estado do sistema), Histórico e Qualidade, ambos consultando o InfluxDB |
| Publicação | Nenhuma configuração de GitHub Pages ou Actions |
| Serviços externos | Nenhum |

Limitações que orientaram a arquitetura:

1. O InfluxDB só escuta no computador da antena. Um celular na internet não consegue consultá-lo, e o GitHub Pages não executa backend.
2. O protocolo V3 não transmite velocidade. Distância e rumo podem ser calculados das posições; velocidade não é inventada.
3. O plano de voo é local (sem mapa de fundo), por decisão de privacidade já existente; foi mantido.

## 2. Arquitetura

```
ESP32 TX ──ESP-NOW──> ESP32 RX ──USB/COM3──> gateway (PC da antena)
                                              ├─ CSV em dados/              (registro local)
                                              ├─ InfluxDB 127.0.0.1:8086    (registro oficial)
                                              └─ fila em memória ──REST──> Firebase Realtime Database
                                                                              │ REST streaming (SSE)
GitHub Pages (site estático) ── celulares, tablets, notebooks ──────────────────┘ só leitura
```

* **InfluxDB continua sendo a fonte oficial.** Nada foi migrado, apagado ou alterado no esquema.
* **Firebase é só distribuição.** Guarda o estado atual (`live`) e um buffer compacto da trajetória de cada sessão, necessário porque o banco oficial não é acessível pela internet.
* **A antena não depende do Firebase.** `save_sample` grava CSV, enfileira para o InfluxDB e só *enfileira em memória* para o Firebase. O envio acontece em outra thread.

### Estrutura no Realtime Database

```
access/transmitters/{uid}: true          criado manualmente no console; ninguém escreve pelo app
live                                     estado compartilhado (um único objeto pequeno)
  sessionId, status, statusAt, startedAt, source, points, lastKey, heartbeatAt
  antenna: {state: ok|stale|none|disconnected|simulated, ageS}
sessions/{sessionId}
  info:   status, startedAt, pausedAt, resumedAt, finishedAt, source, gatewaySession
  track/{tttttttttttttt_cccccc}: n, t, seq, lat, lon, ga, ba, tc, pa, hu, sat, fix, pv
  events/{chave}: type (start|pause|resume|finish), at
```

* `sessionId`: `AAAAMMDDTHHMMSSZ_xxxxxx` (UTC + aleatório).
* Chave do ponto: 13 dígitos do timestamp UTC em ms + contador. Ordena cronologicamente e é determinística, então reenviar o mesmo lote não duplica nada.
* `n`: número do ponto na sessão (1, 2, 3…). O espectador detecta lacunas comparando com `live.points`.
* Campos do ponto (`t` em ms UTC, `lat`/`lon` em graus, `ga` altitude GPS, `ba` altitude barométrica, `tc` °C, `pa` Pa, `hu` %, `sat`, `fix`, `seq` do rádio, `pv` versão do protocolo) só existem quando vieram na amostra. Sem fix, não há coordenadas.
* Precisão preservada: os valores publicados são os mesmos números gravados no CSV e no InfluxDB (verificado no teste ponta a ponta).

### Fluxo do transmissor

1. Operador conecta a conta do transmissor no painel **Firebase · transmissor** (e-mail e senha, só em memória).
2. **Iniciar trajetória** verifica: antena com frame há menos de 5 s (ou fonte simulada com a opção de teste marcada) e amostra válida recente. Cria o `sessionId`, registra em `dados/trajetorias.jsonl`, publica `live` como *preparando*.
3. O primeiro ponto muda o estado para *em andamento*. Cada amostra vira um ponto na fila.
4. **Pausar**: a gravação oficial continua; o site deixa de receber pontos. **Retomar**: *retomando* até o próximo ponto. **Encerrar**: *finalizada* (pede confirmação).
5. A cada lote (no máximo 1 por segundo em operação normal) vão juntos o batimento (`heartbeatAt`, carimbo do servidor) e o estado da antena. Sem pontos novos, o batimento vai a cada 5 s.

Se o gateway for reiniciado no meio de uma trajetória, `dados/trajetoria_ativa.json` restaura a mesma sessão. Pontos que estavam só na fila em memória nesse momento não chegam ao site (continuam no InfluxDB e no CSV).

### Fluxo do espectador

1. Abre o stream de `live`. Sem sessão: **Aguardando início**.
2. Com sessão: abre o stream de `sessions/{id}/track`, que entrega o percurso anterior e depois cada ponto novo, sem intervalo entre histórico e tempo real (o mesmo stream faz as duas coisas).
3. Quando `live.sessionId` muda (o operador iniciou outra), troca de sessão sozinho. Nenhum clique é necessário.
4. Perdeu a conexão: fecha o stream e reabre com `orderBy="$key"&startAt="<última chave>"`, recebendo só o que faltou. Pontos repetidos são descartados pela chave; pontos fora de ordem são reposicionados.
5. Se `live.points` indicar pontos que não chegaram por 8 s, recarrega o percurso inteiro.

### Estados mostrados

| Estado | Quando |
|---|---|
| Aguardando início | Nenhuma sessão em `live` |
| Preparando transmissão | Operador iniciou; nenhum ponto ainda |
| Em andamento | Pontos chegando, transmissor e antena vivos |
| Pausada | Operador pausou |
| Retomando | Operador retomou; aguardando o próximo ponto |
| Finalizada | Operador encerrou; o percurso completo fica na tela |
| Sem sinal da antena | Transmissor vivo, mas a antena parou (`antenna.state` ≠ ok) |
| Transmissor desconectado | Nenhuma alteração em `live` por 15 s (batimento a cada 5 s) |
| Sincronizando | Carregando estado ou percurso; ou confirmando o transmissor ao entrar (até 12 s) |
| Conexão interrompida | **Este** dispositivo está sem internet ou com o stream em erro |

Perda de internet do espectador e perda de sinal da antena são estados diferentes. O frescor é medido com `performance.now()` (relógio monotônico): nada depende do relógio de parede de cada aparelho. Fora de *Em andamento*, o marcador fica neutro e as leituras dizem "última leitura conhecida".

## 3. Segurança

* Regras em `firebase/database.rules.json`:
  * leitura pública só de `live` e de `sessions/{id}` (não é possível listar sessões);
  * escrita só por usuário autenticado cujo UID esteja em `access/transmitters` (gravado pelo console; ninguém grava pelo app);
  * validação de formato de cada campo, enumeração de estados e chaves; campos desconhecidos são recusados.
* O transmissor usa Firebase Authentication (e-mail e senha). A senha fica só na memória do gateway; o token é renovado automaticamente.
* O site não contém controles, chaves privadas nem métodos de escrita (verificado em `tests/test_site.py`). A chave de API web do Firebase é pública por natureza e não dá permissão nenhuma.
* O painel do operador só existe em `127.0.0.1:8765` e mantém a checagem de `Origin` dos POSTs.
* O site tem Content-Security-Policy sem `unsafe-inline`, conectando só ao domínio do Realtime Database.

**Privacidade:** coordenadas exatas passam a ser visíveis para quem tiver o endereço do site durante uma sessão. O botão *Ocultar coordenadas* só afeta a tela. Se for preciso restringir o acesso, a evolução natural é exigir login (Firebase Authentication) também para leitura e trocar `".read": true` por uma lista de espectadores.

## 4. Desempenho, limites e custos (telemetria a 1 Hz)

| Medida | Estimativa |
|---|---|
| Escritas do transmissor | 1 lote/s (~350 B) em operação; até 400 operações por lote ao reenviar fila |
| Download por espectador | ~1,5 a 2 MB por hora de sessão (ponto + atualização de `live`) |
| Novo espectador | percurso anterior completo: ~0,75 MB por hora de sessão |
| Armazenamento | ~0,75 MB por hora de trajetória |
| Conexões | 2 por espectador (estado e percurso) + o transmissor |

Plano gratuito Spark do Realtime Database: **100 conexões simultâneas** (cerca de **49 espectadores** ao mesmo tempo), 1 GB armazenado, 10 GB de download por mês. Exemplo: 10 espectadores × 4 h × 8 sessões por mês ≈ 0,6 GB/mês. Acima de ~49 espectadores simultâneos é preciso o plano Blaze (pago por uso).

Desenho: a trajetória é simplificada só no desenho acima de 2.500 posições (os dados ficam completos). Fila do transmissor limitada a 120.000 operações (~16 h a 1 Hz); acima disso os pontos mais antigos da fila são descartados do site e contados no painel (continuam no InfluxDB). Sessões antigas podem ser apagadas do Firebase pelo console sem afetar o registro oficial.

## 5. Configuração

### Firebase (uma vez)

1. Projeto: `canide` (já existe, Realtime Database `canide-default-rtdb`).
2. Authentication > Sign-in method: ativar **E-mail/senha**.
3. Authentication > Users: criar a conta do transmissor (ex.: `transmissor@...`) e copiar o **UID**.
4. Realtime Database > Dados: criar `access/transmitters/<UID>` com valor `true`.
5. Publicar as regras: `firebase deploy --only database` na raiz do repositório (ou colar `firebase/database.rules.json` em Realtime Database > Regras).

### Gateway (computador da antena)

`GATEWAY_PC/firebase.config.json` traz os valores públicos (chave web, endereço do banco, endereço do site). O computador da antena não precisa de login. A credencial é gerada uma única vez, em qualquer computador:

1. Abra o painel (`127.0.0.1:8765`), seção **Firebase · transmissor**, informe e-mail e senha da conta do transmissor, deixe **Manter conectado neste computador** marcado e clique em **Conectar transmissor**.
2. O gateway cria `GATEWAY_PC/transmissor.local.json` com o token de renovação do Firebase (a senha nunca é gravada).
3. Leve a pasta `GATEWAY_PC` inteira para o computador da antena (OneDrive, pendrive ou zip). Ao executar `INICIAR_ECOTAM.bat`, o gateway encontra o arquivo e se conecta sozinho, inclusive se ligar sem internet (tenta de novo com espera crescente até 2 min).

O arquivo está no `.gitignore` e não vai para o GitHub (repositório público); um teste automatizado garante isso. Quem tiver o arquivo pode publicar como transmissor: trate-o como uma chave. Para revogar, desative a conta em Authentication ou remova o UID de `access/transmitters`; **Desconectar e esquecer** apaga o arquivo local. Não há variáveis de ambiente obrigatórias.

### GitHub Pages

1. Repositório > Settings > Pages > Source: **GitHub Actions**.
2. Opcional: Settings > Secrets and variables > Actions > Variables: `FIREBASE_DATABASE_URL` (padrão: `https://canide-default-rtdb.firebaseio.com`).
3. Cada push em `main` (repositório `tagcanide/systemy`) que altere `site/` ou `GATEWAY_PC/` roda os testes e publica (`.github/workflows/pages.yml`). Se o ambiente `github-pages` só aceitar a branch padrão, adicione `redesign-canide` em Settings > Environments > github-pages.

O build (`python site/build.py _site`) junta `site/` com `dashboard.css`, `dashboard.js` e o kit de marca do gateway, com caminhos relativos (funciona no subcaminho `/systemy/`).

## 6. Executar e testar localmente

```powershell
cd GATEWAY_PC
python -m unittest discover -s tests -v        # gateway, sincronização, site, regras
node --test ..\site\tests\sync-core.test.js     # núcleo do espectador
python tests\e2e_online.py                       # ponta a ponta: gateway real + Firebase de teste + 4 espectadores
python tests\demo_online.py                      # demonstração navegável sem antena e sem nuvem
```

`tests/fake_rtdb.py` implementa o protocolo REST do Firebase (escrita multi-caminho, streaming SSE, `orderBy/startAt`). Ele não avalia o arquivo de regras: as regras devem ser verificadas no emulador oficial (`firebase emulators:start --only database,auth --project demo-canide`, requer Java 21) ou no projeto real.

## 7. Diagnóstico de problemas

| Sintoma no painel do operador | Causa provável |
|---|---|
| "E-mail ou senha do transmissor incorretos" | Conta errada ou login por e-mail/senha desativado |
| "sem autorização de transmissor" | Falta `access/transmitters/<UID> = true` |
| Sincronização "Escrita negada" | Regras não publicadas ou UID removido |
| "Sem conexão · reenviando" e fila crescendo | Internet do PC ou Firebase indisponível; a gravação local continua, a fila é enviada na volta |
| Iniciar recusado "Antena sem dados válidos recentes" | COM3 fechada ou RX sem frames há mais de 5 s |

| Sintoma no site | Causa provável |
|---|---|
| Conexão interrompida | Internet do aparelho; tenta de novo sozinho |
| Transmissor desconectado | PC da antena sem internet, gateway fechado ou sincronização desligada |
| Sem sinal da antena | Gateway ativo, mas sem frames do RX |
| Sincronização não configurada | `FIREBASE_DATABASE_URL` inválida no build |
