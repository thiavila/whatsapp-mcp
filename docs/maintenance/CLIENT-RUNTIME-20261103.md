# Cliente MCP — plano local de segurança, 03/11/2026

Base: main d3f645448125a601564b80e60c1e2c6c470bc687 (PR15). Branch dedicada
`codex/mcp-client-runtime-20261103`. Outros agentes possuem o executor original;
esta frente não muda a VM, publica, dispara CI ou altera branches alheias.
O fork não contém GSD. Este documento registra objetivo, decisões e retomada.

## Objetivo e contrato

Corrigir os clientes Python3.11.15/certifi2025.1.31 observados em processos reais.
Preservar Go, três bridges, perfis, bancos, sessões e valores de credenciais.
O artefato Go-only atual não corrige os clientes. Python3.11.18 trata o incidente
emSign no ssl; certifi2026.10.19 remove a raiz afetada. Fontes oficiais:
https://www.python.org/downloads/release/python-31118/ e
https://github.com/astral-sh/uv/releases/tag/0.13.5.

## Arquivos previstos

- `.python-version`, `pyproject.toml`, `uv.lock`: patch Python explícito e
  atualização mínima do certifi, sem atualização geral das dependências.
- `scripts/client/`: pins de distribuição com hashes, montagem isolada em CI,
  canário sem dados reais/LLM/envio, runner sem sync/download em produção.
- `.github/workflows/release.yml`, CI de código e `scripts/ci/check.py`: construir
  e testar a mesma distribuição Linux x64 admitida pelo executor, sem secrets.
- `scripts/maintenance/`: manifesto versionado, validação/extração limitada,
  preparo antes da parada, transição apenas das entradas MCP WhatsApp reconhecidas
  para o runner fixo e rollback de código/configuração sem restaurar dados/auth.
- `tests/`: fixtures sintéticas para admissão, runner, migração restrita de
  configuração, crash windows e rollback que conserva edições alheias.

## Gates e decisões

1. Verificar origem/hash do Python portátil3.11.18, uv e wheels. Não selecionar
   outra versão silenciosamente se a distribuição não existir.
2. Runtime empacotado e runner efetivo não usam o uv global nem instalam/sincronizam
   dependências. Testar initialize/tools-list no SDK real com HOME/store sintéticos,
   sem chamar ferramentas e sem rede externa. Não abrir SQLite de produção no CI.
3. Manifesto novo exige executor compatível; executor antigo deve rejeitar antes
   de parar serviços. Bootstrap do executor pela coordenadora após CI/revisão.
4. Migrar somente comandos/args WhatsApp estritamente reconhecidos. Qualquer entrada
   desconhecida bloqueia antes da parada; nunca reescrever env, headers ou tokens.
   Abranger default+7 perfis, provar ausência de filhos antigos antes de promover.
5. Journal durable antes de qualquer configuração/runtime ativo mudar; recuperação
   restaura somente os campos de comando que ainda correspondem à alteração da
   transação. Fonte e dados atuais permanecem preservados em conflito.
6. Auth8080: preparar probe positivo/negativo sem corpo e transição opt-in via
   configuração revisada usando token existente quando reconciliado. Não gerar,
   copiar para artefatos, rotacionar ou presumir credencial. Sem autoenforcement
   durante este patch se os consumidores não estiverem integralmente reconciliados.
7. UID separado permanece proposta; o executor atual assume UID1000. Não migrar.

## Verificação e entrega

Testes locais sintéticos e diff concreto antes da revisão Claude Opus5.5
(perfil Pessoal, teto USD5). Registrar modelo efetivo, tokens/cache/custo e SHA/diff.
Se egress for recusado, não contornar; entregar diff e revisão pendente.
Nenhum push/CI/merge/deploy por esta frente. CI oficial e canário real na VM são
responsabilidade da coordenadora, após conciliação do trabalho concorrente.

## Implementação local

O cliente usa workflow próprio `client-release.yml` e artefato
`mcp-client-release` (`client.tar.gz` + `manifest.json`). O release Go continua
schema1/Go-only e deixa de ser disparado por mudanças exclusivas do cliente.
Assim, nenhuma bridge/banco precisa parar para corrigir Python. A origem exige
main atual, PR integrado, `MCP CI result`, workflow/commit/repositório exatos,
digest GitHub e manifesto com versões e SHA do payload. O executor antigo
ignora esse workflow; precisa do bootstrap revisado antes de consumir clientes.

Python3.11.18+20261026 Linux x64 e uv0.13.5 têm arquivos e SHA256 fixados em
`scripts/client/pins.json`. O builder só instala wheels com os hashes do lock
num Python portátil novo em diretório temporário. `--break-system-packages`
aplica-se exclusivamente a essa cópia, nunca ao Python do host ou da VM.
O lock só troca certifi e acrescenta pip26.2.2 (grupo bootstrap) e PyYAML6.0.3
(adapter). Nenhuma outra versão já resolvida mudou.

O runner usa uv e Python desse artefato, `--no-project --no-sync --frozen
--offline --no-config --no-python-downloads`, ignora variáveis de
injeção Python/uv e conserva as variáveis de aplicação. O override interno de
BRIDGE_DIR mantém a semântica dos caminhos legados, inclusive stores relativos.
UV_ENV_FILE/UV_ENV_FILE_OVERRIDE/UV_NO_ENV_FILE são preservados de forma opaca para manter
credenciais carregadas legitimamente pelo uv. Arquivos.env nunca são lidos pelo
adapter/deployer. O runner descarta stderr bruto, inclusive erros de dotenv.
O CLI e o canário nunca chamam uma ferramenta MCP: initialize e tools/list,
com catálogo paginado, são o limite do smoke automático.

`client-stage.json` publica somente SHA/payload/inventário de hashes para a
coordenadora. Antes de parar serviços, `client-readiness.json` precisa ser arquivo
regular pertencente a root, sem escrita de grupo/outros (0644 permite leitura
pelo executor UID1000), ligado a esses valores e válido por até1h. Campos:
`sha`, `client_sha256`, `config_inventory`, `checked_at`, `expires_at`,
`external_consumers_fenced`, `profile_tools_compatible`. Os dois últimos só
podem ser true após verificar cron/CLI/outros escritores e leituras/prévias
autorizadas de default+7 perfis. Não inferir isso do stage, do catálogo ou do CI.
Prova ausente só prepara o artefato; nenhuma configuração/serviço muda. Ela é
exigida na PRIMEIRA migração, junto à política root-owned de autonomia. Releases
posteriores já geridos mantêm a autonomia escolhida; não exigem uma nova prova
humana por payload. O gate completo está em CLIENT-ACTIVATION-GATE.md.

O journal `client-pending.json` precede qualquer parada. Pausa os quatro timers,
recusa watchers ocupados, respeita `hermes gateway stop` sem forçar e pausa o
dashboard. Filhos MCP fora desses cgroups bloqueiam o cutover. Configurações YAML
recebem somente os campos command/args estritamente reconhecidos; env/headers
e demais bytes não são serializados para o journal. A validação de inventário
é fail-closed para novos perfis/comandos. A recuperação mantém o estado vivo,
preserva credenciais e alterações alheias e recusa sobrescrever command estrangeiro.
Não há reset Git, swap Go, cópia de banco ou restauração de credencial nesse fluxo.
Antes de executar código já extraído, verifica novamente bytes/links/arquivos
contra o tar admitido. Após retomar, exige os filhos reais no runtime novo e
confere o hash do executável mapeado em /proc; Python antigo/(deleted) não passa.
As fases staged/applying/applied distinguem recusa/adiamento de rollback efetivo.
Staged retoma os timers e permite nova tentativa sem bloquear o SHA. Recuperação
não depende de GitHub. Erro de cliente é registrado/notificado separadamente e
não aborta a fila Go; esta possui seu próprio gate de fonte contra UV legado.
Uma mudança Go posterior pode ser descendente da publicação do cliente se os
arquivos de cliente/pipeline não mudaram. Payload/extracão/control são publicados
por rename após fsync, e releases públicos íntegros não referenciados têm retenção
limitada. Artefatos/cache alterados são preservados para diagnóstico, sem execução.

## Auth8080 — transição separada e compatível

O patch inclui `bridge_auth_probe.py`, opt-in e fora do catálogo MCP. Ele usa
o token já resolvido pelo cliente, restringe o destino às três bridges locais,
proíbe redirects/proxy e confere GET/chats sem ler o corpo: sem token401 e com
token200. Auth off (200/200) falha. Token ausente bloqueia; não provisiona nada.

Após conciliar ownership, uma mudança de configuração via pipeline deve:

1. Reconciliar cada consumidor real da8080 e a referência do token Indexa existente;
   a instância auth off não prova que um token aprovado inexista em outro local.
   Não usar tokens Melancia. Não imprimir valores nem gravá-los no artefato.
2. Atualizar os consumidores para a mesma referência `WHATSAPP_BRIDGE_TOKEN_FILE`
   (ou manter a forma existente aprovada), conservando env/headers e credenciais.
   O cliente já aceita token mesmo quando a bridge ainda está em modo permissivo.
3. Cercar consumidores/CLI/cron e preparar configuração da bridge com
   `WHATSAPP_BRIDGE_AUTH_MODE=enforce` e `WHATSAPP_BRIDGE_REQUIRE_TOKEN=true`.
   Ativar a configuração pelo executor oficial, com journal/config anterior;
   não alterar sessão, DB ou Go. Requer token existente válido antes da parada.
4. Com consumidores ainda cercados, provar 401/200 e saúde connected/logged_in;
   só então retomar. Erro reverte apenas referências/configuração da transação,
   preserva token e dados atuais e mantém cercado se houver conflito.

O patch não ativa enforce automaticamente: o inventário completo e o token
existente ainda não foram conciliados. O probe está coberto com tokens fictícios.

## UID separado — proposta, sem migração

Preferência: UID estático nologin para gateway/dashboard/watchers/MCP/bridges,
sem sudo/docker/socket administrativo. Thiago UID1000 mantém SSH e sudo. Migrar
só o gateway quebra o acesso SQLite direto dos MCPs; per-profile UID exigiria
mudança maior do gateway compartilhado e pools globais. NoNewPrivileges no mesmo
UID não resolve os acessos administrativos fora do cgroup.

Antes da troca: testar sessão humana independente SSH+sudo; cercar todos os
consumidores; mapear somente estado/credenciais do produto e preservar o estado
canônico mais recente; testar os oito perfis e turn-owner. O publisher/broker e
os dois executores assumem UID1000/bus1000: precisam de adapters revisados para
o novo UID/bus ANTES da migração. Nunca montar Docker/adminbus/home inteiro.
Rollback cerca primeiro o novo UID, preserva a última fila/sessão/DB e restaura
somente código/unidades/ownership necessários; schema desconhecido mantém parado
com recuperação humana disponível. Não executar dois grupos sobre o mesmo store.

## Evidência local e limites

- 36 testes de cliente,22 de manutenção e31 de runtime/transações passaram.
- actionlint aprovou as três workflows alteradas/adicionadas.
- Lock: certifi2025.1.31 →2026.10.19; adicionados pip26.2.2 e PyYAML6.0.3.
- SDK real no macOS arm64 com Python3.11.18/uv0.13.5/pip26.2.2/certifi2026.10.19:
  initialize +57 ferramentas, nenhum tools/call, HOME/store sintéticos. Recibo em
  CLIENT-MACOS-CANARY.json. Catálogo idêntico ao da base. A segunda prova usa o
  entrypoint real run-client → launch → chdir → execve → uv → Python, com cwd
  contendo pyproject/.venv/.python-version inválidos e .env sentinela ignorado.
  A prova final repete com UV_ENV_FILE explícito sintético e exige seu marcador
  no processo real, preservando o mesmo catálogo; nenhum segredo real foi usado.
  Isso não é execução do artefato Linux.
- Os dois arquivos Linux baixados conferem SHA oficial. O Python inclui symlinks
  terminfo com diferença de caixa; sua extração em volume macOS padrão falha por
  case-insensitivity. Não é prova de falha em Linux nem extração Linux aprovada.
- Docker Desktop já estava aberto por outra frente, mas o daemon não respondeu
  aos probes limitados. Não iniciei containers nem reiniciei/encerrei Docker alheio.
  Canário Linux UNPRIVILEGED/NoNewPrivileges sem rede e build/relocação reais ficam
  obrigatórios no pipeline. CI usa exatamente o helper de preflight da VM; não
  presume que a política de userns/AppArmor da VM seja igual à do runner.
  CI instala bubblewrap e prepara uma exceção AppArmor restrita ao executável
  bwrap no host efêmero; não desabilita globalmente a restrição e não instala
  nada na VM. O preflight real sob NoNewPrivileges continua obrigatório.
- Nenhuma escrita/instalação/restart na VM, envio de mensagem, token ou perfil real
  foi usado nesta implementação. Primeiro review Opus5.5: NO-GO com4P1; correções
  implementadas com testes reais de YAML/journal. R2 confirmou4P1 resolvidos,
  GOcondicional e3P2; R3 confirmou todos resolvidos, CODEGO semP1/P2. Os dois
  P3finais (preservar UV_NO_ENV_FILE e carga AppArmor idempotente) foram ajustados
  conforme a recomendação. Revisão final Opus5.5: CODEGO, todosP1/P2/P3fechados.
  CI oficial e
  gates VM permanecem pendentes. Nenhum envio real ocorreu nos testes.

## Entrega

Família implementadora: OpenAI/Codex. Família revisora: Anthropic/Claude,
modelo efetivo `claude-opus-5-5` nas quatro chamadas concluídas com success/ok.
Versão final revisada (hash de todos os arquivos de código/CI/testes listados em
CLIENT-REVIEW-SNAPSHOT-FINAL.json):
`d6718ca4da87b45ce2db5083da2fbbb257741482c70de77ac57d851e35510439`.
O orquestrador verificou os bytes antes/depois de cada revisão; o peer usou só
Read/Grep, sem executar checks nem calcular hashes. Recibos originais completos
CLIENT-REVIEW-OPUS55{,-R2,-R3,-FINAL}.json; totais em CLIENT-REVIEW-USAGE.json.

Custo equivalente das revisões: USD3.4333654 (tetoUSD5). Tokens: entrada94,
saída77456, criação de cache189664, leitura de cache1832787. Não houve chamada
LLM de produto/WhatsApp nem envio real. As cópias byte-idênticas dos recibos
estão no diretório central cybersecurity-2026-10-28/reviews, prefixo
`hermes-mcp-client-20261103-opus55-`, com deduplicação por session_id.

Branch local codex/mcp-client-runtime-20261103 baseada em main d3f6454/PR15,
sem push/PR/Actions/status. A coordenadora integra pela fila, revalida base e
lease de manutenção ANTES de qualquer merge, e usa somente o pipeline oficial.
Não liberar produção pelo GO de código ou pelos testes macOS. Ainda faltam
build/relocação/bwrap Linux oficial e provas reais de ferramentas, processo,
recuperação/administração e autonomia da VM sob política atual.
