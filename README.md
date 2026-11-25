# CANIDÉ · Gateway de telemetria ECOTAM

* `GATEWAY_PC/`: gateway do computador da antena (recepção serial, CSV, InfluxDB, painel local em `127.0.0.1:8765`) e transmissor oficial da trajetória online.
* `site/`: site público de acompanhamento (GitHub Pages), somente leitura.
* `firebase/database.rules.json`: regras do Firebase Realtime Database.
* `.github/workflows/pages.yml`: testes e publicação do site.

O InfluxDB local continua sendo o registro oficial. O Firebase só distribui o estado e o percurso da sessão ativa para os espectadores.
Arquitetura, configuração, custos e diagnóstico: [`GATEWAY_PC/docs/software/OnlineSync.md`](GATEWAY_PC/docs/software/OnlineSync.md).
