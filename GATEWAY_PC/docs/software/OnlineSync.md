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
admins/{uid}: true                      papel de administrador (criado no console; ninguém grava pelo app)
access/transmitters/{uid}               computadores autorizados: name, approvedAt, approvedBy, computer (só admin grava)
pairing/{uid}                           pedidos de autorização: code, computer, requestedAt (o próprio computador grava)
devices/{uid}                           presença do transmissor: computer, lastSeen, antenna
audit/{chave}                           auditoria só de acréscimo: type, uid, at, computer, detail
live                                    estado compartilhado (um único objeto pequeno)
  sessionId, status, statusAt, startedAt, source, points, lastKey, heartbeatAt
  antenna: {state: ok|stale|none|disconnected|simulated, ageS}
sessions/{sessionId}
  info:   status, startedAt, pausedAt, resumedAt, finishedAt, source, gatewaySession
  track/{tttttttttttttt_cccccc}: n, t, seq, lat, lon, ga, ba, tc, pa, hu, sat, fix, pv
  events/{chave}: type (start|pause|resume|finish), at
sessoes/, codigos/                      app de controle remoto do site canidé (regras preservadas)
```

* `sessionId`: `AAAAMMDDTHHMMSSZ_xxxxxx` (UTC + aleatório).
* Chave do ponto: 13 dígitos do timestamp UTC em ms + contador. Ordena cronologicamente e é determinística, então reenviar o mesmo lote não duplica nada.
* `n`: número do ponto na sessão (1, 2, 3…). O espectador detecta lacunas comparando com `live.points`.
* Campos do ponto (`t` em ms UTC, `lat`/`lon` em graus, `ga` altitude GPS, `ba` altitude barométrica, `tc` °C, `pa` Pa, `hu` %, `sat`, `fix`, `seq` do rádio, `pv` versão do protocolo) só existem quando vieram na amostra. Sem fix, não há coordenadas.
* Precisão preservada: os valores publicados são os mesmos números gravados no CSV e no InfluxDB (verificado no teste ponta a ponta).

### Autenticação automática do computador (sem login, sem arquivos)

1. Ao abrir o CANIDÉ, o gateway lê a configuração pública do repositório (`firebase.config.json`: chave de API web e endereço do banco, iguais em todo computador).
2. Na **primeira execução** naquele computador, ele cria a própria conta no Firebase Authentication (anônima, sem senha). O token de renovação fica só nele, em `%LOCALAPPDATA%\CANIDE\identidade-dispositivo.json`, cifrado com DPAPI (preso à conta Windows daquele computador; fora do OneDrive e do repositório). Cada computador tem identidade própria; uma não serve em outro.
3. O gateway registra `pairing/{uid}` com o nome do computador e um **código de autorização** (ex.: `JERN-85GZ`) mostrado no painel local.
4. O administrador abre `admin.html` no site, entra com a conta de administrador, confere o código e clica em **Autorizar**. A aprovação grava `access/transmitters/{uid}`.
5. O gateway consulta a cada 10 s; ao ver a aprovação, passa a **Autorizado** e publica sozinho. Nas próximas execuções (reinício, queda de energia) ele renova o token de 1 h automaticamente e reconfere a autorização a cada minuto.
6. **Revogar** (botão em `admin.html`) apaga `access/transmitters/{uid}`: a próxima escrita é recusada pelo servidor, o painel mostra **Revogado** e um novo pedido de autorização aparece. Para eliminar a identidade, apague também a conta em Authentication; o computador cria outra, que precisa de nova aprovação.
7. Recuperação: o botão **Gerar nova identidade** descarta a identidade local e pede nova autorização.

Autenticação do computador e detecção da antena são independentes: o painel mostra **Autorizado** mesmo sem antena; a antena aparece em `live.antenna` e em `devices/{uid}.antenna`.

### Fluxo do transmissor

1. **Iniciar trajetória** verifica: antena com frame há menos de 5 s (ou fonte simulada com a opção de teste marcada) e amostra válida recente. Cria o `sessionId`, registra em `dados/trajetorias.jsonl` e enfileira `live` como *preparando*.
2. O primeiro ponto muda o estado para *em andamento*. Cada amostra vira um ponto na fila; a fila só é enviada com o computador autorizado.
3. **Pausar**: a gravação oficial continua; o site deixa de receber pontos. **Retomar**: *retomando* até o próximo ponto. **Encerrar**: *finalizada*.
4. Cada lote leva batimento (`heartbeatAt`) e estado da antena; sem pontos novos, batimento a cada 5 s. Ações do operador entram na auditoria.

Se o gateway for reiniciado no meio de uma trajetória, `dados/trajetoria_ativa.json` restaura a mesma sessão.

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

| Camada | Como está protegido |
|---|---|
| **Criptografia em trânsito** | Todo tráfego é HTTPS/TLS: site (GitHub Pages), Authentication e Realtime Database |
| **Criptografia em repouso** | Dados do Firebase cifrados pelo Google; identidade de cada computador cifrada com DPAPI (AES-256) no próprio computador |
| **Credenciais** | Nenhuma senha no gateway nem no repositório; tokens de acesso de 1 h renovados automaticamente; nenhum token transportado entre computadores; token do administrador só na memória da aba |
| **Autorização** | Regras do Realtime Database avaliadas no servidor a cada escrita: só UIDs em `access/transmitters` publicam; só `admins` autorizam, revogam e leem listas e auditoria |
| **Auditoria** | `audit/` só aceita acréscimo, com o UID do autor verificado pelo servidor |
| **Página de administração** | Sem segredos no código; dados de pedidos exibidos só como texto (sem HTML); CSP sem scripts externos |

**Não é criptografia ponta a ponta.** O Firebase precisa ler os pontos para distribuí-los, e qualquer pessoa com o endereço do site pode lê-los: a posição durante uma sessão é **pública por decisão**, para que os espectadores só abram o link. Risco: terceiros podem acompanhar o local em tempo real. Para restringir, a evolução é exigir login de espectador (Firebase Authentication) e trocar `".read": true` de `live` e `sessions` por uma lista de espectadores; o transmissor não muda.

**Por que não há Cloud Functions/Admin SDK:** exigiriam o plano Blaze (pago, com cartão). A autorização de dispositivos é feita no servidor pelas regras do Firebase, com o mesmo efeito para este caso (aprovar, revogar, bloquear). Se no futuro for preciso emitir certificados ou tokens personalizados, uma Cloud Function com Admin SDK pode ser adicionada sem mudar o site.

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

### Firebase (uma vez, no console ou pela CLI)

1. Authentication > Sign-in method: **E-mail/senha** (conta do administrador) e **Anônimo** (identidade dos computadores) ativados.
2. Conta do administrador em Authentication; seu UID em `admins/<UID> = true` (Realtime Database > Dados).
3. Regras publicadas: `firebase deploy --only database --project canide` na raiz do repositório.

### Computador da antena

Nenhuma configuração. Copie ou clone o projeto, execute `INICIAR_ECOTAM.bat`, aguarde o código de autorização no painel e aprove em `admin.html`. Isso acontece uma única vez por computador.

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
| Identidade "Aguardando autorização" | Aprove o código em `admin.html` |
| "Revogado" | O administrador revogou; aprove o novo pedido se for o caso |
| "Sem internet" | A antena, o CSV e o InfluxDB continuam; a fila é enviada na volta |
| "Não configurado" | `firebase.config.json` ausente ou inválido |
| Iniciar recusado "Antena sem dados válidos recentes" | COM3 fechada ou RX sem frames há mais de 5 s |

| Sintoma no site | Causa provável |
|---|---|
| Conexão interrompida | Internet do aparelho; tenta de novo sozinho |
| Transmissor desconectado | PC da antena sem internet, gateway fechado ou sincronização desligada |
| Sem sinal da antena | Gateway ativo, mas sem frames do RX |
| Sincronização não configurada | `FIREBASE_DATABASE_URL` inválida no build |
